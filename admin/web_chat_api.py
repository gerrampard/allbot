import base64
import hashlib
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from loguru import logger

router = APIRouter(prefix="/api/webchat", tags=["webchat"])

# 存储会话信息
web_sessions: Dict[str, Dict[str, Any]] = {}
_sender_index: Dict[str, str] = {}
_media_index: Dict[str, Dict[str, Any]] = {}
_check_auth = None

_MEDIA_DIR = Path(__file__).resolve().parent.parent / "temp" / "webchat_media"

# 单会话模式：固定会话 ID
_FIXED_SESSION_ID = "webchat"


def _ensure_session(session_id: str) -> Dict[str, Any]:
    session_id = _FIXED_SESSION_ID
    if session_id not in web_sessions:
        web_sessions[session_id] = {
            "created_at": int(time.time()),
            "messages": [],
            "sender_wxid": f"web-{session_id}",
        }
        _sender_index[web_sessions[session_id]["sender_wxid"]] = session_id
    return web_sessions[session_id]


def _safe_filename(filename: str) -> str:
    basename = Path(filename or "upload.bin").name
    basename = re.sub(r"[^a-zA-Z0-9._-]+", "_", basename)
    return basename or "upload.bin"


def _guess_media_type(filename: str, fallback: str = "application/octet-stream") -> str:
    mime, _ = mimetypes.guess_type(filename)
    return mime or fallback


def _register_media_file(path: Path, filename: str, media_type: str) -> str:
    media_id = str(uuid.uuid4())
    _media_index[media_id] = {
        "path": str(path),
        "filename": filename,
        "media_type": media_type,
        "created_at": int(time.time()),
    }
    return media_id


def _write_base64_media(payload_base64: str, filename: str, media_type: str) -> Optional[str]:
    try:
        raw = base64.b64decode(payload_base64)
    except Exception as exc:
        logger.warning(f"解码 base64 媒体失败: {exc}")
        return None

    _MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(filename)
    suffix = Path(safe_name).suffix or ""
    media_path = _MEDIA_DIR / f"{uuid.uuid4()}{suffix}"
    try:
        media_path.write_bytes(raw)
    except Exception as exc:
        logger.error(f"写入媒体文件失败: {exc}")
        return None

    return _register_media_file(media_path, safe_name, media_type)


def _get_web_adapter():
    try:
        from adapter.web import get_web_adapter, WebAdapter
    except Exception as e:
        logger.error(f"加载Web适配器失败: {e}")
        return None
    adapter = get_web_adapter()

    try:
        from pathlib import Path
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        config_path = Path(__file__).resolve().parent.parent / "adapter" / "web" / "config.toml"
        if not config_path.exists():
            return adapter

        with open(config_path, "rb") as f:
            config_data = tomllib.load(f)

        enabled = False
        adapter_section = config_data.get("adapter")
        if isinstance(adapter_section, dict) and "enabled" in adapter_section:
            enabled = bool(adapter_section.get("enabled"))
        if not enabled:
            for section in config_data.values():
                if isinstance(section, dict) and "enable" in section:
                    enabled = bool(section.get("enable"))
                    break

        if adapter and adapter.enabled:
            return adapter

        if not enabled:
            return adapter

        adapter = WebAdapter(config_data, config_path)
        if adapter and adapter.enabled:
            return adapter
    except Exception as e:
        logger.error(f"初始化Web适配器失败: {e}")

    return adapter


class WebChatMessage:
    """Web聊天消息"""
    def __init__(
        self,
        session_id: str,
        content: str,
        msg_type: int = 1,
        sender_wxid: str = "web-user",
        extra: Optional[Dict[str, Any]] = None,
    ):
        self.session_id = session_id
        self.content = content
        self.msg_type = msg_type
        self.sender_wxid = sender_wxid
        self.extra = extra or {}
        self.timestamp = int(time.time())
        self.msg_id = str(int(time.time() * 1000))

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "MsgId": self.msg_id,
            "MsgType": self.msg_type,
            "Content": {"string": self.content},
            "FromUserName": {"string": self.sender_wxid},
            "ToUserName": {"string": getattr(self, 'bot_wxid', 'web-bot-user')},
            "IsGroup": False,
            "CreateTime": self.timestamp,
            "platform": "web",
            "session_id": self.session_id,
        }
        payload.update(self.extra)
        return payload


async def _require_auth(request: Request) -> Optional[str]:
    if _check_auth is None:
        return None
    username = await _check_auth(request)
    if not username:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return username


