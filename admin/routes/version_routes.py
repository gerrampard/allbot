"""
版本更新路由模块

职责：处理版本检查、版本更新等 API
"""
import os
import json
import asyncio
from datetime import datetime
from fastapi import Request, Depends
from fastapi.responses import JSONResponse
from loguru import logger


def register_version_routes(app, get_version_info, current_dir,
                           update_progress_manager=None, has_update_manager=False):
    """
    注册版本更新相关路由

    Args:
        app: FastAPI 应用实例
        get_version_info: 获取版本信息函数
        current_dir: 当前目录路径
        update_progress_manager: 更新进度管理器
        has_update_manager: 是否有更新管理器
    """
    from admin.utils import require_auth
    from utils.github_proxy import get_github_url

    # 插件市场API配置
    PLUGIN_MARKET_API = {
        "BASE_URL": "http://v.sxkiss.top"
    }

    @app.post("/api/version/check", response_class=JSONResponse, tags=["系统"])
    async def api_version_check(request: Request):
        """检查版本更新"""
        try:
            # 获取请求数据
            data = await request.json()
            current_version = data.get("current_version", "")

            # 请求插件管理后台服务器检查更新
            try:
                url = f"{PLUGIN_MARKET_API['BASE_URL']}/version/check"
                logger.info(f"正在请求版本检查: {url}")

                import requests
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

                response = requests.post(
                    url,
                    json={"current_version": current_version},
                    timeout=5,
                    verify=False
                )

                if response.status_code == 200:
                    result = response.json()
                    latest_version = result.get("latest_version", "")
                    force_update = bool(result.get("force_update") or result.get("forceUpdate"))

                    # 检查版本是否相同
                    if latest_version == current_version and not force_update:
                        result["update_available"] = False

                        # 更新本地版本信息文件
                        try:
                            version_file = os.path.join(os.path.dirname(current_dir), "version.json")
                            version_info = get_version_info()
                            version_info["update_available"] = False
                            version_info["force_update"] = False
                            version_info["last_check"] = datetime.now().isoformat()

                            with open(version_file, "w", encoding="utf-8") as f:
                                json.dump(version_info, f, ensure_ascii=False, indent=2)

                            logger.info(f"更新版本信息文件成功: {version_file}")
                        except Exception as e:
                            logger.error(f"更新版本信息文件失败: {e}")

                    # 如果有更新
                    elif result.get("update_available", False) or force_update:
                        try:
                            version_file = os.path.join(os.path.dirname(current_dir), "version.json")
                            version_info = get_version_info()
                            version_info["update_available"] = True
                            version_info["force_update"] = force_update
                            version_info["latest_version"] = latest_version
                            version_info["update_url"] = result.get("update_url", "")
                            version_info["update_description"] = result.get("update_description", "")
                            version_info["last_check"] = datetime.now().isoformat()

                            with open(version_file, "w", encoding="utf-8") as f:
                                json.dump(version_info, f, ensure_ascii=False, indent=2)

                            logger.info(f"更新版本信息文件成功: {version_file}")
                        except Exception as e:
                            logger.error(f"更新版本信息文件失败: {e}")

                    result["force_update"] = force_update
                    if force_update:
                        result["update_available"] = True
                    result["success"] = True  # 添加 success 字段供前端判断
                    return result
                else:
                    return {"success": False, "error": f"服务器返回错误状态码: {response.status_code}"}

            except Exception as e:
                logger.error(f"连接版本检查服务器失败: {e}")

            # 如果无法连接到服务器，返回本地版本信息
            version_info = get_version_info()
            local_force_update = bool(version_info.get("force_update"))

            if version_info.get("latest_version", "") == current_version and not local_force_update:
                version_info["update_available"] = False

                try:
                    version_file = os.path.join(os.path.dirname(current_dir), "version.json")
                    with open(version_file, "w", encoding="utf-8") as f:
                        json.dump(version_info, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    logger.error(f"更新版本信息文件失败: {e}")

            version_info["success"] = True  # 添加 success 字段供前端判断
            return version_info

        except Exception as e:
            logger.error(f"版本检查失败: {str(e)}")
            return {"success": False, "error": str(e)}


    @app.post("/api/version/update", response_class=JSONResponse, tags=["系统"])
    async def api_version_update(request: Request, username: str = Depends(require_auth)):
        """执行版本更新"""
        try:
            # 获取请求数据
            data = await request.json()
            current_version = data.get("current_version", "")

            # 获取版本信息
            version_info = get_version_info()
            if not version_info.get("update_available", False):
                return {"success": False, "error": "没有可用的更新"}

            # 检查更新进度管理器是否可用
            if not has_update_manager:
                logger.error("更新进度管理器不可用，无法执行更新")
                return {"success": False, "error": "更新进度管理器不可用"}

            # 启动带进度的更新流程
            async def run_update():
                try:
                    from admin.update_with_progress import update_with_progress
                    await update_with_progress(
                        version_info,
                        update_progress_manager,
                        get_github_url,
                        current_dir
                    )
                    # 更新完成后等待3秒再重启
                    await asyncio.sleep(3)

                    # 重启系统
                    logger.warning("更新完成，准备重启系统...")
                    import sys
                    sys.exit(0)
                except Exception as e:
                    logger.error(f"更新流程执行失败: {e}")

            asyncio.create_task(run_update())

            return {
                "success": True,
                "message": "更新任务已启动，请通过WebSocket监听进度"
            }
        except Exception as e:
            logger.error(f"版本更新失败: {str(e)}")
            return {"success": False, "error": f"版本更新失败: {str(e)}"}


    @app.get("/api/version/info", response_class=JSONResponse, tags=["系统"])
    async def api_version_info():
        """获取当前版本信息"""
        try:
            version_info = get_version_info()
            return {
                "success": True,
                "data": version_info
            }
        except Exception as e:
            logger.error(f"获取版本信息失败: {str(e)}")
            return {"success": False, "error": str(e)}

    # ---------------------------------------------------------------------
    # 兼容端点：旧前端使用 /api/check_update 与 /api/update_bot
    # ---------------------------------------------------------------------

    @app.get("/api/check_update", response_class=JSONResponse, tags=["系统"])
    async def api_check_update(username: str = Depends(require_auth)):
        """兼容旧前端：检查更新（从 version.json 返回）"""
        try:
            version_info = get_version_info()
            current = str(version_info.get("version") or "").lstrip("v")
            latest_raw = version_info.get("latest_version") or version_info.get("version") or ""
            latest = str(latest_raw).lstrip("v")

            force_update = bool(version_info.get("force_update"))
            update_available = bool(version_info.get("update_available"))
            has_update = (force_update or update_available) and (force_update or (latest and latest != current))

            return {
                "success": True,
                "has_update": has_update,
                "latest_version": latest or current,
                "update_url": version_info.get("update_url", ""),
                "update_description": version_info.get("update_description", ""),
            }
        except Exception as e:
            logger.error(f"检查更新失败: {e}")
            return {"success": False, "error": str(e)}

    @app.post("/api/update_bot", response_class=JSONResponse, tags=["系统"])
    async def api_update_bot(request: Request, username: str = Depends(require_auth)):
        """兼容旧前端：触发更新并重启（复用 update_with_progress 流程）"""
        try:
            version_info = get_version_info()
            if not (version_info.get("update_available") or version_info.get("force_update")):
                return {"success": False, "error": "没有可用的更新"}

            if not update_progress_manager:
                return {"success": False, "error": "更新进度管理器不可用"}

            async def run_update():
                try:
                    from admin.update_with_progress import update_with_progress
                    await update_with_progress(
                        version_info,
                        update_progress_manager,
                        get_github_url,
                        current_dir,
                    )
                    await asyncio.sleep(3)
                    logger.warning("更新完成，准备重启系统...")
                    import sys
                    sys.exit(0)
                except Exception as e:
                    logger.error(f"更新流程执行失败: {e}")

            asyncio.create_task(run_update())
            return {"success": True, "message": "更新任务已启动"}
        except Exception as e:
            logger.error(f"触发更新失败: {e}")
            return {"success": False, "error": str(e)}
