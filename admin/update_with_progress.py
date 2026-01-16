"""
带进度推送的版本更新功能
"""

import os
import asyncio
import tempfile
import shutil
import zipfile
import io
import json
import requests
from datetime import datetime
from loguru import logger


async def update_with_progress(version_info: dict, update_progress_manager, get_github_url, current_dir):
    """
    带进度推送的更新流程

    Args:
        version_info: 版本信息字典
        update_progress_manager: 更新进度管理器实例
        get_github_url: GitHub URL转换函数
        current_dir: 当前目录路径
    """
    temp_dir = None
    try:
        # 开始更新
        await update_progress_manager.start_update()
        await update_progress_manager.update_progress(0, "初始化", "准备开始更新...")
        await asyncio.sleep(0.5)

        # 阶段1: 创建临时目录 (12.5%)
        await update_progress_manager.update_progress(12, "创建临时目录", "正在创建临时工作目录...")
        temp_dir = tempfile.mkdtemp(prefix="allbot_update_")
        logger.info(f"创建临时目录: {temp_dir}")
        await asyncio.sleep(0.5)

        # 阶段2: 下载更新包 (25%)
        await update_progress_manager.update_progress(25, "下载更新", "正在从GitHub下载最新版本...")
        zip_url = get_github_url("https://github.com/sxkiss/allbot/archive/refs/heads/main.zip")
        logger.info(f"正在从 {zip_url} 下载最新代码...")

        response = requests.get(zip_url, timeout=60)
        if response.status_code != 200:
            raise Exception(f"下载失败: HTTP {response.status_code}")

        # 阶段3: 解压文件 (37.5%)
        await update_progress_manager.update_progress(37, "解压文件", "正在解压更新包...")
        z = zipfile.ZipFile(io.BytesIO(response.content))
        z.extractall(temp_dir)
        logger.info(f"已解压文件到临时目录: {temp_dir}")
        await asyncio.sleep(0.5)

        # 获取解压后的目录
        extracted_dir = None
        for item in os.listdir(temp_dir):
            item_path = os.path.join(temp_dir, item)
            if os.path.isdir(item_path):
                extracted_dir = item_path
                break

        if not extracted_dir:
            raise Exception("解压后未找到有效目录")

        # 阶段4: 创建备份 (50%)
        await update_progress_manager.update_progress(50, "创建备份", "正在备份当前版本...")
        root_dir = os.path.dirname(current_dir)
        backup_dir = os.path.join(root_dir, "backup_" + datetime.now().strftime("%Y%m%d%H%M%S"))
        os.makedirs(backup_dir, exist_ok=True)
        logger.info(f"创建备份目录: {backup_dir}")
        await asyncio.sleep(0.5)

        # 需要更新的文件列表
        update_items = [
            "admin", "WechatAPI", "utils",
            "dow/channel", "dow/lib",
            "version.json", "bot_core.py",
            "main_config.toml.example", "main.py"
        ]

        # 阶段5: 备份文件 (62.5%)
        await update_progress_manager.update_progress(62, "备份文件", "正在备份现有文件...")
        for item in update_items:
            src_path = os.path.join(root_dir, item)
            if os.path.exists(src_path):
                backup_path = os.path.join(backup_dir, item)
                if os.path.isdir(src_path):
                    shutil.copytree(src_path, backup_path)
                else:
                    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
                    shutil.copy2(src_path, backup_path)
                logger.info(f"已备份: {item}")
        await asyncio.sleep(0.5)

        # 阶段6: 更新文件 (75%)
        await update_progress_manager.update_progress(75, "更新文件", "正在安装新版本文件...")
        for item in update_items:
            new_src_path = os.path.join(extracted_dir, item)
            if os.path.exists(new_src_path):
                dst_path = os.path.join(root_dir, item)
                if os.path.isdir(dst_path):
                    shutil.rmtree(dst_path)
                elif os.path.exists(dst_path):
                    os.remove(dst_path)

                if os.path.isdir(new_src_path):
                    shutil.copytree(new_src_path, dst_path)
                else:
                    shutil.copy2(new_src_path, dst_path)
                logger.info(f"已更新: {item}")
        await asyncio.sleep(0.5)

        # 阶段7: 更新版本信息 (87.5%)
        await update_progress_manager.update_progress(87, "更新版本信息", "正在更新版本配置...")
        version_info["version"] = version_info["latest_version"]
        version_info["update_available"] = False
        version_info["force_update"] = False
        version_info["last_check"] = datetime.now().isoformat()

        version_file = os.path.join(root_dir, "version.json")
        with open(version_file, "w", encoding="utf-8") as f:
            json.dump(version_info, f, ensure_ascii=False, indent=2)
        await asyncio.sleep(0.5)

        # 阶段8: 清理临时文件 (95%)
        await update_progress_manager.update_progress(95, "清理临时文件", "正在清理临时文件...")
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            logger.info(f"已清理临时目录: {temp_dir}")
        await asyncio.sleep(0.5)

        # 完成更新
        await update_progress_manager.update_progress(100, "完成", "更新成功！系统即将重启...")
        await update_progress_manager.finish_update(success=True)

        logger.info("更新完成，准备重启系统")

    except Exception as e:
        logger.error(f"更新失败: {str(e)}")
        await update_progress_manager.finish_update(success=False, error=str(e))
        # 清理临时目录
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except:
                pass
        raise
