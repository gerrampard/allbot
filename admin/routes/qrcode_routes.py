"""
二维码路由模块

职责：处理二维码页面显示和重定向
"""
from fastapi import Request
from fastapi.responses import RedirectResponse


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
