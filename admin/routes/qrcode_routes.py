"""
@input: bot_status.json 登录状态文件；运行时 bot 实例（用于 869 验证码校验/切换 mac 拉码/卡密重入）
@output: 二维码页面与登录辅助 API（获取二维码、提交验证码、切换 mac 拉码、提交卡密与代理重入流程）
@position: 管理后台未登录入口（/qrcode）与 869 登录流程辅助路由
@auto-doc: Update header and folder INDEX.md when this file changes
"""
import json
import time
from pathlib import Path
from fastapi import Request
from fastapi.responses import RedirectResponse, JSONResponse
from loguru import logger
from urllib.parse import quote


def register_qrcode_routes(app, templates):
    """
    注册二维码相关路由

    Args:
        app: FastAPI 应用实例
        templates: Jinja2 模板实例
    """
    from admin.core.app_setup import get_version_info

    def _status_file_candidates():
        return [
            Path(__file__).resolve().parent.parent / "bot_status.json",
            Path(__file__).resolve().parent.parent.parent / "bot_status.json",
        ]

    def _load_status():
        for candidate in _status_file_candidates():
            if not candidate.exists():
                continue
            try:
                return json.loads(candidate.read_text(encoding="utf-8", errors="replace")), candidate
            except Exception:
                continue
        return {}, None

    def _save_status(data: dict):
        for candidate in _status_file_candidates():
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                logger.warning(f"写入状态文件失败 {candidate}: {e}")

    @app.route('/qrcode')
    async def page_qrcode(request: Request):
        """二维码页面，不需要认证"""
        version_info = get_version_info()
        version = version_info.get("version", "1.0.0")
        update_available = version_info.get("update_available", False)
        latest_version = version_info.get("latest_version", "")
        update_url = version_info.get("update_url", "")
        update_description = version_info.get("update_description", "")

        return templates.TemplateResponse("qrcode.html", {
            "request": request,
            "version": version,
            "update_available": update_available,
            "latest_version": latest_version,
            "update_url": update_url,
            "update_description": update_description
        })

    @app.route('/qrcode_redirect')
    async def qrcode_redirect(request: Request):
        """二维码重定向 API，用于将用户从主页重定向到二维码页面"""
        return RedirectResponse(url='/qrcode')

    @app.get("/api/login/qrcode", response_class=JSONResponse)
    async def api_login_qrcode(request: Request):
        """
        获取登录二维码 URL（供 /qrcode 页面轮询使用）

        返回格式（与 admin/templates/qrcode.html 约定一致）：
        {
          "success": true,
          "data": { "qrcode_url": "...", "expires_in": 240, "timestamp": 0, "uuid": "..." }
        }
        """
        # 优先读取 admin/bot_status.json，其次回退到项目根目录 bot_status.json
        status_path = next((p for p in _status_file_candidates() if p.exists()), None)
        if status_path is None:
            return {"success": False, "error": "未找到 bot_status.json，无法获取二维码"}

        try:
            data = json.loads(status_path.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            logger.error(f"读取 bot_status.json 失败: {e}")
            return {"success": False, "error": "读取状态文件失败"}

        qrcode_url = data.get("qrcode_url")
        uuid = data.get("uuid")

        # 兼容：从 details 中提取二维码链接
        if not qrcode_url:
            details = str(data.get("details") or "")
            import re
            m = re.search(r"(https?://[^\\s]+)", details)
            if m:
                qrcode_url = m.group(1)

        # 兼容：仅有 uuid 时构建二维码地址
        if not qrcode_url and uuid:
            qrcode_url = f"https://api.pwmqr.com/qrcode/create/?url=http://weixin.qq.com/x/{uuid}"

        if not qrcode_url:
            raw_status = str(data.get("status", "") or "").strip().lower()
            if raw_status in {"waiting_login", "scanning", "initialized", "initializing"}:
                return {
                    "success": True,
                    "data": {
                        "pending": True,
                        "status": raw_status or "waiting_login",
                        "message": "二维码生成中，请稍候...",
                        "login_mode": data.get("login_mode") or data.get("device_type") or "",
                    },
                }
            return {"success": False, "error": "未找到二维码信息，请稍后重试"}

        expires_in_raw = data.get("expires_in", 240)
        try:
            expires_in = int(expires_in_raw)
        except Exception:
            expires_in = 240

        return {
            "success": True,
            "data": {
                "qrcode_url": qrcode_url,
                "expires_in": expires_in,
                "timestamp": data.get("timestamp") or time.time(),
                "uuid": uuid or "",
                "login_mode": data.get("login_mode") or data.get("device_type") or "",
                "data62": data.get("data62") or "",
                "ticket": data.get("ticket") or "",
                "needs_auth_key": bool(data.get("needs_auth_key")),
            },
        }

    @app.post("/api/login/verify_code", response_class=JSONResponse)
    async def api_login_verify_code(request: Request):
        """869 专属：手动提交手机上显示的数字验证码（VerifyCode）。"""
        try:
            payload = await request.json()
        except Exception:
            payload = {}

        code = str((payload or {}).get("code", "") or "").strip()
        if not code:
            return JSONResponse(status_code=400, content={"success": False, "error": "缺少 code"})

        try:
            from admin.core.app_setup import get_bot_instance

            wrapper = get_bot_instance()
            wxapi = getattr(wrapper, "bot", wrapper)
            if wxapi is None:
                return JSONResponse(status_code=503, content={"success": False, "error": "机器人实例未初始化"})

            protocol_version = str(getattr(wxapi, "protocol_version", "")).lower()
            if protocol_version != "869":
                return JSONResponse(status_code=400, content={"success": False, "error": "verify_code 仅在 869 客户端可用"})

            if not hasattr(wxapi, "verify_code"):
                return JSONResponse(status_code=400, content={"success": False, "error": "当前客户端不支持 verify_code"})

            data62 = str((payload or {}).get("data62", "") or "").strip()
            ticket = str((payload or {}).get("ticket", "") or "").strip()

            result = await wxapi.verify_code(code, data62=data62, ticket=ticket)
            return {"success": True, "data": result}
        except Exception as e:
            logger.error(f"提交验证码失败: {e}")
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/login/force_mac_qrcode", response_class=JSONResponse)
    async def api_login_force_mac_qrcode(request: Request):
        """869 专属：手动切换 mac 模式拉取二维码。"""
        try:
            from admin.core.app_setup import get_bot_instance

            wrapper = get_bot_instance()
            wxapi = getattr(wrapper, "bot", wrapper)
            if wxapi is None:
                return JSONResponse(status_code=503, content={"success": False, "error": "机器人实例未初始化"})

            protocol_version = str(getattr(wxapi, "protocol_version", "")).lower()
            if protocol_version != "869":
                return JSONResponse(status_code=400, content={"success": False, "error": "仅 869 客户端支持切换 mac 拉码"})

            if not hasattr(wxapi, "get_qr_code"):
                return JSONResponse(status_code=400, content={"success": False, "error": "当前客户端不支持拉取二维码"})

            device_id = str(getattr(wxapi, "device_id", "") or "").strip()
            if not device_id and hasattr(wxapi, "create_device_id"):
                device_id = str(wxapi.create_device_id()).strip()

            uuid, qrcode_url = await wxapi.get_qr_code(
                device_name="mac",
                device_id=device_id,
                print_qr=False,
            )
            now = time.time()
            status_data, _ = _load_status()
            if not isinstance(status_data, dict):
                status_data = {}
            status_data.update(
                {
                    "status": "waiting_login",
                    "details": "等待微信扫码登录（mac）",
                    "qrcode_url": qrcode_url,
                    "uuid": uuid,
                    "expires_in": 240,
                    "timestamp": now,
                    "login_mode": "mac",
                    "device_type": "mac",
                    "device_id": getattr(wxapi, "device_id", "") or device_id,
                    "token_key": getattr(wxapi, "token_key", "") or "",
                    "poll_key": getattr(wxapi, "poll_key", "") or "",
                    "data62": getattr(wxapi, "data62", "") or "",
                    "ticket": getattr(wxapi, "ticket", "") or "",
                }
            )
            _save_status(status_data)

            return {
                "success": True,
                "data": {
                    "qrcode_url": qrcode_url,
                    "uuid": uuid,
                    "expires_in": 240,
                    "timestamp": now,
                    "login_mode": "mac",
                    "data62": status_data.get("data62", ""),
                    "ticket": status_data.get("ticket", ""),
                },
            }
        except Exception as e:
            logger.error(f"切换 mac 拉码失败: {e}")
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.post("/api/login/restart_869_flow", response_class=JSONResponse)
    async def api_login_restart_869_flow(request: Request):
        """869 专属：提交卡密 key/拉码代理，并重新拉取二维码进入流程。"""
        try:
            payload = await request.json()
        except Exception:
            payload = {}

        auth_key = str((payload or {}).get("auth_key", "") or "").strip()
        qrcode_proxy = str((payload or {}).get("qrcode_proxy", "") or "").strip()

        try:
            from admin.core.app_setup import get_bot_instance

            wrapper = get_bot_instance()
            wxapi = getattr(wrapper, "bot", wrapper)
            if wxapi is None:
                return JSONResponse(status_code=503, content={"success": False, "error": "机器人实例未初始化"})

            protocol_version = str(getattr(wxapi, "protocol_version", "") or "").lower()
            if protocol_version != "869":
                return JSONResponse(status_code=400, content={"success": False, "error": "仅 869 客户端支持该接口"})

            if auth_key:
                setattr(wxapi, "auth_key", auth_key)
                auth_keys = getattr(wxapi, "auth_keys", None)
                if not isinstance(auth_keys, list):
                    auth_keys = []
                auth_keys = [str(x).strip() for x in auth_keys if str(x).strip()]
                if auth_key not in auth_keys:
                    auth_keys.insert(0, auth_key)
                setattr(wxapi, "auth_keys", auth_keys)

            if qrcode_proxy:
                setattr(wxapi, "login_qrcode_proxy", qrcode_proxy)

            device_id = str(getattr(wxapi, "device_id", "") or "").strip()
            if not device_id and hasattr(wxapi, "create_device_id"):
                device_id = str(wxapi.create_device_id()).strip()

            device_type = str(getattr(wxapi, "device_type", "") or "").strip().lower()
            login_mode = "mac" if device_type == "mac" else "ipad"

            uuid, qrcode_url = await wxapi.get_qr_code(
                device_name=login_mode,
                device_id=device_id,
                proxy=qrcode_proxy or None,
                print_qr=False,
            )

            now = time.time()
            status_data, _ = _load_status()
            if not isinstance(status_data, dict):
                status_data = {}
            status_data.update(
                {
                    "status": "waiting_login",
                    "details": "等待微信扫码登录",
                    "qrcode_url": qrcode_url,
                    "uuid": uuid,
                    "expires_in": 240,
                    "timestamp": now,
                    "login_mode": login_mode,
                    "device_type": login_mode,
                    "device_id": getattr(wxapi, "device_id", "") or device_id,
                    "token_key": getattr(wxapi, "token_key", "") or "",
                    "poll_key": getattr(wxapi, "poll_key", "") or "",
                    "data62": getattr(wxapi, "data62", "") or "",
                    "ticket": getattr(wxapi, "ticket", "") or "",
                    "needs_auth_key": False,
                }
            )
            _save_status(status_data)

            return {
                "success": True,
                "data": {
                    "qrcode_url": qrcode_url,
                    "uuid": uuid,
                    "expires_in": 240,
                    "timestamp": now,
                    "login_mode": login_mode,
                    "data62": status_data.get("data62", ""),
                    "ticket": status_data.get("ticket", ""),
                },
            }
        except Exception as e:
            logger.error(f"重启 869 登录流程失败: {e}")
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    @app.get("/api/qrcode")
    async def api_qrcode(data: str = ""):
        """简单二维码生成代理（兼容旧逻辑，避免前端引用 404）"""
        if not data:
            return JSONResponse(status_code=400, content={"success": False, "error": "缺少 data 参数"})

        target = f"https://api.qrserver.com/v1/create-qr-code/?data={quote(data)}"
        return RedirectResponse(url=target, status_code=302)
