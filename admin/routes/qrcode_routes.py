"""
二维码路由模块

职责：处理二维码页面显示和重定向
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
        candidates = [
            Path(__file__).resolve().parent.parent / "bot_status.json",
            Path(__file__).resolve().parent.parent.parent / "bot_status.json",
        ]
        status_path = next((p for p in candidates if p.exists()), None)
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
            },
        }

    @app.get("/api/qrcode")
    async def api_qrcode(data: str = ""):
        """简单二维码生成代理（兼容旧逻辑，避免前端引用 404）"""
        if not data:
            return JSONResponse(status_code=400, content={"success": False, "error": "缺少 data 参数"})

        target = f"https://api.qrserver.com/v1/create-qr-code/?data={quote(data)}"
        return RedirectResponse(url=target, status_code=302)
