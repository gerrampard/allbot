"""
AllBot 管理后台 - 核心应用设置模块

职责：
- FastAPI 应用实例创建与配置
- 全局配置管理
- Bot 实例管理
- 认证与授权
- 中间件注册
- 静态文件与模板引擎设置
"""
import os
import sys
import json
import time
from datetime import datetime
from typing import Optional, List, Any
from pathlib import Path
from loguru import logger

# 导入 tomllib 或 tomli
try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # Python 3.10 及以下版本
    except ImportError:
        class TomliNotAvailable:
            @staticmethod
            def load(f):
                raise ImportError("tomllib 或 tomli 库不可用，请安装 tomli 库: pip install tomli")
        tomllib = TomliNotAvailable()

from fastapi import FastAPI, Request, HTTPException, WebSocket
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from itsdangerous import URLSafeSerializer

# 当前目录
current_dir = os.path.dirname(os.path.abspath(__file__))
admin_dir = os.path.dirname(current_dir)

# API 标签分组定义
tags_metadata = [
    {"name": "系统", "description": "系统监控、状态、日志等相关接口"},
    {"name": "插件", "description": "插件管理、配置、启用/禁用等操作"},
    {"name": "账号", "description": "微信账号管理、切换、登录/登出"},
    {"name": "文件", "description": "文件上传、下载、删除、列表查看"},
    {"name": "联系人", "description": "好友、群组管理与查询"},
    {"name": "朋友圈", "description": "朋友圈列表、点赞、评论等操作"},
    {"name": "提醒", "description": "定时提醒的增删改查"},
    {"name": "适配器", "description": "多平台适配器管理（QQ/Telegram/Web/Windows）"},
    {"name": "AI平台", "description": "AI 模型平台配置与密钥管理"},
]

# 全局变量
app: FastAPI = None
templates: Jinja2Templates = None
security = HTTPBasic()
bot_instance = None
config = {
    "host": "0.0.0.0",
    "port": 8080,
    "username": "admin",
    "password": "admin123",
    "debug": False,
    "secret_key": "xybotv2_admin_secret_key",
    "max_history": 1000,
    "log_level": "INFO"
}

# WebSocket 连接管理
active_connections: List[WebSocket] = []

# 服务器状态
SERVER_RUNNING = False
SERVER_THREAD = None


def set_log_level(level: str):
    """设置日志级别"""
    handlers = logger._core.handlers
    for handler_id, handler in handlers.items():
        if hasattr(handler, "_sink") and handler._sink == sys.stderr:
            handler._level = logger.level(level).no
    logger.info(f"管理后台日志级别已设置为: {level}")


def set_bot_instance(bot):
    """设置 bot 实例供其他模块使用"""
    global bot_instance
    bot_instance = bot
    logger.info("管理后台已设置 bot 实例")
    return bot_instance


def get_bot_instance():
    """获取 bot 实例"""
    global bot_instance
    if bot_instance is None:
        logger.debug("bot 实例未设置（可能处于启动阶段）")
    return bot_instance


def load_config():
    """加载配置"""
    global config
    try:
        # 优先从 main_config.toml 读取配置
        main_config_path = os.path.join(os.path.dirname(admin_dir), "main_config.toml")
        if os.path.exists(main_config_path):
            with open(main_config_path, "rb") as f:
                main_config = tomllib.load(f)
                if "Admin" in main_config:
                    admin_config = main_config["Admin"]
                    for key in ["host", "port", "username", "password", "debug", "log_level"]:
                        if key in admin_config:
                            config[key] = admin_config[key]
                    if "log_level" in admin_config:
                        set_log_level(admin_config["log_level"])
                    logger.info(f"从 main_config.toml 加载管理后台配置: {main_config_path}")
        else:
            # 尝试从 config.json 加载
            config_path = os.path.join(admin_dir, "config.json")
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    loaded_config = json.load(f)
                    config.update(loaded_config)
                    logger.info(f"从 config.json 加载管理后台配置: {config_path}")
                    logger.warning("建议将配置迁移到 main_config.toml 中")

        # 环境变量优先级最高
        env_mappings = {
            "ADMIN_USERNAME": "username",
            "ADMIN_PASSWORD": "password",
            "ADMIN_HOST": "host",
            "ADMIN_PORT": "port",
            "ADMIN_DEBUG": "debug"
        }

        for env_key, config_key in env_mappings.items():
            if env_key in os.environ:
                if config_key == "port" and os.environ[env_key].isdigit():
                    config[config_key] = int(os.environ[env_key])
                elif config_key == "debug":
                    config[config_key] = os.environ[env_key].lower() in ("true", "1", "yes")
                else:
                    config[config_key] = os.environ[env_key]
                logger.info(f"从环境变量 {env_key} 加载配置")

    except Exception as e:
        logger.error(f"加载管理后台配置失败: {str(e)}")
        logger.warning("使用默认配置")


