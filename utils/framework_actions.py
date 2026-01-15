import asyncio
import io
import os
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import requests
from loguru import logger

from utils.github_proxy import get_github_url


_update_lock = asyncio.Lock()


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _version_file() -> Path:
    return _project_root() / "version.json"


def _read_version_info() -> Dict:
    path = _version_file()
    if path.exists():
        try:
            import json

            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"读取 version.json 失败，将使用默认值: {e}")

    return {
        "version": "1.0.0",
        "update_available": False,
        "latest_version": "",
        "update_url": "",
        "update_description": "",
        "last_check": datetime.now().isoformat(),
    }


def _write_version_info(version_info: Dict) -> None:
    try:
        import json

        _version_file().write_text(
            json.dumps(version_info, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error(f"写入 version.json 失败: {e}")


def _plugin_market_base_url() -> str:
    return os.environ.get("PLUGIN_MARKET_BASE_URL", "http://v.sxkiss.top")


def _check_update_via_admin_logic(current_version: str) -> Dict:
    """复用后台界面版本检查逻辑（上游市场 /version/check + 更新本地 version.json）。"""
    base_url = _plugin_market_base_url().rstrip("/")
    url = f"{base_url}/version/check"
    logger.info(f"正在请求版本检查: {url}")

    try:
        response = requests.post(url, json={"current_version": current_version}, timeout=5)
        if response.status_code != 200:
            return {"success": False, "error": f"服务器返回错误状态码: {response.status_code}"}
        result = response.json()
    except Exception as e:
        logger.error(f"连接版本检查服务器失败: {e}")
        result = {"success": False, "error": f"连接版本检查服务器失败: {e}"}

    version_info = _read_version_info()
    latest_version = result.get("latest_version", "")
    force_update = bool(result.get("force_update") or result.get("forceUpdate"))

    version_info["last_check"] = datetime.now().isoformat()
    version_info["force_update"] = force_update
    if force_update:
        version_info["update_available"] = True
        version_info["latest_version"] = latest_version or current_version
        version_info["update_url"] = result.get("update_url", "")
        version_info["update_description"] = result.get("update_description", "")
    elif latest_version and latest_version != current_version:
        version_info["update_available"] = True
        version_info["latest_version"] = latest_version
        version_info["update_url"] = result.get("update_url", "")
        version_info["update_description"] = result.get("update_description", "")
    else:
        version_info["update_available"] = False

    _write_version_info(version_info)

    merged = {"success": True, **version_info}
    merged.update({k: v for k, v in result.items() if k not in merged})
    return merged


async def restart_framework() -> bool:
    """重启框架（容器/进程）。"""
    try:
        from admin.restart_api import restart_system

        await restart_system()
        return True
    except Exception as e:
        logger.error(f"重启框架失败: {e}")

    try:
        os._exit(1)
    except Exception as e:
        logger.error(f"退出进程失败: {e}")
        return False


async def update_framework() -> Dict[str, str]:
    """更新框架代码（从 GitHub ZIP 下载）并触发重启。

    说明：
    - 为避免覆盖用户配置/自定义插件，默认不更新 `main_config.toml`、`plugins/`、`adapter/*/config.toml`。
    - 更新完成会在项目根目录生成 `backup_YYYYmmddHHMMSS/` 备份目录。
    """
    async with _update_lock:
        root_dir = _project_root()
        temp_dir = Path(tempfile.mkdtemp(prefix="allbot_update_"))

        version_info = _read_version_info()
        current_version = str(version_info.get("version", "") or "").strip() or "1.0.0"

        check_result = _check_update_via_admin_logic(current_version)
        if not check_result.get("update_available", False):
            return {"success": "false", "message": "没有可用的更新"}

        update_items: List[str] = [
            "admin",
            "WechatAPI",
            "utils",
            "dow/channel",
            "dow/lib",
            "version.json",
            "bot_core.py",
            "main_config.toml.example",
            "main.py",
        ]

        try:
            zip_url = get_github_url("https://github.com/sxkiss/allbot/archive/refs/heads/main.zip")
            logger.info(f"开始下载更新: {zip_url}")
            resp = requests.get(zip_url, timeout=60)
            if resp.status_code != 200:
                return {"success": "false", "message": f"下载更新失败: HTTP {resp.status_code}"}

            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                zf.extractall(temp_dir)

            extracted_dir = next((p for p in temp_dir.iterdir() if p.is_dir()), None)
            if not extracted_dir:
                return {"success": "false", "message": "解压后未找到有效目录"}

            backup_dir = root_dir / ("backup_" + datetime.now().strftime("%Y%m%d%H%M%S"))
            backup_dir.mkdir(parents=True, exist_ok=True)

            for item in update_items:
                src_path = root_dir / item
                if src_path.exists():
                    backup_path = backup_dir / item
                    if src_path.is_dir():
                        shutil.copytree(src_path, backup_path)
                    else:
                        backup_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_path, backup_path)

                new_src_path = extracted_dir / item
                if not new_src_path.exists():
                    logger.warning(f"更新包中未找到: {item}")
                    continue

                dst_path = root_dir / item
                if dst_path.exists():
                    if dst_path.is_dir():
                        shutil.rmtree(dst_path)
                    else:
                        dst_path.unlink()

                if new_src_path.is_dir():
                    shutil.copytree(new_src_path, dst_path)
                else:
                    dst_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(new_src_path, dst_path)

                logger.info(f"已更新: {item}")

            # 更新版本信息（与后台逻辑一致）
            latest_version = str(check_result.get("latest_version", "") or "").strip()
            new_version_info = _read_version_info()
            if latest_version:
                new_version_info["version"] = latest_version
            new_version_info["update_available"] = False
            new_version_info["last_check"] = datetime.now().isoformat()
            _write_version_info(new_version_info)

            logger.success("更新完成，准备重启框架")
            await restart_framework()
            return {"success": "true", "message": "更新完成，正在重启框架..."}
        except Exception as e:
            logger.error(f"更新失败: {e}")
            return {"success": "false", "message": f"更新失败: {e}"}
        finally:
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass
