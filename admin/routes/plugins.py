"""
插件管理路由模块

职责：处理插件列表、启用/禁用、配置、市场等 API
"""
import asyncio
import os
import json
import shutil
import subprocess
from pathlib import Path
from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from loguru import logger
import aiohttp


def register_plugins_routes(app, current_dir, plugin_manager=None):
    """
    注册插件管理相关路由

    Args:
        app: FastAPI 应用实例
        current_dir: 当前目录路径
        plugin_manager: 插件管理器实例
    """
    from fastapi import Depends
    from admin.utils import require_auth

    @app.get("/api/plugins", response_class=JSONResponse)
    async def api_plugins_list(request: Request, username: str = Depends(require_auth)):
        # 检查认证状态
        try:
            try:
                from utils.plugin_manager import plugin_manager
            except ImportError as e:
                logger.error(f"导入plugin_manager失败: {str(e)}")
                return {"success": False, "error": f"导入plugin_manager失败: {str(e)}"}

            import os
            try:
                import tomllib as toml_parser
            except ImportError:
                try:
                    import tomli as toml_parser
                except ImportError as e:
                    logger.error(f"缺少TOML解析库: {str(e)}")
                    return {"success": False, "error": "缺少TOML解析库，请安装tomli或使用Python 3.11+"}

            # 获取插件信息列表
            plugins_info = plugin_manager.get_plugin_info()

            # 确保返回的数据是可序列化的
            if not isinstance(plugins_info, list):
                plugins_info = []
                logger.error("plugin_manager.get_plugin_info()返回了非列表类型")

            # 获取适配器信息
            adapters_info = []
            adapter_dir = "adapter"
            if os.path.exists(adapter_dir) and os.path.isdir(adapter_dir):
                for dirname in os.listdir(adapter_dir):
                    # 跳过特殊目录
                    if dirname.startswith('.') or dirname == '__pycache__':
                        continue
                    adapter_path = os.path.join(adapter_dir, dirname)
                    if os.path.isdir(adapter_path):
                        # 读取适配器的 config.toml
                        config_path = os.path.join(adapter_path, "config.toml")
                        adapter_info = {
                            "name": dirname,
                            "version": "未知",
                            "description": "适配器",
                            "author": "未知",
                            "enabled": False,
                            "type": "adapter"
                        }

                        if os.path.exists(config_path):
                            try:
                                with open(config_path, "rb") as f:
                                    config = toml_parser.load(f)
                                    adapter_info["version"] = config.get("version", "未知")
                                    adapter_info["description"] = config.get("description", "适配器")
                                    adapter_info["author"] = config.get("author", "未知")
                            except Exception as e:
                                logger.warning(f"读取适配器 {dirname} 配置失败: {str(e)}")

                        adapters_info.append(adapter_info)

            # 记录调试信息
            logger.debug(f"获取到{len(plugins_info)}个插件信息和{len(adapters_info)}个适配器信息")

            # 合并插件和适配器信息
            all_items = plugins_info + adapters_info

            return {
                "success": True,
                "data": {
                    "plugins": all_items
                }
            }
        except Exception as e:
            logger.error(f"获取插件信息失败: {str(e)}")
            return {"success": False, "error": f"获取插件信息失败: {str(e)}"}

    # API: 启用插件
    @app.post("/api/plugins/{plugin_name}/enable", response_class=JSONResponse)
    async def api_enable_plugin(plugin_name: str, request: Request, username: str = Depends(require_auth)):
        # 检查认证状态
        try:
            from utils.plugin_manager import plugin_manager
            from admin.core.app_setup import get_bot_instance

            bot = get_bot_instance()
            if bot is None:
                return {"success": False, "error": "机器人实例未初始化，请确保机器人已启动"}

            success = await plugin_manager.load_plugin_from_directory(bot, plugin_name)
            return {"success": success}
        except Exception as e:
            logger.error(f"启用插件失败: {str(e)}")
            return {"success": False, "error": str(e)}

    # API: 禁用插件
    @app.post("/api/plugins/{plugin_name}/disable", response_class=JSONResponse)
    async def api_disable_plugin(plugin_name: str, request: Request, username: str = Depends(require_auth)):
        # 检查认证状态
        try:
            from utils.plugin_manager import plugin_manager

            # 调用 unload_plugin 方法并设置 add_to_excluded 参数为 True
            # 这样会将插件添加到禁用列表中并保存到配置文件
            success = await plugin_manager.unload_plugin(plugin_name, add_to_excluded=True)
            return {"success": success}
        except Exception as e:
            logger.error(f"禁用插件失败: {str(e)}")
            return {"success": False, "error": str(e)}

    # API: 删除插件
    @app.post("/api/plugins/{plugin_name}/delete", response_class=JSONResponse)
    async def api_delete_plugin(plugin_name: str, request: Request, username: str = Depends(require_auth)):
        # 检查认证状态
        try:
            from utils.plugin_manager import plugin_manager
            import shutil
            import os

            # 首先确保插件已经被卸载
            if plugin_name in plugin_manager.plugins:
                await plugin_manager.unload_plugin(plugin_name)

            # 查找插件目录
            plugin_dir = None
            for dirname in os.listdir("plugins"):
                if os.path.isdir(f"plugins/{dirname}") and os.path.exists(f"plugins/{dirname}/main.py"):
                    try:
                        # 检查目录中的main.py是否包含该插件类
                        with open(f"plugins/{dirname}/main.py", "r", encoding="utf-8") as f:
                            content = f.read()
                            if f"class {plugin_name}(" in content:
                                plugin_dir = f"plugins/{dirname}"
                                break
                    except Exception as e:
                        logger.error(f"检查插件目录时出错: {str(e)}")

            if not plugin_dir:
                return {"success": False, "error": f"找不到插件 {plugin_name} 的目录"}

            # 防止删除核心插件
            if plugin_name == "ManagePlugin":
                return {"success": False, "error": "不能删除核心插件 ManagePlugin"}

            # 删除插件目录
            shutil.rmtree(plugin_dir)

            # 从插件信息中移除
            if plugin_name in plugin_manager.plugin_info:
                del plugin_manager.plugin_info[plugin_name]

            return {"success": True, "message": f"插件 {plugin_name} 已成功删除"}
        except Exception as e:
            logger.error(f"删除插件失败: {str(e)}")
            return {"success": False, "error": str(e)}

    # API: 删除适配器
    @app.post("/api/adapters/{adapter_name}/delete", response_class=JSONResponse)
    async def api_delete_adapter(adapter_name: str, request: Request, username: str = Depends(require_auth)):
        # 检查认证状态
        try:
            import shutil
            import os

            # 检查适配器目录是否存在
            adapter_dir = os.path.join("adapter", adapter_name)
            if not os.path.exists(adapter_dir) or not os.path.isdir(adapter_dir):
                return {"success": False, "error": f"找不到适配器 {adapter_name} 的目录"}

            # 删除适配器目录
            shutil.rmtree(adapter_dir)
            logger.info(f"适配器 {adapter_name} 已成功删除")

            return {"success": True, "message": f"适配器 {adapter_name} 已成功删除"}
        except Exception as e:
            logger.error(f"删除适配器失败: {str(e)}")
            return {"success": False, "error": str(e)}

    # API: 获取适配器配置
    @app.get("/api/adapters/{adapter_name}/config", response_class=JSONResponse)
    async def api_get_adapter_config(adapter_name: str, request: Request, username: str = Depends(require_auth)):
        # 检查认证状态
        try:
            import os

            # 检查适配器目录是否存在
            adapter_dir = os.path.join("adapter", adapter_name)
            if not os.path.exists(adapter_dir) or not os.path.isdir(adapter_dir):
                return {"success": False, "error": f"找不到适配器 {adapter_name}"}

            # 读取配置文件
            config_path = os.path.join(adapter_dir, "config.toml")
            if not os.path.exists(config_path):
                return {"success": False, "error": f"适配器 {adapter_name} 没有配置文件"}

            with open(config_path, "r", encoding="utf-8") as f:
                config_content = f.read()

            return {"success": True, "config": config_content}
        except Exception as e:
            logger.error(f"获取适配器配置失败: {str(e)}")
            return {"success": False, "error": str(e)}

    # API: 保存适配器配置
    @app.post("/api/adapters/{adapter_name}/config", response_class=JSONResponse)
    async def api_save_adapter_config(adapter_name: str, request: Request, username: str = Depends(require_auth)):
        # 检查认证状态
        try:
            import os

            # 获取请求数据
            data = await request.json()
            config_content = data.get('config')

            if not config_content:
                return {"success": False, "error": "配置内容不能为空"}

            # 检查适配器目录是否存在
            adapter_dir = os.path.join("adapter", adapter_name)
            if not os.path.exists(adapter_dir) or not os.path.isdir(adapter_dir):
                return {"success": False, "error": f"找不到适配器 {adapter_name}"}

            # 备份原配置文件
            config_path = os.path.join(adapter_dir, "config.toml")
            if os.path.exists(config_path):
                backup_path = f"{config_path}.bak"
                try:
                    with open(config_path, "r", encoding="utf-8") as src:
                        with open(backup_path, "w", encoding="utf-8") as dst:
                            dst.write(src.read())
                    logger.info(f"已备份适配器配置文件到 {backup_path}")
                except Exception as e:
                    logger.warning(f"备份适配器配置文件失败: {str(e)}")

            # 保存新配置文件
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(config_content)

            logger.info(f"适配器 {adapter_name} 配置文件已保存")
            return {"success": True, "message": "配置已保存"}
        except Exception as e:
            logger.error(f"保存适配器配置失败: {str(e)}")
            return {"success": False, "error": str(e)}

    # API: 删除插件（备用路由）
    @app.post("/api/plugin/delete", response_class=JSONResponse)
    async def api_delete_plugin_alt(request: Request, username: str = Depends(require_auth)):
        # 检查认证状态
        try:
            # 获取请求数据
            data = await request.json()
            plugin_name = data.get('plugin_id')

            if not plugin_name:
                return {"success": False, "error": "缺少插件ID参数"}

            from utils.plugin_manager import plugin_manager
            import shutil
            import os

            # 首先确保插件已经被卸载
            if plugin_name in plugin_manager.plugins:
                await plugin_manager.unload_plugin(plugin_name)

            # 查找插件目录
            plugin_dir = None
            for dirname in os.listdir("plugins"):
                if os.path.isdir(f"plugins/{dirname}") and os.path.exists(f"plugins/{dirname}/main.py"):
                    try:
                        # 检查目录中的main.py是否包含该插件类
                        with open(f"plugins/{dirname}/main.py", "r", encoding="utf-8") as f:
                            content = f.read()
                            if f"class {plugin_name}(" in content:
                                plugin_dir = f"plugins/{dirname}"
                                break
                    except Exception as e:
                        logger.error(f"检查插件目录时出错: {str(e)}")

            if not plugin_dir:
                return {"success": False, "error": f"找不到插件 {plugin_name} 的目录"}

            # 防止删除核心插件
            if plugin_name == "ManagePlugin":
                return {"success": False, "error": "不能删除核心插件 ManagePlugin"}

            # 删除插件目录
            shutil.rmtree(plugin_dir)

            # 从插件信息中移除
            if plugin_name in plugin_manager.plugin_info:
                del plugin_manager.plugin_info[plugin_name]

            return {"success": True, "message": f"插件 {plugin_name} 已成功删除"}
        except Exception as e:
            logger.error(f"删除插件失败: {str(e)}")
            return {"success": False, "error": str(e)}



    # 辅助函数: 查找插件配置路径
    def find_plugin_config_path(plugin_id: str):
        """查找插件配置文件路径，尝试多个可能的位置"""
        # 首先尝试直接使用插件ID作为目录名
        possible_paths = [
            os.path.join("plugins", plugin_id, "config.toml"),  # 原始路径
            os.path.join("_data", "plugins", plugin_id, "config.toml"),  # _data目录下的路径
            os.path.join("..", "plugins", plugin_id, "config.toml"),  # 相对上级目录
            os.path.abspath(os.path.join("plugins", plugin_id, "config.toml")),  # 绝对路径
            os.path.join(os.path.dirname(os.path.dirname(current_dir)), "plugins", plugin_id, "config.toml")  # 项目根目录
        ]

        # 如果没有找到，尝试遍历所有插件目录查找匹配的插件类
        plugin_dirs = []
        for dirname in os.listdir("plugins"):
            if os.path.isdir(f"plugins/{dirname}") and os.path.exists(f"plugins/{dirname}/main.py"):
                try:
                    # 检查目录中的main.py是否包含该插件类
                    with open(f"plugins/{dirname}/main.py", "r", encoding="utf-8") as f:
                        content = f.read()
                        if f"class {plugin_id}(" in content:
                            plugin_dirs.append(dirname)
                except Exception as e:
                    logger.error(f"检查插件目录时出错: {str(e)}")

        # 将找到的目录添加到可能路径中
        for dirname in plugin_dirs:
            possible_paths.append(os.path.join("plugins", dirname, "config.toml"))

        # 检查环境变量定义的数据目录
        data_dir_env = os.environ.get('XYBOT_DATA_DIR')
        if data_dir_env:
            possible_paths.append(os.path.join(data_dir_env, "plugins", plugin_id, "config.toml"))

        # 检查Docker环境特定路径
        if os.path.exists('/.dockerenv') or os.path.exists('/run/.containerenv'):
            docker_paths = [
                os.path.join("/app/data/plugins", plugin_id, "config.toml"),
                os.path.join("/data/plugins", plugin_id, "config.toml"),
                os.path.join("/usr/local/xybot/plugins", plugin_id, "config.toml")
            ]
            possible_paths.extend(docker_paths)

        # 查找第一个存在的路径
        for path in possible_paths:
            if os.path.exists(path):
                logger.debug(f"找到插件配置文件: {path}")
                return path

        # 如果没有找到存在的文件，返回默认路径
        # 如果有找到插件目录，使用第一个找到的目录
        if plugin_dirs:
            return os.path.join("plugins", plugin_dirs[0], "config.toml")

        # 否则使用插件ID作为目录名
        return os.path.join("plugins", plugin_id, "config.toml")

    # API: 获取插件配置
    @app.get("/api/plugin_config", response_class=JSONResponse)
    async def api_get_plugin_config(plugin_id: str, request: Request, username: str = Depends(require_auth)):
        # 检查认证状态
        try:
            import tomllib

            # 查找配置文件路径
            config_path = find_plugin_config_path(plugin_id)
            if not config_path:
                return {"success": False, "message": f"插件 {plugin_id} 的配置文件不存在"}

            # 读取配置
            with open(config_path, "rb") as f:
                config_content = tomllib.load(f)

            return {
                "success": True,
                "config": config_content
            }
        except Exception as e:
            logger.error(f"获取插件配置失败: {str(e)}")
            return {"success": False, "error": str(e)}

    # API: 获取插件配置文件路径
    @app.get("/api/plugin_config_file", response_class=JSONResponse)
    async def api_get_plugin_config_file(plugin_id: str, request: Request, username: str = Depends(require_auth)):
        # 检查认证状态
        try:
            # 查找配置文件路径
            config_path = find_plugin_config_path(plugin_id)
            if not config_path:
                # 如果配置文件不存在，返回默认位置
                # 如插件尚未创建配置文件，返回它应该创建的位置
                config_path = os.path.join("plugins", plugin_id, "config.toml")

            # 检查文件是否存在，如果不存在则创建一个空的配置文件
            if not os.path.exists(config_path):
                try:
                    # 确保目录存在
                    os.makedirs(os.path.dirname(config_path), exist_ok=True)
                    # 创建空的配置文件
                    with open(config_path, 'w', encoding='utf-8') as f:
                        f.write("# 插件配置文件\n\n[basic]\n# 是否启用插件\nenable = true\n")
                    logger.info(f"创建了新的插件配置文件: {config_path}")
                except Exception as e:
                    logger.error(f"创建插件配置文件失败: {str(e)}")

            # 转换为相对路径，以便在文件管理器中打开
            relative_path = os.path.normpath(config_path)

            return {
                "success": True,
                "config_file": relative_path
            }
        except Exception as e:
            logger.error(f"获取插件配置文件路径失败: {str(e)}")
            return {"success": False, "error": str(e)}

    # API: 获取插件README.md内容
    @app.get("/api/plugin_readme", response_class=JSONResponse)
    async def api_get_plugin_readme(plugin_id: str, request: Request, username: str = Depends(require_auth)):
        # 检查认证状态
        try:
            # 查找插件目录
            plugin_dir = None
            readme_path = None

            # 首先检查是否有与插件名相同的目录
            if os.path.isdir(f"plugins/{plugin_id}") and os.path.exists(f"plugins/{plugin_id}/README.md"):
                plugin_dir = plugin_id
                readme_path = f"plugins/{plugin_id}/README.md"
            else:
                # 遍历所有插件目录
                for dirname in os.listdir("plugins"):
                    if os.path.isdir(f"plugins/{dirname}"):
                        # 检查目录中是否有与插件同名的类
                        if os.path.exists(f"plugins/{dirname}/main.py"):
                            try:
                                # 先检查文件内容是否包含插件类名
                                with open(f"plugins/{dirname}/main.py", "r", encoding="utf-8") as f:
                                    content = f.read()
                                    if f"class {plugin_id}" in content:
                                        plugin_dir = dirname
                                        readme_path = f"plugins/{dirname}/README.md"
                                        if os.path.exists(readme_path):
                                            break

                                # 如果没找到，再尝试加载模块检查
                                if not plugin_dir:
                                    module = importlib.import_module(f"plugins.{dirname}.main")
                                    for name, obj in inspect.getmembers(module):
                                        if (inspect.isclass(obj) and
                                            issubclass(obj, PluginBase) and
                                            obj != PluginBase and
                                            obj.__name__ == plugin_id):
                                            # 找到了插件目录，检查README.md
                                            plugin_dir = dirname
                                            readme_path = f"plugins/{dirname}/README.md"
                                            break
                            except Exception as e:
                                logger.error(f"检查插件{plugin_id}的README.md时出错: {str(e)}")

            if not plugin_dir:
                return {"success": False, "message": f"找不到插件 {plugin_id} 的目录"}

            if not readme_path or not os.path.exists(readme_path):
                return {"success": False, "message": f"插件 {plugin_id} 的README.md文件不存在"}

            # 读取README.md内容
            with open(readme_path, "r", encoding="utf-8") as f:
                readme_content = f.read()

            return {
                "success": True,
                "readme": readme_content,
                "plugin_id": plugin_id,
                "plugin_dir": plugin_dir
            }
        except Exception as e:
            logger.error(f"获取插件README.md失败: {str(e)}")
            return {"success": False, "error": str(e)}

    # API: 保存插件配置
    @app.post("/api/save_plugin_config", response_class=JSONResponse)
    async def api_save_plugin_config(request: Request, username: str = Depends(require_auth)):
        # 检查认证状态
        try:
            # 获取请求数据
            data = await request.json()
            plugin_id = data.get('plugin_id')
            config = data.get('config')

            if not plugin_id or not config:
                return {"success": False, "message": "缺少必要参数"}

            # 找到配置文件路径
            config_path = find_plugin_config_path(plugin_id)
            if not config_path:
                # 如果配置文件不存在，创建默认位置
                config_path = os.path.join("plugins", plugin_id, "config.toml")
                os.makedirs(os.path.dirname(config_path), exist_ok=True)

            # 生成TOML内容
            toml_content = ""
            for section, values in config.items():
                toml_content += f"[{section}]\n"
                for key, value in values.items():
                    if isinstance(value, str):
                        toml_content += f'{key} = "{value}"\n'
                    elif isinstance(value, bool):
                        toml_content += f"{key} = {str(value).lower()}\n"
                    else:
                        toml_content += f"{key} = {value}\n"
                toml_content += "\n"

            # 保存配置
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(toml_content)

            return {"success": True, "message": "配置已保存"}
        except Exception as e:
            logger.error(f"保存插件配置失败: {str(e)}")
            return {"success": False, "error": str(e)}

    @app.get("/api/plugin_market/categories", response_class=JSONResponse)
    async def api_get_plugin_categories(request: Request, username: str = Depends(require_auth)):
        # 检查认证状态
        try:
            # 使用 aiohttp 客户端（与文件其他部分保持一致）
            async with aiohttp.ClientSession() as session:
                # 构建请求头
                headers = {
                    "X-Client-ID": get_client_id(),
                    "X-Bot-Version": get_bot_version(),
                    "User-Agent": f"XYBot/{get_bot_version()}"
                }

                try:
                    # 请求远程API获取分类列表
                    async with session.get(
                        f"{PLUGIN_MARKET_API['BASE_URL']}/categories",
                        headers=headers,
                        timeout=10,
                        allow_redirects=True
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            logger.info(f"成功获取插件分类列表，共 {len(data.get('categories', []))} 个分类")
                            return data
                        else:
                            logger.warning(f"获取分类列表失败，状态码: {response.status}")
                            # 返回默认分类
                            return await get_default_categories()

                except aiohttp.ClientError as e:
                    logger.error(f"请求分类列表失败: {str(e)}")
                    # 返回默认分类
                    return await get_default_categories()

        except Exception as e:
            logger.error(f"获取插件分类失败: {str(e)}")
            # 返回默认分类
            return await get_default_categories()

    # 获取默认分类列表（当远程API不可用时使用）
    async def get_default_categories():
        return {
            "success": True,
            "categories": [
                {
                    "id": 1,
                    "value": "all",
                    "label": "全部",
                    "icon": "bi-grid-3x3-gap-fill",
                    "description": "所有插件",
                    "sort_order": 0,
                    "is_active": True
                },
                {
                    "id": 2,
                    "value": "tools",
                    "label": "工具",
                    "icon": "bi-tools",
                    "description": "工具类插件",
                    "sort_order": 1,
                    "is_active": True
                },
                {
                    "id": 3,
                    "value": "ai",
                    "label": "AI",
                    "icon": "bi-cpu",
                    "description": "AI 相关插件",
                    "sort_order": 2,
                    "is_active": True
                },
                {
                    "id": 4,
                    "value": "entertainment",
                    "label": "娱乐",
                    "icon": "bi-controller",
                    "description": "娱乐类插件",
                    "sort_order": 3,
                    "is_active": True
                },
                {
                    "id": 5,
                    "value": "adapter",
                    "label": "适配器",
                    "icon": "bi-plug",
                    "description": "适配器插件",
                    "sort_order": 4,
                    "is_active": True
                },
                {
                    "id": 6,
                    "value": "other",
                    "label": "其他",
                    "icon": "bi-three-dots",
                    "description": "其他类型插件",
                    "sort_order": 5,
                    "is_active": True
                }
            ]
        }

    # API: 提交插件到市场
    @app.get("/api/plugin_market", response_class=JSONResponse)
    async def api_get_plugin_market(request: Request, username: str = Depends(require_auth)):
        # 检查认证状态
        try:
            # 从远程服务器获取插件市场数据
            async with aiohttp.ClientSession() as session:
                try:
                    # 设置超时时间防止长时间等待
                    async with session.get(
                        f"{PLUGIN_MARKET_API['BASE_URL']}{PLUGIN_MARKET_API['LIST']}",
                        timeout=10
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            plugins = data.get('plugins', [])
                            return {"success": True, "plugins": plugins}
                        else:
                            error_text = await response.text()
                            return {"success": False, "error": f"远程服务器返回错误: {response.status} - {error_text}"}
                except aiohttp.ClientError as e:
                    logger.error(f"连接远程插件市场失败: {e}")
                    # 尝试从本地缓存获取
                    cache_path = os.path.join(current_dir, 'plugin_market_cache.json')
                    if os.path.exists(cache_path):
                        with open(cache_path, 'r', encoding='utf-8') as f:
                            cache_data = json.load(f)
                            return {"success": True, "plugins": cache_data.get('plugins', []), "cached": True}
                    else:
                        return {"success": False, "error": f"无法连接到远程服务器: {str(e)}"}
        except Exception as e:
            logger.error(f"获取插件市场失败: {str(e)}")
            return {"success": False, "error": str(e)}

    # API: 获取插件市场列表 (新路径)
    @app.get("/api/plugin_market/list", response_class=JSONResponse)
    async def api_get_plugin_market_list(request: Request, username: str = Depends(require_auth)):
        # 检查认证状态
        try:
            # 从远程服务器获取插件市场数据
            async with aiohttp.ClientSession() as session:
                try:
                    # 设置超时时间防止长时间等待
                    async with session.get(
                        f"{PLUGIN_MARKET_API['BASE_URL']}{PLUGIN_MARKET_API['LIST']}",
                        timeout=10
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            plugins = data.get('plugins', [])
                            return {"success": True, "plugins": plugins}
                        else:
                            error_text = await response.text()
                            return {"success": False, "error": f"远程服务器返回错误: {response.status} - {error_text}"}
                except aiohttp.ClientError as e:
                    logger.error(f"连接远程插件市场失败: {e}")
                    # 尝试从本地缓存获取
                    cache_path = os.path.join(current_dir, 'plugin_market_cache.json')
                    if os.path.exists(cache_path):
                        with open(cache_path, 'r', encoding='utf-8') as f:
                            cache_data = json.load(f)
                            return {"success": True, "plugins": cache_data.get('plugins', []), "cached": True}
                    else:
                        # 如果没有缓存，返回一些示例插件数据
                        sample_plugins = [
                            {
                                "id": "sample1",
                                "name": "DifyConversationManager",
                                "description": "dify会话管理器，集成Dify接口对话，可以进行对话管理",
                                "author": "全部的运营",
                                "version": "1.2.0",
                                "github_url": "https://github.com/sxkiss/allbot",
                                "tags": ["AI", "对话"],
                                "category": "ai",
                                "update_time": datetime.now().isoformat()
                            },
                            {
                                "id": "sample2",
                                "name": "AutoSummary",
                                "description": "快速总结文本内容的插件，让你的文章一键生成摘要",
                                "author": "全部的运营",
                                "version": "1.3.0",
                                "github_url": "https://github.com/sxkiss/allbot",
                                "tags": ["AI", "工具"],
                                "category": "ai",
                                "update_time": datetime.now().isoformat()
                            },
                            {
                                "id": "sample3",
                                "name": "ChatSummary",
                                "description": "聊天记录总结工具，自动分析对话内容，提取关键信息",
                                "author": "全部的运营",
                                "version": "1.1.9",
                                "github_url": "https://github.com/sxkiss/allbot",
                                "tags": ["AI", "聊天"],
                                "category": "ai",
                                "update_time": datetime.now().isoformat()
                            }
                        ]
                        return {"success": True, "plugins": sample_plugins, "sample": True}
        except Exception as e:
            logger.error(f"获取插件市场失败: {str(e)}")
            return {"success": False, "error": str(e)}

    # API: 提交插件到市场
    @app.post("/api/plugin_market/submit", response_class=JSONResponse)
    async def api_submit_plugin(request: Request, username: str = Depends(require_auth)):
        # 检查认证状态
        try:
            # 从请求中获取JSON数据
            data = await request.json()

            # 准备提交数据
            plugin_data = {
                "name": data.get("name"),
                "description": data.get("description"),
                "author": data.get("author"),
                "version": data.get("version"),
                "github_url": data.get("github_url"),
                "tags": data.get("tags", []),
                "requirements": data.get("requirements", []),
                "submitted_by": username,  # 记录提交者
                "submitted_at": datetime.now().isoformat(),  # 记录提交时间
                "status": "pending"  # 状态：pending, approved, rejected
            }

            # 处理图标（如果有）
            if "icon" in data and data["icon"]:
                plugin_data["icon"] = data["icon"]

            # 发送到远程服务器
            async with aiohttp.ClientSession() as session:
                try:
                    # 将数据发送到远程服务器进行审核
                    async with session.post(
                        f"{PLUGIN_MARKET_API['BASE_URL']}{PLUGIN_MARKET_API['SUBMIT']}",
                        json=plugin_data,
                        timeout=30
                    ) as response:
                        if response.status == 200:
                            resp_data = await response.json()
                            return {"success": True, "message": "插件提交成功，等待审核", "id": resp_data.get("id")}
                        else:
                            error_text = await response.text()
                            return {"success": False, "error": f"远程服务器返回错误: {response.status} - {error_text}"}
                except aiohttp.ClientError as e:
                    logger.error(f"提交插件到远程服务器失败: {e}")

                    # 保存到本地临时文件，稍后重试
                    temp_dir = os.path.join(current_dir, 'pending_plugins')
                    os.makedirs(temp_dir, exist_ok=True)
                    safe_name = (plugin_data.get('name') or 'plugin').replace(' ', '_')
                    temp_file = os.path.join(temp_dir, f"{int(time.time())}_{safe_name}.json")

                    with open(temp_file, 'w', encoding='utf-8') as f:
                        json.dump(plugin_data, f, ensure_ascii=False, indent=2)

                    return {
                        "success": True,
                        "message": "由于网络问题，插件已暂存在本地，将在网络恢复后自动提交",
                        "local_only": True
                    }
        except Exception as e:
            logger.error(f"提交插件失败: {str(e)}\n{traceback.format_exc()}")
            return {"success": False, "error": str(e)}

    # API: 安装插件
    @app.post("/api/plugin_market/install", response_class=JSONResponse)
    async def api_install_plugin_from_market(request: Request, username: str = Depends(require_auth)):
        """从插件市场安装插件"""
        try:
            from admin.services import PluginInstaller

            # 获取请求数据
            data = await request.json()
            plugin_data = data.get('plugin_data', {})
            plugin_name = plugin_data.get('name')
            github_url = plugin_data.get('github_url')

            if not plugin_name or not github_url:
                return {"success": False, "error": "缺少必要参数"}

            # 使用 PluginInstaller 服务
            installer = PluginInstaller()
            result = installer.install_plugin(
                plugin_name=plugin_name,
                github_url=github_url,
                install_dependencies=True
            )

            # 如果安装成功，尝试自动加载插件
            if result.get("success"):
                try:
                    from utils.plugin_manager import plugin_manager
                    bot_instance = getattr(app.state, 'bot_instance', None)
                    if bot_instance:
                        await plugin_manager.load_plugin_from_directory(bot_instance, plugin_name)
                except Exception as e:
                    logger.warning(f"自动加载插件失败，用户需要手动启用: {str(e)}")

            return result

        except Exception as e:
            logger.error(f"安装插件失败: {str(e)}")
            return {"success": False, "error": str(e)}

    # 周期性任务：同步本地待审核插件到服务器
    async def sync_pending_plugins():
        """检查本地待审核插件并尝试同步到服务器"""
        try:
            temp_dir = os.path.join(current_dir, 'pending_plugins')
            if not os.path.exists(temp_dir):
                return

            for filename in os.listdir(temp_dir):
                if not filename.endswith('.json'):
                    continue

                file_path = os.path.join(temp_dir, filename)
                with open(file_path, 'r', encoding='utf-8') as f:
                    plugin_data = json.load(f)

                # 尝试发送到服务器
                async with aiohttp.ClientSession() as session:
                    try:
                        # 使用插件市场API配置
                        url = f"{PLUGIN_MARKET_API['BASE_URL']}{PLUGIN_MARKET_API['SUBMIT']}"
                        logger.info(f"正在同步插件到服务器: {url}")

                        async with session.post(
                            url,
                            json=plugin_data,
                            timeout=10,
                            ssl=False,  # 明确指定不使用SSL
                            allow_redirects=True  # 允许重定向
                        ) as response:
                            if response.status == 200:
                                # 删除本地文件
                                os.remove(file_path)
                                logger.info(f"成功同步插件到服务器: {plugin_data.get('name')}")
                    except Exception as e:
                        logger.error(f"同步插件到服务器失败: {e}")
                        continue  # 跳过当前文件，稍后重试
        except Exception as e:
            logger.error(f"同步待审核插件失败: {str(e)}")

    # 周期性任务：缓存插件市场数据
    async def cache_plugin_market():
        """从远程服务器缓存插件市场数据到本地"""
        try:
            async with aiohttp.ClientSession() as session:
                try:
                    # 使用插件市场API配置，使用正确的URL格式
                    url = f"{PLUGIN_MARKET_API['BASE_URL']}{PLUGIN_MARKET_API['LIST']}"
                    # 添加尾部斜杠以避免重定向（如果没有查询参数）
                    if not url.endswith('/') and '?' not in url:
                        url += '/'
                    logger.info(f"正在缓存插件市场数据: {url}")

                    async with session.get(url, timeout=10, ssl=False, allow_redirects=True) as response:
                        if response.status == 200:
                            data = await response.json()

                            # 确保缓存目录存在
                            cache_dir = os.path.dirname(os.path.join(current_dir, 'plugin_market_cache.json'))
                            os.makedirs(cache_dir, exist_ok=True)

                            # 保存到本地缓存
                            cache_path = os.path.join(current_dir, 'plugin_market_cache.json')
                            with open(cache_path, 'w', encoding='utf-8') as f:
                                json.dump(data, f, ensure_ascii=False, indent=2)

                            logger.info(f"成功缓存插件市场数据，共{len(data.get('plugins', []))}个插件")
                except Exception as e:
                    logger.error(f"缓存插件市场数据失败: {e}")
        except Exception as e:
            logger.error(f"缓存插件市场任务失败: {str(e)}")

    # 缓存插件市场数据
    @app.on_event("startup")
    async def startup_cache_plugin_market():
        # 应用启动时缓存一次插件市场数据
        asyncio.create_task(cache_plugin_market())

        # 设置定时任务每小时更新一次缓存
        async def periodic_cache():
            while True:
                await asyncio.sleep(3600)  # 每小时执行一次
                await cache_plugin_market()

        # 设置定时任务每10分钟同步一次待审核插件
        async def periodic_sync():
            while True:
                await asyncio.sleep(600)  # 每10分钟执行一次
                await sync_pending_plugins()

        # 启动定时任务
        asyncio.create_task(periodic_cache())
        asyncio.create_task(periodic_sync())

    # 插件市场API配置
    # 说明：用于插件安装/详情等场景的上游地址；与前面的同名配置保持一致。
    PLUGIN_MARKET_API = {
        "BASE_URL": os.environ.get("PLUGIN_MARKET_BASE_URL", "http://v.sxkiss.top"),
        "LIST": "/plugins/?status=approved",  # 添加尾部斜杠，避免重定向
        "DETAIL": "/plugins/",
        "INSTALL": "/plugins/install/",
        "CACHE_DIR": os.path.join(current_dir, "cache"),
    }

    # 辅助函数：获取客户端ID
    def get_client_id():
        """获取或生成唯一的客户端ID"""
        cache_dir = PLUGIN_MARKET_API["CACHE_DIR"]
        os.makedirs(cache_dir, exist_ok=True)
        client_id_file = os.path.join(cache_dir, "client_id")

        # 如果文件存在，读取ID
        if os.path.exists(client_id_file):
            try:
                with open(client_id_file, "r") as f:
                    return f.read().strip()
            except Exception as e:
                logger.warning(f"读取客户端ID失败: {e}")

        # 生成新ID
        import uuid
        client_id = str(uuid.uuid4())

        # 保存到文件
        try:
            with open(client_id_file, "w") as f:
                f.write(client_id)
        except Exception as e:
            logger.warning(f"保存客户端ID失败: {e}")

        return client_id

    # 辅助函数：获取Bot版本
    def get_bot_version():
        """获取Bot版本信息"""
        try:
            # 尝试从配置文件读取版本
            config_file = os.path.join(os.path.dirname(current_dir), "main_config.toml")
            if os.path.exists(config_file):
                try:
                    import tomllib
                except ImportError:
                    import tomli as tomllib
                with open(config_file, "rb") as f:
                    config = tomllib.load(f)
                    return config.get("version", "1.0.0")
        except Exception as e:
            logger.debug(f"读取版本信息失败: {e}")
        return "1.0.0"

    # API: 安装插件
    @app.post("/api/plugins/install", response_class=JSONResponse)
    async def api_install_plugin_direct(request: Request, username: str = Depends(require_auth)):
        """直接安装插件（通过 GitHub URL）"""
        try:
            from admin.services import PluginInstaller

            # 获取请求数据
            data = await request.json()
            plugin_name = data.get('name')
            github_url = data.get('github_url')

            if not plugin_name or not github_url:
                return {"success": False, "error": "缺少必要参数"}

            # 使用 PluginInstaller 服务
            installer = PluginInstaller()
            result = installer.install_plugin(
                plugin_name=plugin_name,
                github_url=github_url,
                install_dependencies=True
            )

            # 如果安装成功，尝试自动加载插件
            if result.get("success"):
                try:
                    from utils.plugin_manager import plugin_manager
                    bot_instance = getattr(app.state, 'bot_instance', None)
                    if bot_instance:
                        success = await plugin_manager.load_plugin_from_directory(bot_instance, plugin_name)
                        if not success:
                            result["message"] = f"插件 {plugin_name} 安装成功，但加载失败"
                except Exception as e:
                    logger.warning(f"自动加载插件失败: {str(e)}")
                    result["message"] = f"插件 {plugin_name} 安装成功，但自动加载失败"

            return result

        except Exception as e:
            logger.error(f"安装插件失败: {str(e)}")
            return {"success": False, "error": str(e)}

    # 注释掉重复的API端点定义
    #@app.get('/api/system/info')
    #async def system_info_api(request: Request, username: str = Depends(require_auth)):
    #    """系统信息API"""
    #    try:
    #        info = get_system_info()
    #        return JSONResponse(content={
    #            "success": True,
    #            "data": info,
    #            "error": None
    #        })
    #    except Exception as e:
    #        logger.error(f"获取系统信息API失败: {str(e)}")
    #        return JSONResponse(content={
    #            "success": False,
    #            "data": {
    #                "hostname": "unknown",
    #                "platform": "unknown",
    #                "python_version": "unknown",
    #                "cpu_count": 0,
    #                "memory_total": 0,
    #                "memory_available": 0,
    #                "disk_total": 0,
    #                "disk_free": 0,
    #                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    #            },
    #            "error": str(e)
    #        })

    def check_auth(request: Request):
        """检查用户认证状态"""
        try:
            token = request.headers.get('Authorization')
            if not token:
                # 尝试从cookie中获取token
                token = request.cookies.get('token')

            if not token:
                raise HTTPException(status_code=401, detail="未登录或登录已过期")

            # 这里可以添加token验证的逻辑
            # 例如验证token的有效性，检查是否过期等
            # 如果验证失败，抛出HTTPException(status_code=401)

            return True
        except Exception as e:
            logger.error(f"认证检查失败: {str(e)}")
            raise HTTPException(status_code=401, detail="认证失败")

    from fastapi import WebSocket
    from utils.plugin_manager import plugin_manager

    # WebSocket连接管理
    class ConnectionManager:
        def __init__(self):
            self.active_connections: List[WebSocket] = []

        async def connect(self, websocket: WebSocket, username: str = Depends(require_auth)):
            await websocket.accept()
            self.active_connections.append(websocket)

        def disconnect(self, websocket: WebSocket):
            self.active_connections.remove(websocket)

        async def send_message(self, message: str, websocket: WebSocket, username: str = Depends(require_auth)):
            await websocket.send_text(message)

    manager = ConnectionManager()

    @app.websocket("/ws/plugins")
    async def websocket_endpoint(websocket: WebSocket, username: str = Depends(require_auth)):
        await manager.connect(websocket)
        try:
            while True:
                data = await websocket.receive_json()
                if data["action"] == "install_plugin":
                    plugin_data = data["data"]
                    try:
                        # 获取DependencyManager插件实例
                        dependency_manager = None
                        for plugin in plugin_manager.plugins:
                            if plugin.__class__.__name__ == "DependencyManager":
                                dependency_manager = plugin
                                break

                        if not dependency_manager:
                            await websocket.send_json({
                                "type": "install_complete",
                                "success": False,
                                "error": "DependencyManager插件未安装"
                            })
                            continue

                        # 发送进度消息
                        await websocket.send_json({
                            "type": "install_progress",
                            "message": "开始安装插件..."
                        })

                        # 使用DependencyManager的安装方法
                        await dependency_manager._handle_github_install(
                            bot_instance,
                            "admin",  # 使用admin作为chat_id
                            plugin_data["github_url"]
                        )

                        # 发送完成消息
                        await websocket.send_json({
                            "type": "install_complete",
                            "success": True
                        })

                    except Exception as e:
                        logger.error(f"安装插件失败: {str(e)}")
                        await websocket.send_json({
                            "type": "install_complete",
                            "success": False,
                            "error": str(e)
                        })
        except WebSocketDisconnect:
            manager.disconnect(websocket)