def _normalize_reply_message(reply: Dict[str, Any]) -> Dict[str, Any]:
    msg_type = reply.get("msg_type", "text")
    ts = int(reply.get("timestamp") or time.time())

    if msg_type == "text":
        content_data = reply.get("content", {})
        return {
            "role": "bot",
            "type": "text",
            "content": content_data.get("text", ""),
            "timestamp": ts,
        }

    if msg_type in ["image", "video", "voice"]:
        media_data = reply.get("content", {}).get("media", {})
        kind = media_data.get("kind")
        value = media_data.get("value")
        filename = media_data.get("filename") or f"{msg_type}_{ts}"
        if not Path(filename).suffix:
            if msg_type == "image":
                filename = f"{filename}.jpg"
            elif msg_type == "video":
                filename = f"{filename}.mp4"
            elif msg_type == "voice":
                filename = f"{filename}.wav"
        media_type = _guess_media_type(filename)

        if kind == "base64" and isinstance(value, str) and value:
            media_id = _write_base64_media(value, filename, media_type)
            if media_id:
                return {
                    "role": "bot",
                    "type": msg_type,
                    "content": "",
                    "timestamp": ts,
                    "filename": _safe_filename(filename),
                    "media_url": f"/api/webchat/media/{media_id}",
                }

        if kind == "url" and isinstance(value, str) and value:
            return {
                "role": "bot",
                "type": msg_type,
                "content": "",
                "timestamp": ts,
                "filename": _safe_filename(filename),
                "media_url": value,
            }

        return {
            "role": "bot",
            "type": "text",
            "content": str(reply),
            "timestamp": ts,
        }

    return {
        "role": "bot",
        "type": "text",
        "content": str(reply),
        "timestamp": ts,
    }


def _ingest_pending_replies(adapter, limit: int = 50) -> int:
    """从回复队列消费消息并回写到会话内存中"""
    if not adapter or not getattr(adapter, "enabled", False):
        return 0

    try:
        replies = adapter.pop_replies(limit=limit)
    except Exception as e:
        logger.error(f"消费Web回复队列失败: {e}")
        return 0

    if not replies:
        return 0

    appended = 0
    for reply in replies:
        wxid = reply.get("wxid")
        if not wxid:
            continue
        session_id = _sender_index.get(wxid)
        if not session_id:
            continue
        session = web_sessions.get(session_id)
        if not session:
            continue

        session["messages"].append(_normalize_reply_message(reply))
        appended += 1

    return appended


@router.get("/status")
async def get_webchat_status(request: Request):
    """获取Web聊天状态"""
    try:
        await _require_auth(request)
        
        adapter = _get_web_adapter()
        if adapter:
            return JSONResponse({
                "success": True,
                "data": {
                    "enabled": adapter.enabled,
                    "platform": adapter.platform,
                    "bot_wxid": adapter.bot_identity,
                }
            })
        else:
            return JSONResponse({
                "success": True,
                "data": {
                    "enabled": False,
                    "platform": "web",
                    "bot_wxid": "web-bot",
                }
            })
    except Exception as e:
        logger.error(f"获取Web聊天状态失败: {e}")
        return JSONResponse({"success": False, "error": str(e)})


@router.post("/send")
async def send_message(request: Request):
    """发送消息到Web聊天"""
    try:
        await _require_auth(request)
        
        data = await request.json()
        content = data.get("content", "")
        session_id = _FIXED_SESSION_ID
        msg_type = int(data.get("msg_type", 1))
        
        if not content:
            return JSONResponse({"success": False, "error": "消息内容不能为空"})
        
        session = _ensure_session(session_id)
        
        # 创建消息
        message = WebChatMessage(
            session_id=session_id,
            content=content,
            msg_type=msg_type,
            sender_wxid=session["sender_wxid"],
        )
        
        # 记录消息
        session["messages"].append({
            "role": "user",
            "type": "text",
            "content": content,
            "timestamp": message.timestamp,
        })
        
        # 获取适配器并发送消息
        adapter = _get_web_adapter()
        if not adapter or not adapter.enabled:
            return JSONResponse({
                "success": False,
                "error": "Web适配器未启用"
            })
        
        # 发送到队列
        success = adapter.send_message_to_queue(message.to_dict())
        if not success:
            return JSONResponse({
                "success": False,
                "error": "发送消息失败"
            })

        # 不在发送接口中阻塞等待回复；由前端轮询 sessions/{session_id} 获取。
        _ingest_pending_replies(adapter, limit=50)

        return JSONResponse({
            "success": True,
            "data": {
                "session_id": session_id,
                "user_message": content,
                "msg_id": message.msg_id,
                "timestamp": int(time.time()),
                "status": "queued",
            }
        })
            
    except Exception as e:
        logger.error(f"发送消息失败: {e}")
        return JSONResponse({"success": False, "error": str(e)})


