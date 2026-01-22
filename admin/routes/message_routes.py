"""
消息/群聊补齐路由模块

职责：
- 提供旧前端依赖但在重构后缺失的接口，避免页面 404
- 保持实现最小化：仅做参数校验与最基本的 bot 调用
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, Optional

from fastapi import Request, Depends
from fastapi.responses import JSONResponse
from loguru import logger


def register_message_routes(app) -> None:
    from admin.utils import require_auth
    from admin.core.app_setup import get_bot_instance

    @app.post("/api/send_message", response_class=JSONResponse, tags=["联系人"])
    async def api_send_message(request: Request, username: str = Depends(require_auth)):
        """发送文本消息到指定联系人（兼容旧前端）"""
        try:
            payload = await request.json()
        except Exception:
            return {"success": False, "error": "请求体不是合法 JSON"}

        to_wxid = payload.get("to_wxid")
        content = payload.get("content")
        at_users = payload.get("at", "")

        if not to_wxid or not content:
            return {"success": False, "error": "缺少必要参数，需要 to_wxid 与 content"}

        bot = get_bot_instance()
        if bot is None:
            return {"success": False, "error": "机器人实例未初始化，请确保机器人已启动"}

        target = getattr(bot, "bot", bot)
        send_func = getattr(target, "send_text_message", None)
        if not callable(send_func):
            return {"success": False, "error": "微信 API 不支持发送文本消息"}

        try:
            if inspect.iscoroutinefunction(send_func):
                result = await send_func(to_wxid, content, at_users)
            else:
                result = send_func(to_wxid, content, at_users)
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return {"success": False, "error": f"发送消息失败: {str(e)}"}

        # 尽量兼容旧返回结构（不强依赖具体 SDK 返回值）
        data: Dict[str, Any] = {}
        if isinstance(result, (list, tuple)) and len(result) >= 3:
            data = {
                "client_msg_id": result[0],
                "create_time": result[1],
                "new_msg_id": result[2],
            }
        elif isinstance(result, dict):
            data = result

        return {"success": True, "message": "消息发送成功", "data": data}

    @app.post("/api/group/announcement", response_class=JSONResponse, tags=["联系人"])
    async def api_group_announcement(request: Request, username: str = Depends(require_auth)):
        """获取群公告（兼容旧前端；当前实现返回空公告）"""
        try:
            payload = await request.json()
        except Exception:
            return {"success": False, "error": "请求体不是合法 JSON"}

        wxid = payload.get("wxid")
        if not wxid:
            return {"success": False, "error": "缺少群聊ID(wxid)参数"}

        if not str(wxid).endswith("@chatroom"):
            return {"success": False, "error": "无效的群ID，只有群聊才有公告"}

        # 由于微信 API 限制，公告功能默认返回空
        return {"success": True, "announcement": ""}