def verify_credentials(credentials: HTTPBasicCredentials):
    """验证用户凭据"""
    correct_username = config["username"]
    correct_password = config["password"]

    if (credentials.username != correct_username or
        credentials.password != correct_password):
        raise HTTPException(
            status_code=401,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


async def check_auth(request: Request) -> Optional[str]:
    """检查用户是否已认证"""
    try:
        session_cookie = request.cookies.get("session")
        if not session_cookie:
            logger.debug("未找到会话 Cookie")
            return None

        logger.debug(f"获取到会话 Cookie: {session_cookie[:15]}...")

        try:
            serializer = URLSafeSerializer(config["secret_key"], "session")
            session_data = serializer.loads(session_cookie)
            logger.debug(f"解析会话数据成功: {session_data}")

            expires = session_data.get("expires", 0)
            if expires < time.time():
                logger.debug(f"会话已过期: 当前时间 {time.time()}, 过期时间 {expires}")
                return None

            logger.debug(f"会话有效，用户: {session_data.get('username')}")
            return session_data.get("username")
        except Exception as e:
            logger.error(f"解析会话数据失败: {str(e)}")
            return None
    except Exception as e:
        logger.error(f"检查认证失败: {str(e)}")
        return None


# WebSocket 连接管理
async def connect_websocket(websocket: WebSocket):
    """连接 WebSocket"""
    await websocket.accept()
    active_connections.append(websocket)


async def disconnect_websocket(websocket: WebSocket):
    """断开 WebSocket"""
    if websocket in active_connections:
        active_connections.remove(websocket)


async def broadcast_message(message: str):
    """向所有 WebSocket 连接广播消息"""
    for connection in active_connections:
        try:
            await connection.send_text(message)
        except Exception as e:
            logger.error(f"广播消息失败: {str(e)}")
            await disconnect_websocket(connection)


def get_version_info():
    """获取版本信息"""
    try:
        version_file = os.path.join(os.path.dirname(admin_dir), "version.json")
        if os.path.exists(version_file):
            with open(version_file, "r", encoding="utf-8") as f:
                return json.load(f)
        else:
            logger.warning(f"版本文件不存在: {version_file}")
            version_info = {
                "version": "v1.0.0",
                "last_check": datetime.now().isoformat(),
                "update_available": False,
                "latest_version": "",
                "update_url": "",
                "update_description": ""
            }
            with open(version_file, "w", encoding="utf-8") as f:
                json.dump(version_info, f, ensure_ascii=False, indent=2)
            return version_info
    except Exception as e:
        logger.error(f"读取版本信息失败: {e}")
        return {
            "version": "v1.0.0",
            "last_check": datetime.now().isoformat(),
            "update_available": False,
            "latest_version": "",
            "update_url": "",
            "update_description": ""
        }


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例"""
    global app, templates

    # 创建 FastAPI 应用
    app = FastAPI(
        title="AllBot 智能微信机器人管理后台",
        description="""
## AllBot 管理后台 API 文档

基于 FastAPI 构建的 AllBot 可视化管理平台，提供完整的机器人管理功能。

### 主要功能模块

* **系统监控**：实时查看 CPU、内存、磁盘使用情况
* **插件管理**：安装、卸载、启用、禁用、配置插件（支持 56+ 插件）
* **账号管理**：多微信账号绑定与切换
* **文件管理**：上传、下载、删除机器人使用的文件
* **联系人管理**：好友、群组列表查看与搜索
* **朋友圈管理**：查看、点赞、评论朋友圈
* **提醒管理**：定时提醒的增删改查
* **适配器管理**：多平台适配器状态查看（QQ/Telegram/Web/Windows）
* **AI 平台管理**：配置各类 AI 模型平台的密钥

### 认证说明

所有 API 需要通过 HTTP Basic Auth 或 Session 认证。

### 技术栈

- **框架**：FastAPI + Uvicorn
- **模板引擎**：Jinja2
- **数据库**：SQLite (aiosqlite)
- **缓存**：Redis
- **日志**：Loguru
        """,
        version="1.0.0",
        openapi_tags=tags_metadata,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        contact={
            "name": "AllBot 开发团队",
            "url": "https://github.com/yourusername/allbot",
        },
        license_info={
            "name": "MIT License",
            "url": "https://opensource.org/licenses/MIT",
        },
    )

    # 配置模板目录
    templates_dir = os.path.join(admin_dir, "templates")
    templates = Jinja2Templates(directory=templates_dir)
    logger.info(f"模板目录: {templates_dir}")

    # 配置静态文件目录
    static_dir = os.path.join(admin_dir, "static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.mount("/admin/static", StaticFiles(directory=static_dir), name="admin.static")
    logger.info(f"静态文件目录: {static_dir}")

    # 添加中间件
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    logger.info("中间件添加完成")

    # 将 check_auth 函数附加到 app.state，供路由模块使用
    app.state.check_auth = check_auth

    # 初始化认证依赖注入模块
    from admin.utils import init_auth_dependencies
    init_auth_dependencies(check_auth)
    logger.info("认证依赖注入模块已初始化")

    # 初始化 app.state 依赖注入
    init_app_state(app)

    # 统一注册路由（避免在多个位置重复注册导致冲突）
    try:
        from admin.routes.registry import register_all
        register_all(app)
        logger.info("路由注册中心已完成统一注册")
    except Exception as e:
        logger.error(f"路由注册中心注册失败: {e}")

    return app


def init_app_state(app: FastAPI):
    """
    初始化 app.state，注入所有全局依赖

    统一依赖注入机制，所有全局实例通过 app.state 管理，
    符合 SOLID 的依赖倒置原则，提升代码可维护性。

    Args:
        app: FastAPI 应用实例
    """
    logger.info("开始初始化 app.state 依赖注入...")

    # 1. 注入模板引擎
    global templates
    if templates is not None:
        app.state.templates = templates
        logger.info("✓ 已注入 templates")
    else:
        logger.warning("✗ templates 未初始化")

    # 2. 注入更新进度管理器
    try:
        from admin.update_manager import update_progress_manager
        app.state.update_progress_manager = update_progress_manager
        logger.info("✓ 已注入 update_progress_manager")
    except ImportError as e:
        logger.error(f"✗ 导入 update_progress_manager 失败: {e}")
        app.state.update_progress_manager = None

    # 3. 注入插件管理器
    try:
        from utils.plugin_manager import plugin_manager
        app.state.plugin_manager = plugin_manager
        logger.info("✓ 已注入 plugin_manager")
    except ImportError as e:
        logger.warning(f"插件管理器未找到（可能尚未初始化）: {e}")
        app.state.plugin_manager = None

    # 4. 注入 bot 状态获取函数
    def get_bot_status_safe():
        """安全获取 bot 状态，兼容 is_login 属性和 is_logged_in() 方法"""
        if not bot_instance:
            return {"is_login": False, "wxid": ""}

        # 优先使用 is_logged_in() 方法，其次尝试 is_login 属性
        is_login = False
        if hasattr(bot_instance, 'is_logged_in') and callable(bot_instance.is_logged_in):
            is_login = bot_instance.is_logged_in()
        elif hasattr(bot_instance, 'is_login'):
            is_login = bot_instance.is_login

        return {
            "is_login": is_login,
            "wxid": getattr(bot_instance, "wxid", "")
        }

    app.state.get_bot_status = get_bot_status_safe
    logger.info("✓ 已注入 get_bot_status")

    logger.info("app.state 依赖注入完成")


def init_app():
    """初始化 FastAPI 应用（兼容旧代码）"""
    global app, templates
    if app is None:
        create_app()
    logger.info(f"管理后台初始化完成，将在 {config['host']}:{config['port']} 上启动")
