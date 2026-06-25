"""
@input: OpenClawGatewayClient, ClawPlugin 配置
@output: EventHandler 类 — 网关事件分发、pending run 终态收敛、媒体回传、事件转发
@position: 事件处理层，负责所有从 OpenClaw 推送的事件的内部处理与转发
@auto-doc: Update header and folder INDEX.md when this file changes
"""

import asyncio
import os
import time
from typing import Any, Dict, List, Optional

from loguru import logger

from WechatAPI import WechatAPIClient
from .gateway_client import _safe_text, _compact_json, _dump_json, WatchRoute
from .media_pipeline import MediaPipeline


class EventHandler:
    """网关事件处理器。

    职责：
    - 事件分发（agent/chat/health/shutdown/node.pair/device.pair/cron 等）
    - Pending run 终态收敛
    - 模型错误自动重试
    - 事件转发到固定 to-wxids
    - 入站媒体回传到微信
    """

    def __init__(self, plugin, media_pipeline: MediaPipeline):
        self.plugin = plugin
        self.mp = media_pipeline
        self.gateway = plugin.gateway
        self.bot = plugin.bot
        self.stream_reply_enable = plugin.stream_reply_enable
        self.event_forward_enable = plugin.event_forward_enable
        self.event_forward_allowed = plugin.event_forward_allowed
        self.event_forward_to_wxids = plugin.event_forward_to_wxids
        self.event_mention_in_group = plugin.event_mention_in_group

    async def on_gateway_event(self, frame: dict):
        if not isinstance(frame, dict):
            return

        event_name = _safe_text(frame.get("event") or frame.get("name") or frame.get("method")).strip()
        payload = frame.get("payload", {})
        run_id = self.plugin._extract_run_id_from_event(frame)

        logger.info("[Claw] 事件处理: event={} run_id={}", event_name, run_id or "-")

        # 忽略纯系统心跳/节拍事件
        if event_name in {"presence", "tick", "heartbeat"}:
            return
        if event_name == "sessions.changed":
            logger.debug("[Claw] sessions.changed 事件已接收")
            return
        if event_name == "voicewake.changed":
            logger.debug("[Claw] voicewake.changed 事件已接收")
            return
        if event_name in {"exec.approval.requested", "exec.approval.resolved",
                          "plugin.approval.requested", "plugin.approval.resolved"}:
            logger.debug("[Claw] {} 事件已接收", event_name)
            return
        if event_name in {"session.message", "session.operation", "session.tool"}:
            logger.info("[Claw] 会话事件: event=%s run_id=%s payload_keys=%s", event_name, run_id or "-", list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__)
            if run_id:
                self.plugin._cleanup_pending_run_routes()
                pending = self.plugin._pending_run_routes.get(run_id)
                if pending:
                    self.plugin._update_pending_run_meta(run_id, lastProgressAt=time.time(), watchdogTriggered=False)
            return
        if event_name == "shutdown":
            logger.warning("[Claw] 收到网关 shutdown 事件")
            return
        if event_name in {"node.pair.requested", "node.pair.resolved", "device.pair.requested", "device.pair.resolved"}:
            logger.info("[Claw] {} 事件已接收", event_name)
            return
        if event_name == "cron":
            logger.debug("[Claw] cron 事件已接收")
            return

        if run_id:
            self.plugin._cleanup_pending_run_routes()
            pending = self.plugin._pending_run_routes.get(run_id)
            if pending:
                self.plugin._update_pending_run_meta(run_id, lastProgressAt=time.time(), watchdogTriggered=False)
            if isinstance(payload, dict):
                update_mode, stream_text = self.plugin._extract_stream_text_update(event_name, payload)
                if stream_text:
                    if update_mode == "append":
                        self.plugin._pending_run_texts[run_id] = f"{self.plugin._pending_run_texts.get(run_id, '')}{stream_text}".strip()
                    else:
                        self.plugin._pending_run_texts[run_id] = stream_text
                    if pending and self.stream_reply_enable:
                        route, _ = pending
                        await self.plugin._maybe_send_stream_update_to_route(run_id, route)
            if pending:
                route, _ = pending
                if not isinstance(payload, dict):
                    return
                media_sent = await self._maybe_send_gateway_media(run_id, route, payload)
                meta = self.plugin._pending_run_meta.get(run_id) or {}
                session_key = _safe_text(meta.get("sessionKey")).strip()
                if not self.plugin._is_run_completion_event(event_name, payload):
                    return
                reply_text = await self.plugin._resolve_best_final_reply_text(
                    run_id, payload, session_key,
                    prefer_history=bool(session_key), require_current_history_turn=bool(session_key),
                )
                pending_text = self.plugin._pending_run_texts.get(run_id, "").strip()
                reply_failure_kind = self.plugin._classify_model_failure_text(reply_text) if reply_text else ""

                if self.plugin._is_explicit_run_error(event_name, payload):
                    error_text = self.plugin._extract_openclaw_error_text(payload) or reply_text
                    if not error_text:
                        error_text = "unknown model error"
                    error_kind = self.plugin._classify_run_error(error_text, payload)
                    if error_kind == "unknown":
                        error_kind = "model_error"
                    if reply_text and not reply_failure_kind:
                        logger.warning("[Claw] 显式错误事件附带正常文本，按文本终态回写 run_id={} event={}", run_id, event_name or "-")
                        await self.plugin._finalize_pending_run_once(run_id, route, reply_text, source=f"event:{event_name or '-'}:text-wins")
                        return
                    if self.plugin._is_non_retryable_run_error(error_kind, error_text, payload):
                        logger.warning("[Claw] 显式错误事件命中不可重试错误，停止自动重试 run_id={} kind={} error={}", run_id, error_kind, error_text or "-")
                        self.plugin._clear_pending_run(run_id)
                        return
                    handled = await self.plugin._retry_pending_run_via_gateway(run_id, route, error_text, error_kind=error_kind)
                    if handled:
                        return
                    logger.warning("[Claw] 显式错误事件终止且自动重试失败 run_id={} error={}", run_id, error_text)
                    self.plugin._clear_pending_run(run_id)
                    return

                if event_name == "agent" and session_key and not reply_text:
                    logger.debug("[Claw] agent 完成事件已收到但无文本，等待 agent.wait 兜底(run_id={})", run_id)
                    return

                if reply_text:
                    failure_kind = reply_failure_kind
                    if failure_kind:
                        if self.plugin._is_terminal_failure_text(reply_text):
                            logger.warning("[Claw] 终态返回模型失败终止提示，抑制回写 run_id={} kind={}", run_id, failure_kind)
                            self.plugin._clear_pending_run(run_id)
                            return
                        if self.plugin._is_non_retryable_run_error(failure_kind, reply_text):
                            logger.warning("[Claw] 终态返回不可重试模型失败文本，停止自动重试 run_id={} kind={}", run_id, failure_kind)
                            self.plugin._clear_pending_run(run_id)
                            return
                        handled = await self.plugin._retry_pending_run_via_gateway(run_id, route, reply_text, error_kind=failure_kind)
                        if handled:
                            return
                        logger.warning("[Claw] 终态返回模型失败文本且自动重试失败 run_id={} kind={}", run_id, failure_kind)
                        self.plugin._clear_pending_run(run_id)
                        return
                    await self.plugin._finalize_pending_run_once(run_id, route, reply_text, source=f"event:{event_name or '-'}")
                    return

                if event_name == "chat" and not media_sent:
                    if session_key:
                        finalized = await self.plugin._maybe_finalize_run_via_chat_history(
                            run_id, route, session_key, reason="chat-empty-complete",
                            min_chars=max(1, len(pending_text)),
                        )
                        if finalized:
                            return
                        logger.info("[Claw] chat 完成事件无文本，等待 agent.wait/chat.history 兜底 run_id={}", run_id)
                        return
                    error_text = self.plugin._extract_openclaw_error_text(payload) or "empty response"
                    error_kind = self.plugin._classify_run_error(error_text, payload)
                    if error_kind == "unknown":
                        error_kind = "empty_model"
                    handled = await self.plugin._retry_pending_run_via_gateway(run_id, route, error_text, error_kind=error_kind)
                    if handled:
                        return
                    logger.warning("[Claw] chat 完成无文本且重试失败 run_id={} error={}", run_id, error_text)
                    self.plugin._clear_pending_run(run_id)
                    return

                if event_name == "chat":
                    logger.info("[Claw] chat 完成事件无文本，结束等待(run_id={})", run_id)
                    self.plugin._clear_pending_run(run_id)
                    return

                if session_key:
                    logger.debug("[Claw] 非 chat 终态事件无文本，继续等待实时事件(run_id={})", run_id)
                    return

                self.plugin._clear_pending_run(run_id)
                return

        # 原始网关事件体只允许转发到显式配置的固定目标
        if not self.event_forward_enable or not self.event_forward_to_wxids:
            return
        if self.event_forward_allowed and event_name not in self.event_forward_allowed:
            return
        message_text = f"[ClawEvent] {event_name}\n{_dump_json(frame.get('payload', {}))}"
        for target_wxid in self.event_forward_to_wxids:
            try:
                await self.bot.send_text_message(target_wxid, message_text)
            except Exception as exc:
                logger.warning("[Claw] 事件转发失败(to_wxid={}): {}", target_wxid, exc)

    async def _maybe_send_gateway_media(self, run_id: str, route: WatchRoute, payload: dict) -> bool:
        # 用户要求：不自动下载/回传网关返回的任何媒体/附件
        return False