@router.post("/send_file")
async def send_file(
    request: Request,
    session_id: str = Form(...),
    file: UploadFile = File(...),
):
    """上传文件并发送到 Web 对话（图片会作为图片消息入队，其他文件作为文本+附件入队）"""
    try:
        await _require_auth(request)

        session_id = _FIXED_SESSION_ID

        safe_name = _safe_filename(file.filename)
        content_type = file.content_type or _guess_media_type(safe_name)
        raw = await file.read()
        if not raw:
            return JSONResponse({"success": False, "error": "文件内容为空"})

        upload_dir = _MEDIA_DIR / "uploads" / session_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        upload_path = upload_dir / f"{uuid.uuid4()}_{safe_name}"
        upload_path.write_bytes(raw)

        media_id = _register_media_file(upload_path, safe_name, content_type)
        media_url = f"/api/webchat/media/{media_id}"

        session = _ensure_session(session_id)
        ts = int(time.time())

        is_image = (content_type or "").startswith("image/")
        is_video = (content_type or "").startswith("video/")

        message_type = "image" if is_image else ("video" if is_video else "file")
        session["messages"].append({
            "role": "user",
            "type": message_type,
            "content": safe_name,
            "timestamp": ts,
            "filename": safe_name,
            "media_url": media_url,
        })

        adapter = _get_web_adapter()
        if not adapter or not adapter.enabled:
            return JSONResponse({"success": False, "error": "Web适配器未启用"})

        sender_wxid = session["sender_wxid"]

        if is_image:
            md5_value = hashlib.md5(raw).hexdigest()
            extra = {
                "ResourcePath": str(upload_path),
                "ImageMD5": md5_value,
            }
            message = WebChatMessage(
                session_id=session_id,
                content="",
                msg_type=3,
                sender_wxid=sender_wxid,
                extra=extra,
            )
            if not adapter.send_message_to_queue(message.to_dict()):
                return JSONResponse({"success": False, "error": "发送图片消息失败"})
        else:
            extra = {
                "ResourcePath": str(upload_path),
                "Filename": safe_name,
                "FileSize": len(raw),
                "FileContentType": content_type,
            }
            message = WebChatMessage(
                session_id=session_id,
                content=f"[文件] {safe_name}",
                msg_type=1,
                sender_wxid=sender_wxid,
                extra=extra,
            )
            if not adapter.send_message_to_queue(message.to_dict()):
                return JSONResponse({"success": False, "error": "发送文件消息失败"})

        _ingest_pending_replies(adapter, limit=50)

        return JSONResponse({
            "success": True,
            "data": {
                "session_id": session_id,
                "timestamp": ts,
                "status": "queued",
                "media_url": media_url,
                "filename": safe_name,
                "type": message_type,
            },
        })
    except Exception as e:
        logger.error(f"发送文件失败: {e}")
        return JSONResponse({"success": False, "error": str(e)})


@router.get("/media/{media_id}")
async def get_media(request: Request, media_id: str):
    """获取 Web 对话媒体文件（需要后台登录）"""
    await _require_auth(request)

    item = _media_index.get(media_id)
    if not item:
        raise HTTPException(status_code=404, detail="媒体不存在")

    media_path = Path(item.get("path") or "")
    if not media_path.exists() or not media_path.is_file():
        raise HTTPException(status_code=404, detail="媒体文件不存在")

    return FileResponse(
        path=str(media_path),
        filename=item.get("filename") or media_path.name,
        media_type=item.get("media_type") or "application/octet-stream",
    )


@router.get("/sessions")
async def get_sessions(request: Request):
    """获取所有会话"""
    try:
        await _require_auth(request)

        adapter = _get_web_adapter()
        _ingest_pending_replies(adapter, limit=200)
        
        session_id = _FIXED_SESSION_ID
        session = _ensure_session(session_id)
        sessions_data = [{
            "session_id": session_id,
            "created_at": session["created_at"],
            "message_count": len(session["messages"]),
            "sender_wxid": session["sender_wxid"],
        }]
        
        return JSONResponse({
            "success": True,
            "data": sessions_data
        })
    except Exception as e:
        logger.error(f"获取会话列表失败: {e}")
        return JSONResponse({"success": False, "error": str(e)})


@router.get("/sessions/{session_id}")
async def get_session_messages(request: Request, session_id: str):
    """获取会话消息历史"""
    try:
        await _require_auth(request)

        adapter = _get_web_adapter()
        _ingest_pending_replies(adapter, limit=200)
        
        session_id = _FIXED_SESSION_ID
        session = _ensure_session(session_id)
        return JSONResponse({
            "success": True,
            "data": {
                "session_id": session_id,
                "created_at": session["created_at"],
                "sender_wxid": session["sender_wxid"],
                "messages": session["messages"],
            }
        })
    except Exception as e:
        logger.error(f"获取会话消息失败: {e}")
        return JSONResponse({"success": False, "error": str(e)})


def register_web_chat_routes(app, check_auth):
    """注册Web聊天路由"""
    global _check_auth
    _check_auth = check_auth
    app.include_router(router)
    logger.info("Web聊天API路由已注册")
