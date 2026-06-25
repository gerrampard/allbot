"""
@input: OpenClawGatewayClient, ClawPlugin 配置
@output: ReplyWriter 类 — 回复分片发送、流式 delta 累积/节流/去重、终态收敛、pending run 生命周期管理
@position: 回复写入层，负责所有 OpenClaw 回复到微信的发送逻辑
@auto-doc: Update header and folder INDEX.md when this file changes
"""

import asyncio
import json
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from WechatAPI import WechatAPIClient
from .gateway_client import _safe_text, _compact_json, _dump_json, OpenClawGatewayClient, WatchRoute


class ReplyWriter:
    """回复写入器：分片/流式/终态/去重/自动重试。

    职责：
    - 回复分片发送（按标点/换行切分，控制单条长度）
    - 流式 delta 累积与节流（字数+时间双阈值）
    - 终态收敛（事件文本/累计文本/chat.history 最长匹配）
    - Pending run 生命周期管理（bind/await/finalize/clear）
    - 模型错误分类与自动重试
    - 群组 @提及
    - 联系人显示名称查找
    """

    def __init__(self, plugin):
        self.plugin = plugin
        self.bot = plugin.bot
        self.gateway = plugin.gateway
        self.stream_reply_enable = plugin.stream_reply_enable
        self.max_reply_chars = plugin.max_reply_chars
        self.trigger_expect_final = plugin.trigger_expect_final
        self.trigger_timeout_seconds = plugin.trigger_timeout_seconds
        self.pending_run_ttl_seconds = plugin.pending_run_ttl_seconds
        self.pending_run_watchdog_enable = plugin.pending_run_watchdog_enable
        self.pending_run_watchdog_seconds = plugin.pending_run_watchdog_seconds
        self.pending_run_watchdog_interval_seconds = plugin.pending_run_watchdog_interval_seconds
        self.retry_hint_to_gateway_enable = plugin.retry_hint_to_gateway_enable
        self._MAX_EXPLICIT_ERROR_RETRIES = plugin._MAX_EXPLICIT_ERROR_RETRIES
        self._DEFERRED_REPLY = plugin._DEFERRED_REPLY

        self._pending_run_routes: Dict[str, tuple[WatchRoute, float]] = plugin._pending_run_routes
        self._pending_run_meta: Dict[str, dict] = plugin._pending_run_meta
        self._pending_run_texts: Dict[str, str] = plugin._pending_run_texts
        self._pending_run_stream_sent_texts: Dict[str, str] = plugin._pending_run_stream_sent_texts
        self._pending_run_stream_sent_at: Dict[str, float] = plugin._pending_run_stream_sent_at
        self._pending_run_media_fingerprints: Dict[str, set[str]] = plugin._pending_run_media_fingerprints
        self._pending_run_finalize_locks: Dict[str, asyncio.Lock] = plugin._pending_run_finalize_locks
        self._pending_run_watchdog_task: Optional[asyncio.Task] = plugin._pending_run_watchdog_task

        # ── 别名绑定：将无下划线方法映射为带下划线别名，供内部代码调用 ──
        self._update_pending_run_meta = self.update_pending_run_meta
        self._clear_pending_run = self.clear_pending_run
        self._retry_pending_run_via_gateway = self.retry_pending_run_via_gateway
        self._is_terminal_failure_text = self.is_terminal_failure_text
        self._is_non_retryable_run_error = self.is_non_retryable_run_error
        self._build_gateway_retry_hint = self.build_gateway_retry_hint
        self._finalize_pending_run_once = self.finalize_pending_run_once
        self._bind_pending_run = self.bind_pending_run
        self._cleanup_pending_run_routes = self.cleanup_pending_run_routes
        self._await_pending_run_final = self.await_pending_run_final
        self._clone_json_payload = self.clone_json_payload
        self._resolve_best_final_reply_text = self.resolve_best_final_reply_text

    # ── Reply Splitting & Sending ────────────────────────────

    def split_reply_chunks(self, content: str) -> list[str]:
        text = _safe_text(content)
        if not text:
            return []
        safe_limit = 900
        limit = min(max(int(self.max_reply_chars or 0), 200), safe_limit)
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        remaining = text
        split_chars = ("\n", "。", "！", "？", "!", "?", "；", ";", "，", ",", " ")
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            window = remaining[: limit + 1]
            split_at = -1
            for marker in split_chars:
                idx = window.rfind(marker)
                if idx > split_at:
                    split_at = idx
            if split_at < int(limit * 0.5):
                split_at = limit
            chunk = remaining[:split_at].rstrip()
            if not chunk:
                chunk = remaining[:limit]
                split_at = len(chunk)
            chunks.append(chunk)
            remaining = remaining[split_at:].lstrip()
        return [item for item in chunks if item]

    async def send_to_route(self, route: WatchRoute, content: str):
        bot = self.plugin.bot  # 实时获取，避免 on_enable 前 bot=None
        if not bot:
            return
        chunks = self.split_reply_chunks(content)
        if not chunks:
            return
        chunk_total = len(chunks)
        if chunk_total > 1:
            logger.info("[Claw] 分片发送(to_wxid={}): chunks={}", route.to_wxid, chunk_total)
        mentioned = False
        for index, chunk in enumerate(chunks, start=1):
            try:
                if not mentioned and route.is_group and route.sender_wxid and self.plugin.event_mention_in_group:
                    mention_text = await self._build_group_mention_text(bot, route, chunk)
                    await bot.send_text_message(route.to_wxid, mention_text, [route.sender_wxid])
                    mentioned = True
                    if index < chunk_total:
                        await asyncio.sleep(0.25)
                    continue
            except Exception as exc:
                logger.warning("[Claw] 发送@消息失败(to_wxid={}): {}", route.to_wxid, exc)
            try:
                await bot.send_text_message(route.to_wxid, chunk)
            except Exception as exc:
                logger.warning("[Claw] 发送文本失败(to_wxid={}): {}", route.to_wxid, exc)
            if index < chunk_total:
                await asyncio.sleep(0.25)

    async def _build_group_mention_text(self, bot: WechatAPIClient, route: WatchRoute, content: str) -> str:
        sender_wxid = _safe_text(route.sender_wxid).strip()
        if not route.is_group or not sender_wxid:
            return content
        nickname = self._lookup_group_member_display(bot, route)
        if not nickname:
            try:
                fetched = await bot.get_nickname(sender_wxid)
            except Exception:
                fetched = ""
            nickname = _safe_text(fetched).strip()
        if self._looks_like_wxid_text(nickname, wxid=sender_wxid):
            return content
        return f"@{nickname} {content}"

    def _lookup_group_member_display(self, bot: WechatAPIClient, route: WatchRoute) -> str:
        sender_wxid = _safe_text(route.sender_wxid).strip()
        if not route.is_group or not sender_wxid:
            return ""
        if route.sender_name and not self._looks_like_wxid_text(route.sender_name, wxid=sender_wxid):
            return route.sender_name
        getter = getattr(bot, "get_local_nickname", None)
        if callable(getter):
            try:
                nickname = _safe_text(getter(sender_wxid, route.to_wxid)).strip()
                if nickname and not self._looks_like_wxid_text(nickname, wxid=sender_wxid):
                    return nickname
            except Exception:
                pass
        return self._lookup_contact_display(sender_wxid)

    def _lookup_contact_display(self, wxid: str) -> str:
        wxid = _safe_text(wxid).strip()
        if not wxid:
            return ""
        try:
            from database.contacts_db import get_contact_from_db
            contact = get_contact_from_db(wxid)
            if not isinstance(contact, dict):
                return ""
            remark = _safe_text(contact.get("remark")).strip()
            nickname = _safe_text(contact.get("nickname")).strip()
            display = remark or nickname
            return "" if self._looks_like_wxid_text(display, wxid=wxid) else display
        except Exception:
            return ""

    def _looks_like_wxid_text(self, text: str, *, wxid: str = "") -> bool:
        value = _safe_text(text).strip()
        wxid_text = _safe_text(wxid).strip()
        if not value:
            return True
        lowered = value.lower()
        if wxid_text and value == wxid_text:
            return True
        if lowered.startswith("wxid_"):
            return True
        if lowered.endswith("@chatroom"):
            return True
        if re.fullmatch(r"[A-Za-z0-9_@.-]{12,}", value):
            return True
        return False

    # ── Stream Reply ─────────────────────────────────────────

    def extract_stream_text_update(self, event_name: str, payload: dict) -> tuple[str, str]:
        if not isinstance(payload, dict):
            return "", ""
        if event_name == "agent":
            data = payload.get("data")
            if isinstance(data, dict):
                full_text = _safe_text(data.get("text")).strip()
                if full_text:
                    return "replace", full_text
                delta_text = _safe_text(data.get("delta")).strip()
                if delta_text:
                    return "append", delta_text
        if event_name == "chat":
            message = payload.get("message")
            if isinstance(message, dict):
                role = _safe_text(message.get("role")).strip().lower()
                if role and role not in {"assistant", "bot"}:
                    return "", ""
                content = message.get("content")
                if isinstance(content, list):
                    texts: list[str] = []
                    for segment in content:
                        if isinstance(segment, dict):
                            segment_text = _safe_text(segment.get("text")).strip()
                        else:
                            segment_text = _safe_text(segment).strip()
                        if segment_text:
                            texts.append(segment_text)
                    if texts:
                        return "replace", "".join(texts).strip()
                content_text = _safe_text(content).strip()
                if content_text:
                    return "replace", content_text
        return "", ""

    def compute_unsent_stream_suffix(self, run_id: str, full_text: str) -> tuple[str, str]:
        current_text = _safe_text(full_text).strip()
        sent_text = self._pending_run_stream_sent_texts.get(run_id, "")
        if not current_text:
            return "", sent_text
        if sent_text and current_text.startswith(sent_text):
            return current_text[len(sent_text):], sent_text
        if sent_text and sent_text.startswith(current_text):
            return "", sent_text
        return current_text, sent_text

    async def maybe_send_stream_update_to_route(self, run_id: str, route: WatchRoute) -> None:
        full_text = self._pending_run_texts.get(run_id, "")
        suffix, _ = self.compute_unsent_stream_suffix(run_id, full_text)
        if not suffix:
            return
        now = time.time()
        last_sent_at = float(self._pending_run_stream_sent_at.get(run_id) or 0.0)
        min_flush_chars = 120
        max_flush_interval_seconds = 2.5
        if len(suffix) < 8 and now - last_sent_at < max_flush_interval_seconds:
            return
        should_flush = len(suffix) >= min_flush_chars or now - last_sent_at >= max_flush_interval_seconds
        if not should_flush:
            return
        await self.send_to_route(route, suffix)
        self._pending_run_stream_sent_texts[run_id] = _safe_text(full_text).strip()
        self._pending_run_stream_sent_at[run_id] = now
        meta = self._pending_run_meta.get(run_id) or {}
        flush_count = int(meta.get("streamFlushCount") or 0) + 1
        self._update_pending_run_meta(run_id, streamFlushCount=flush_count)

    async def send_final_reply_without_duplicate(self, run_id: str, route: WatchRoute, reply_text: str) -> None:
        final_text = _safe_text(reply_text).strip()
        if not final_text:
            return
        suffix, _ = self.compute_unsent_stream_suffix(run_id, final_text)
        if suffix:
            await self.send_to_route(route, suffix)
        self._pending_run_stream_sent_texts[run_id] = final_text
        self._pending_run_stream_sent_at[run_id] = time.time()

    # ── Pending Run Lifecycle ────────────────────────────────

    def update_pending_run_meta(self, run_id: str, **updates: Any) -> None:
        current = self._pending_run_meta.get(run_id) or {}
        current.update(updates)
        self._pending_run_meta[run_id] = current

    def clone_json_payload(self, payload: Any) -> Any:
        try:
            return json.loads(json.dumps(payload, ensure_ascii=False))
        except Exception:
            if isinstance(payload, dict):
                return dict(payload)
            return payload

    def bind_pending_run(
        self, run_id: str, route: WatchRoute, *, session_key: str,
        request_params: Optional[dict] = None, retry_count: int = 0,
    ) -> None:
        self._pending_run_texts.pop(run_id, None)
        self._pending_run_stream_sent_at.pop(run_id, None)
        expires_at = time.time() + self.pending_run_ttl_seconds
        self._pending_run_routes[run_id] = (route, expires_at)
        now = time.time()
        meta = {
            "sessionKey": session_key,
            "retryCount": int(retry_count or 0),
            "acceptedAt": now,
            "lastProgressAt": now,
            "watchdogTriggered": False,
            "finalSent": False,
            "streamFlushCount": 0,
            "finalizerStarted": True,
        }
        if session_key:
            self.plugin._remember_session_route(session_key, route)
        if isinstance(request_params, dict):
            meta["requestParams"] = self.clone_json_payload(request_params)
        self._pending_run_meta[run_id] = meta
        self.plugin._create_task_safe(
            self.await_pending_run_final(run_id),
            name=f"claw-run-finalizer:{run_id[:8]}",
        )

    async def await_pending_run_final(self, run_id: str) -> None:
        await asyncio.sleep(0)
        meta = self._pending_run_meta.get(run_id) or {}
        session_key = _safe_text(meta.get("sessionKey")).strip()
        if not session_key:
            return
        timeout_seconds = max(int(self.pending_run_watchdog_seconds), int(self.pending_run_ttl_seconds))
        try:
            payload = await self.gateway.request(
                method="agent.wait", params={"runId": run_id},
                expect_final=True, timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            logger.warning("[Claw] agent.wait 兜底失败 run_id={} error={}", run_id, exc)
            return
        pending = self._pending_run_routes.get(run_id)
        if not pending:
            return
        route, _ = pending
        reply_text = await self.resolve_best_final_reply_text(run_id, payload, session_key, prefer_history=True)
        if reply_text:
            failure_kind = self.classify_model_failure_text(reply_text)
            if failure_kind:
                if self._is_terminal_failure_text(reply_text):
                    logger.warning("[Claw] agent.wait 收到模型失败终止提示，抑制回写 run_id={} kind={}", run_id, failure_kind)
                    self._clear_pending_run(run_id)
                    return
                if self._is_non_retryable_run_error(failure_kind, reply_text):
                    logger.warning("[Claw] agent.wait 收到不可重试模型失败文本，停止自动重试 run_id={} kind={}", run_id, failure_kind)
                    self._clear_pending_run(run_id)
                    return
                handled = await self._retry_pending_run_via_gateway(run_id, route, reply_text, error_kind=failure_kind)
                if handled:
                    return
                logger.warning("[Claw] agent.wait 收到模型失败文本且自动重试失败 run_id={} kind={}", run_id, failure_kind)
                self._clear_pending_run(run_id)
                return
            await self.finalize_pending_run_once(run_id, route, reply_text, source="agent.wait")
            return
        status = ""
        if isinstance(payload, dict):
            status = _safe_text(payload.get("status")).strip().lower()
        error_text = self.extract_openclaw_error_text(payload) or status or "empty response"
        error_kind = self.classify_run_error(error_text, payload if isinstance(payload, dict) else {})
        if error_kind == "unknown":
            if not status or status == "ok":
                error_kind = "empty_model"
            else:
                error_kind = "model_error"
        if self._is_non_retryable_run_error(error_kind, error_text, payload):
            logger.warning("[Claw] agent.wait 收到不可重试错误，停止自动重试 run_id={} kind={} error={}", run_id, error_kind, error_text or "-")
            self._clear_pending_run(run_id)
            return
        handled = await self._retry_pending_run_via_gateway(run_id, route, error_text or "model error", error_kind=error_kind)
        if handled:
            return
        logger.warning("[Claw] agent.wait 已完成但未取到终态文本 run_id={} status={}", run_id, status or "-")
        self._clear_pending_run(run_id)

    async def retry_pending_run_via_gateway(
        self, run_id: str, route: WatchRoute, error_text: str, *, error_kind: str = "unknown",
    ) -> bool:
        meta = self._pending_run_meta.get(run_id) or {}
        retry_count = int(meta.get("retryCount") or 0)
        request_params = meta.get("requestParams")
        if retry_count >= self._MAX_EXPLICIT_ERROR_RETRIES or not isinstance(request_params, dict):
            return False
        pending_text = self._pending_run_texts.get(run_id, "").strip()
        if pending_text and not self.classify_model_failure_text(pending_text):
            logger.info("[Claw] run 已有正常累计文本，跳过自动重试 run_id={} chars={}", run_id, len(pending_text))
            return False
        if self._is_non_retryable_run_error(error_kind, error_text):
            logger.warning("[Claw] 命中不可重试错误，停止自动重试 run_id={} kind={} error={}", run_id, error_kind or "-", error_text or "-")
            return False
        session_key = _safe_text(meta.get("sessionKey")).strip()
        retry_params = self.clone_json_payload(request_params) or {}
        retry_params["idempotencyKey"] = uuid.uuid4().hex
        next_retry_count = retry_count + 1
        retry_hint = self._build_gateway_retry_hint(error_kind, error_text)
        retry_notice = retry_hint if (self.retry_hint_to_gateway_enable and retry_hint) else ""
        if not retry_notice:
            retry_notice = "Retry this request once and return a complete response."
        retry_params["message"] = f"[Gateway Retry Notice]\n{retry_notice}".strip()
        logger.warning("[Claw] 显式错误触发自动重试 old_run_id={} retry={} error={}", run_id, next_retry_count, error_text or "-")
        self._clear_pending_run(run_id)
        try:
            payload = await self.gateway.request(
                method="agent", params=retry_params, expect_final=False,
                timeout_seconds=max(6, min(self.trigger_timeout_seconds, 20)),
            )
        except Exception as exc:
            logger.warning("[Claw] 自动重试发起失败 old_run_id={} error={}", run_id, exc)
            return False
        reply_text = self.extract_openclaw_reply_text(payload).strip()
        if reply_text:
            failure_kind = self.classify_model_failure_text(reply_text)
            if failure_kind:
                logger.warning("[Claw] 自动重试返回模型失败文本，抑制回写 old_run_id={} kind={}", run_id, failure_kind)
                return True
            await self.send_openclaw_reply(route, reply_text)
            return True
        new_run_id = self.extract_openclaw_run_id(payload)
        if not new_run_id:
            logger.warning("[Claw] 自动重试未返回 runId old_run_id={}", run_id)
            return False
        self.bind_pending_run(new_run_id, route, session_key=session_key, request_params=request_params, retry_count=next_retry_count)
        logger.info("[Claw] 自动重试已受理 old_run_id={} new_run_id={} sessionKey={}", run_id, new_run_id, session_key or "-")
        return True

    async def send_openclaw_reply(self, route: WatchRoute, reply_text: str) -> None:
        text = _safe_text(reply_text).strip()
        if text:
            await self.send_to_route(route, text)

    async def finalize_pending_run_once(self, run_id: str, route: WatchRoute, reply_text: str, *, source: str) -> bool:
        text = _safe_text(reply_text).strip()
        if not text:
            return False
        lock = self._get_pending_run_finalize_lock(run_id)
        async with lock:
            if run_id not in self._pending_run_routes:
                return False
            meta = self._pending_run_meta.get(run_id) or {}
            if bool(meta.get("finalSent")):
                return False
            self.update_pending_run_meta(run_id, finalSent=True, finalSentAt=time.time(), finalSentBy=_safe_text(source).strip() or "-")
            await self.send_final_reply_without_duplicate(run_id, route, text)
            self._clear_pending_run(run_id)
            return True

    def _get_pending_run_finalize_lock(self, run_id: str) -> asyncio.Lock:
        lock = self._pending_run_finalize_locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            self._pending_run_finalize_locks[run_id] = lock
        return lock

    def clear_pending_run(self, run_id: str) -> None:
        """清理 run 关联缓存。"""
        self._pending_run_routes.pop(run_id, None)
        self._pending_run_meta.pop(run_id, None)
        self._pending_run_texts.pop(run_id, None)
        self._pending_run_stream_sent_texts.pop(run_id, None)
        self._pending_run_stream_sent_at.pop(run_id, None)
        self._pending_run_media_fingerprints.pop(run_id, None)
        self._pending_run_finalize_locks.pop(run_id, None)

    # ── Final Reply Resolution ───────────────────────────────

    def extract_openclaw_run_id(self, payload: Any) -> str:
        seen: set[int] = set()
        def walk(node: Any, depth: int = 0) -> str:
            if depth > 6:
                return ""
            if isinstance(node, dict):
                node_id = id(node)
                if node_id in seen:
                    return ""
                seen.add(node_id)
                for key in ("runId", "run_id"):
                    value = _safe_text(node.get(key)).strip()
                    if value:
                        return value
                for key in ("data", "payload", "result", "message", "messages", "items", "payloads"):
                    if key in node:
                        found = walk(node.get(key), depth + 1)
                        if found:
                            return found
                for value in node.values():
                    found = walk(value, depth + 1)
                    if found:
                        return found
                return ""
            if isinstance(node, list):
                for item in node:
                    found = walk(item, depth + 1)
                    if found:
                        return found
            return ""
        return walk(payload)

    def extract_run_id_from_event(self, frame: Any) -> str:
        if not isinstance(frame, dict):
            return ""
        for key in ("runId", "run_id"):
            value = _safe_text(frame.get(key)).strip()
            if value:
                return value
        return self.extract_openclaw_run_id(frame.get("payload"))

    def extract_session_key_from_payload(self, payload: Any) -> str:
        seen: set[int] = set()
        def walk(node: Any, depth: int = 0) -> str:
            if depth > 8:
                return ""
            if isinstance(node, dict):
                node_id = id(node)
                if node_id in seen:
                    return ""
                seen.add(node_id)
                for key in ("sessionKey", "session_key"):
                    value = _safe_text(node.get(key)).strip()
                    if value:
                        return value
                for key in ("data", "payload", "result", "message", "messages", "items", "payloads"):
                    if key in node:
                        found = walk(node.get(key), depth + 1)
                        if found:
                            return found
                for value in node.values():
                    found = walk(value, depth + 1)
                    if found:
                        return found
                return ""
            if isinstance(node, list):
                for item in node:
                    found = walk(item, depth + 1)
                    if found:
                        return found
            return ""
        return walk(payload)

    def resolve_event_route(self, frame: dict, run_id: str) -> Optional[WatchRoute]:
        if run_id:
            pending = self._pending_run_routes.get(run_id)
            if pending:
                return pending[0]
            meta = self._pending_run_meta.get(run_id) or {}
            session_key = _safe_text(meta.get("sessionKey")).strip()
            if session_key:
                route = self.plugin._session_routes.get(session_key)
                if route:
                    return route
        session_key = self.extract_session_key_from_payload(frame)
        if session_key:
            return self.plugin._session_routes.get(session_key)
        return None

    def extract_openclaw_reply_text(self, payload: Any) -> str:
        parts: list[str] = []
        seen_texts: set[str] = set()
        assistant_roles = {"assistant", "bot"}
        def add(text: Any):
            value = _safe_text(text).strip()
            if not value or value in seen_texts:
                return
            seen_texts.add(value)
            parts.append(value)
        def walk(node: Any, depth: int = 0, *, from_message: bool = False):
            if depth > 7:
                return
            if isinstance(node, str):
                if from_message:
                    add(node)
                return
            if isinstance(node, dict):
                role = _safe_text(node.get("role")).strip().lower()
                is_assistant_message = bool(role) and role in assistant_roles
                if not role or is_assistant_message:
                    for key in ("text", "delta"):
                        if key in node:
                            add(node.get(key))
                if role and role not in assistant_roles:
                    message_context = False
                else:
                    message_context = bool(from_message) or is_assistant_message
                content = node.get("content")
                if content is not None:
                    walk(content, depth + 1, from_message=message_context)
                message = node.get("message")
                if message is not None:
                    walk(message, depth + 1, from_message=message_context)
                payloads = node.get("payloads")
                if payloads is not None:
                    walk(payloads, depth + 1, from_message=message_context)
                for key in ("data", "payload", "result", "response", "messages", "items", "output", "outputs"):
                    if key in node:
                        walk(node.get(key), depth + 1, from_message=from_message)
                return
            if isinstance(node, list):
                for item in node:
                    walk(item, depth + 1, from_message=from_message)
        walk(payload)
        return "\n".join(parts).strip()

    def pick_longest_reply_text(self, *texts: Any) -> str:
        best = ""
        for text in texts:
            candidate = _safe_text(text).strip()
            if candidate and len(candidate) > len(best):
                best = candidate
        return best

    async def resolve_best_final_reply_text(
        self, run_id: str, payload: Any, session_key: str,
        *, prefer_history: bool = False, require_current_history_turn: bool = False,
    ) -> str:
        pending_text = _safe_text(self._pending_run_texts.get(run_id, "")).strip()
        payload_text = self.extract_openclaw_reply_text(payload).strip()
        sent_text = _safe_text(self._pending_run_stream_sent_texts.get(run_id, "")).strip()
        best = self.pick_longest_reply_text(payload_text, pending_text)
        expected_user_marker = self._extract_expected_history_user_marker(run_id)
        should_fetch_history = bool(session_key) and (
            prefer_history or not best or (sent_text and len(best) <= len(sent_text))
            or (sent_text and best and not best.startswith(sent_text))
        )
        if should_fetch_history:
            history_text, history_turn_matched = await self.fetch_assistant_reply_via_chat_history(
                session_key, expected_user_marker=(expected_user_marker if require_current_history_turn else ""),
            )
            if require_current_history_turn and expected_user_marker and not history_turn_matched:
                # chat.history 中没有当前用户消息（deliver=false 导致），但如果有 payload/pending 文本可直接使用
                if not best:
                    logger.info("[Claw] chat.history marker不匹配且无pending/payload文本，继续等待 run_id={}", run_id)
                    return ""
                logger.info("[Claw] chat.history marker不匹配但已有文本(best_len={})，直接使用，run_id={}", len(best), run_id)
            else:
                best = self.pick_longest_reply_text(best, history_text)
            if not best:
                await asyncio.sleep(0.6)
                history_text, history_turn_matched = await self.fetch_assistant_reply_via_chat_history(
                    session_key, expected_user_marker=(expected_user_marker if require_current_history_turn else ""),
                )
                if require_current_history_turn and expected_user_marker and not history_turn_matched:
                    if not best:
                        logger.info("[Claw] chat.history 二次查询仍未推进到当前用户消息且无文本，run_id={}", run_id)
                        return ""
                    logger.info("[Claw] chat.history 二次查询marker不匹配但已有文本(best_len={})，继续使用，run_id={}", len(best), run_id)
                else:
                    best = self.pick_longest_reply_text(best, history_text)
        return best

    async def fetch_assistant_reply_via_chat_history(self, session_key: str, *, expected_user_marker: str = "") -> tuple[str, bool]:
        session_key = _safe_text(session_key).strip()
        if not session_key:
            return "", not bool(_safe_text(expected_user_marker).strip())
        try:
            payload = await self.gateway.request(
                method="chat.history", params={"sessionKey": session_key},
                expect_final=False, timeout_seconds=10,
            )
        except Exception as exc:
            logger.warning("[Claw] chat.history 获取失败 sessionKey={} error={}", session_key, exc)
            return "", not bool(_safe_text(expected_user_marker).strip())
        result = self._extract_assistant_reply_from_chat_history(payload, expected_user_marker=expected_user_marker)
        return result

    def _extract_assistant_reply_from_chat_history(self, payload: Any, *, expected_user_marker: str = "") -> tuple[str, bool]:
        if not isinstance(payload, dict):
            return "", not bool(_safe_text(expected_user_marker).strip())
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return "", not bool(_safe_text(expected_user_marker).strip())
        assistant_roles = {"assistant", "bot"}
        user_roles = {"user", "human"}
        last_user_index = -1
        fallback_boundary_index = -1
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            role = _safe_text(message.get("role")).strip().lower()
            if role in user_roles:
                last_user_index = index
            if role and role not in assistant_roles:
                fallback_boundary_index = index
        if last_user_index < 0:
            last_user_index = fallback_boundary_index
        if last_user_index < 0:
            return "", not bool(_safe_text(expected_user_marker).strip())
        marker = _safe_text(expected_user_marker).strip()
        latest_user_text = ""
        if 0 <= last_user_index < len(messages):
            latest_user_text = self._extract_text_from_chat_history_message(messages[last_user_index])
        user_turn_matched = True
        if marker:
            user_turn_matched = marker in latest_user_text
            if not user_turn_matched:
                return "", False
        start = last_user_index + 1
        parts: list[str] = []
        for message in messages[start:]:
            if not isinstance(message, dict):
                continue
            role = _safe_text(message.get("role")).strip().lower()
            if role and role not in assistant_roles:
                continue
            text = self._extract_text_from_chat_history_message(message)
            if text:
                parts.append(text)
        return "\n".join(parts).strip(), user_turn_matched

    def _extract_text_from_chat_history_message(self, message: dict) -> str:
        if not isinstance(message, dict):
            return ""
        parts: list[str] = []
        content = message.get("content")
        if isinstance(content, list):
            seg_parts: list[str] = []
            for segment in content:
                if isinstance(segment, dict):
                    seg_text = _safe_text(segment.get("text")).strip()
                else:
                    seg_text = _safe_text(segment).strip()
                if seg_text:
                    seg_parts.append(seg_text)
            if seg_parts:
                parts.append("".join(seg_parts))
        else:
            content_text = _safe_text(content).strip()
            if content_text:
                parts.append(content_text)
        direct_text = _safe_text(message.get("text")).strip()
        if direct_text:
            parts.append(direct_text)
        return "\n".join([p for p in parts if p]).strip()

    def _extract_expected_history_user_marker(self, run_id: str) -> str:
        meta = self._pending_run_meta.get(run_id) or {}
        request_params = meta.get("requestParams")
        if not isinstance(request_params, dict):
            return ""
        request_message = _safe_text(request_params.get("message")).strip()
        if not request_message:
            return ""
        msg_id_match = re.search(r"^- msg_id:\s*([^\n]+)", request_message, flags=re.MULTILINE)
        if msg_id_match:
            return f"- msg_id: {msg_id_match.group(1).strip()}"
        return request_message[:160]

    # ── Error Classification ─────────────────────────────────

    def extract_openclaw_error_text(self, payload: Any) -> str:
        texts: list[str] = []
        seen: set[str] = set()
        def add(value: Any):
            text = _safe_text(value).strip()
            if not text or text in seen:
                return
            seen.add(text)
            texts.append(text)
        def walk(node: Any, depth: int = 0):
            if depth > 6:
                return
            if isinstance(node, str):
                add(node)
                return
            if isinstance(node, dict):
                for key in ("message", "error", "errorMessage", "error_message", "reason",
                             "detail", "details", "stopReason", "stop_reason",
                             "statusText", "finishReason", "finish_reason", "phase", "code"):
                    if key in node:
                        walk(node.get(key), depth + 1)
                for key in ("data", "payload", "result"):
                    if key in node:
                        walk(node.get(key), depth + 1)
                return
            if isinstance(node, list):
                for item in node:
                    walk(item, depth + 1)
        walk(payload)
        return " | ".join(texts[:4]).strip()

    def classify_run_error(self, error_text: str, payload: Any = None) -> str:
        parts = [_safe_text(error_text).strip().lower()]
        if payload is not None:
            try:
                parts.append(_safe_text(_compact_json(payload, 1600)).strip().lower())
            except Exception:
                pass
        merged = " ".join(part for part in parts if part)
        timeout_keywords = {"timeout", "timed_out", "timed out", "llm request timed out", "network_error",
                            "deadline exceeded", "deadline_exceeded", "econnreset", "etimedout",
                            "connection reset", "超时", "请求超时"}
        empty_keywords = {"empty_model", "empty response", "empty reply", "blank response",
                          "no response", "no output", "empty output", "模型空回复", "空回复", "无回复"}
        model_keywords = {"model_error", "provider_error", "rate limit", "rate_limit", "rate_limited",
                          "too many requests", "insufficient quota", "insufficient_quota",
                          "quota exceeded", "quota_exceeded", "context length", "context_length",
                          "context_length_exceeded", "maximum context length", "max context length",
                          "invalid request", "invalid_request", "bad request", "unauthorized",
                          "forbidden", "permission denied", "access denied", "service unavailable",
                          "server overloaded", "overloaded", "invalid model", "model not found",
                          "model unavailable", "unavailable", "模型错误", "模型不可用", "限流",
                          "频率限制", "配额", "额度不足", "余额不足", "上下文长度", "最大上下文",
                          "无权限", "未授权", "禁止访问", "服务不可用", "服务器过载", "过载",
                          "failovererror", "an error occurred while processing your request",
                          "help.openai.com", "help center"}
        if any(keyword in merged for keyword in timeout_keywords):
            return "timeout"
        if any(keyword in merged for keyword in empty_keywords):
            return "empty_model"
        if any(keyword in merged for keyword in model_keywords):
            return "model_error"
        return "unknown"

    def classify_model_failure_text(self, reply_text: str) -> str:
        text = _safe_text(reply_text).strip()
        if not text:
            return "empty_model"
        lowered = text.lower()
        if len(lowered) > 280:
            strong_markers = ("an error occurred while processing your request", "help.openai.com",
                              "request id", "retry failed", "model timeout retry failed",
                              "model error persisted", "model returned empty", "failovererror")
            if not any(marker in lowered for marker in strong_markers):
                return ""
        kind = self.classify_run_error(text)
        if kind == "unknown":
            return ""
        if kind == "timeout":
            if any(marker in lowered for marker in ("model", "retry", "failed", "please try again later", "error occurred", "重试", "失败")):
                return kind
            return ""
        if kind == "model_error":
            if any(marker in lowered for marker in (
                "model", "provider", "error", "failed", "invalid", "unavailable",
                "rate limit", "too many requests", "quota", "unauthorized", "forbidden",
                "permission", "access denied", "please try again later", "help center",
                "重试", "失败", "限流", "配额", "额度", "无权限")):
                return kind
            return ""
        if kind == "empty_model":
            if any(marker in lowered for marker in ("empty", "blank", "no response", "no output", "空回复", "无回复")):
                return kind
            return ""
        return ""

    def is_non_retryable_run_error(self, error_kind: str, error_text: str, payload: Any = None) -> bool:
        if error_kind != "model_error":
            return False
        parts = [_safe_text(error_text).strip().lower()]
        if payload is not None:
            try:
                parts.append(_safe_text(_compact_json(payload, 1600)).strip().lower())
            except Exception:
                pass
        merged = " ".join(part for part in parts if part)
        if not merged:
            return False
        hard_markers = ("account has been deactivated", "has been deactivated", "deactivated",
                        "unauthorized", "forbidden", "permission denied", "access denied",
                        "invalid api key", "incorrect api key", "invalid_api_key",
                        "insufficient quota", "quota exceeded", "billing", "payment required",
                        "model not found", "no such model", "invalid model", "invalid request",
                        "invalid_request", "unsupported model", "unsupported",
                        "未授权", "无权限", "禁止访问", "权限不足", "账号已停用",
                        "账号被停用", "额度不足", "配额不足")
        return any(marker in merged for marker in hard_markers)

    def is_terminal_failure_text(self, reply_text: str) -> bool:
        text = _safe_text(reply_text).strip().lower()
        if not text:
            return False
        terminal_markers = ("retry failed", "after retry", "please try again later",
                            "contact us", "help center", "help.openai.com",
                            "重试失败", "请稍后再试", "稍后再试")
        return any(marker in text for marker in terminal_markers)

    def build_gateway_retry_hint(self, error_kind: str, error_text: str) -> str:
        if error_kind == "timeout":
            return "Model timed out. Retry this request once and return a complete response."
        if error_kind == "empty_model":
            return "Model returned an empty response. Retry this request once and return a complete response."
        if error_kind == "model_error":
            return "Model failed to respond correctly. Retry this request once and return a complete response."
        detail = _safe_text(error_text).strip()
        if detail:
            return f"Retry this request once due to model failure. Last error: {detail}"
        return "Retry this request once due to model failure and return a complete response."

    def is_explicit_run_error(self, event_name: str, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        event_name = _safe_text(event_name).strip().lower()
        status = _safe_text(payload.get("status")).strip().lower()
        state = _safe_text(payload.get("state")).strip().lower()
        stream = _safe_text(payload.get("stream")).strip().lower()
        reason = _safe_text(payload.get("reason") or payload.get("stopReason") or payload.get("stop_reason")).strip().lower()
        error_message = _safe_text(payload.get("errorMessage") or payload.get("error_message")).strip()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        phase = _safe_text(data.get("phase")).strip().lower()
        error_states = {"error", "failed", "failure", "timeout", "timed_out", "cancelled", "canceled"}
        if status in error_states or state in error_states or phase in error_states:
            return True
        if error_message:
            return True
        if reason in {"network_error", "model_error", "empty_model", "timeout"}:
            return True
        if event_name == "agent" and stream == "lifecycle" and phase in error_states:
            return True
        if event_name == "chat" and state in error_states:
            return True
        if payload.get("error"):
            return True
        extracted_error = self.extract_openclaw_error_text(payload)
        if self.classify_run_error(extracted_error, payload) != "unknown":
            return True
        return False

    def is_run_completion_event(self, event_name: str, payload: dict) -> bool:
        if not isinstance(payload, dict):
            return False
        status = _safe_text(payload.get("status")).strip().lower()
        if status in {"ok", "error"}:
            return True
        if event_name == "agent":
            stream = _safe_text(payload.get("stream")).strip().lower()
            if stream == "lifecycle":
                data = payload.get("data")
                if isinstance(data, dict):
                    phase = _safe_text(data.get("phase")).strip().lower()
                    if phase in {"end", "done", "completed", "complete", "stop", "stopped", "failed", "error"}:
                        return True
        if event_name == "chat":
            state = _safe_text(payload.get("state")).strip().lower()
            if state in {"final", "done", "completed", "complete", "failed", "error"}:
                return True
        return False

    # ── Watchdog ─────────────────────────────────────────────

    async def pending_run_watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(self.pending_run_watchdog_interval_seconds)
            try:
                await self._tick_pending_run_watchdog()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[Claw] pending-run watchdog 异常")

    async def _tick_pending_run_watchdog(self) -> None:
        if not self.plugin.enable or not self.pending_run_watchdog_enable:
            return
        if not self._pending_run_routes:
            return
        self.cleanup_pending_run_routes()
        if not self._pending_run_routes:
            return
        now = time.time()
        stalled: list[tuple[str, WatchRoute, str]] = []
        for run_id, (route, _expires_at) in list(self._pending_run_routes.items()):
            meta = self._pending_run_meta.get(run_id) or {}
            if not meta:
                continue
            if bool(meta.get("watchdogTriggered")):
                continue
            accepted_at = float(meta.get("acceptedAt") or 0.0) or now
            last_progress_at = float(meta.get("lastProgressAt") or 0.0)
            last_stream_at = float(self._pending_run_stream_sent_at.get(run_id) or 0.0)
            last_progress_at = max(last_progress_at, last_stream_at, accepted_at)
            stalled_for = now - last_progress_at
            if stalled_for < float(self.pending_run_watchdog_seconds):
                continue
            stalled.append((run_id, route, f"stalled_for={int(stalled_for)}s"))
        for run_id, route, detail in stalled:
            logger.warning("[Claw] pending run watchdog 触发但未取到终态 run_id={} {}", run_id, detail)
            self.update_pending_run_meta(run_id, watchdogTriggered=True)

    def cleanup_pending_run_routes(self):
        if not self._pending_run_routes:
            return
        now = time.time()
        expired = [run_id for run_id, (_, expires_at) in self._pending_run_routes.items() if expires_at <= now]
        for run_id in expired:
            self.clear_pending_run(run_id)

    async def _maybe_finalize_run_via_chat_history(
        self, run_id: str, route: WatchRoute, session_key: str, *, reason: str, min_chars: int = 1,
    ) -> bool:
        if run_id not in self._pending_run_routes:
            return True
        history_text, history_turn_matched = await self.fetch_assistant_reply_via_chat_history(
            session_key, expected_user_marker=self._extract_expected_history_user_marker(run_id),
        )
        if not history_turn_matched:
            return False
        if not history_text or len(history_text) < int(min_chars):
            return False
        logger.info("[Claw] 通过 chat.history 收敛终态 run_id={} reason={}", run_id, reason)
        sent = await self.finalize_pending_run_once(run_id, route, history_text, source=f"chat.history:{_safe_text(reason).strip() or '-'}")
        return sent or run_id not in self._pending_run_routes
