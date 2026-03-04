"""
@input: Wechat 客户端实例、状态回调与配置路径
@output: 登录态建立、会话恢复与在线状态更新
@position: bot_core 启动流程中的登录编排层
@auto-doc: Update header and folder INDEX.md when this file changes
"""

import asyncio
import aiohttp
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from loguru import logger


class WechatLoginHandler:
    """微信登录处理器"""

    def __init__(self, bot, api_host: str, api_port: int, script_dir: Path, update_status_callback):
        self.bot = bot
        self.api_host = api_host
        self.api_port = api_port
        self.script_dir = script_dir
        self.update_status = update_status_callback

    async def handle_login(self, enable_wechat_login: bool) -> bool:
        if not enable_wechat_login:
            logger.warning("已禁用原生微信登录（enable-wechat-login=false），系统将仅依赖适配器处理消息")
            self.update_status(
                "adapter_mode",
                "已禁用微信登录，等待适配器消息",
                {
                    "nickname": self.bot.nickname or "",
                    "wxid": self.bot.wxid or "",
                    "alias": self.bot.alias or "",
                    "device_name": getattr(self.bot, "device_name", "") or "",
                    "device_id": getattr(self.bot, "device_id", "") or "",
                },
            )
            return True

        protocol_version = str(getattr(self.bot, "protocol_version", "")).lower()
        if protocol_version == "869":
            return await self._handle_login_869()

        robot_stat = self._load_robot_stat()
        wxid = robot_stat.get("wxid", None)
        device_name = robot_stat.get("device_name", None)
        device_id = robot_stat.get("device_id", None)

        if await self.bot.is_logged_in(wxid):
            await self._handle_already_logged_in(wxid)
        else:
            device_name, device_id = await self._handle_new_login(wxid, device_name, device_id)
            self._save_robot_stat(self.bot.wxid, device_name, device_id)

        logger.info("登录设备信息: device_name: {}  device_id: {}", device_name, device_id)
        logger.success("登录成功")

        self.update_status(
            "online",
            f"已登录：{self.bot.nickname}",
            {
                "nickname": self.bot.nickname,
                "wxid": self.bot.wxid,
                "alias": self.bot.alias,
                "device_name": device_name or "",
                "device_id": device_id or "",
            },
        )

        await self._start_auto_heartbeat()
        return True

    async def _handle_login_869(self) -> bool:
        robot_stat = self._load_robot_stat()

        device_name = robot_stat.get("device_name") or self.bot.create_device_name()
        device_id = robot_stat.get("device_id") or self.bot.create_device_id()

        auth_keys = robot_stat.get("auth_keys") or []
        if isinstance(auth_keys, str):
            auth_keys = [auth_keys]
        if not isinstance(auth_keys, list):
            auth_keys = []
        auth_keys = [str(x).strip() for x in auth_keys if str(x).strip()]
        auth_key_single = str(robot_stat.get("auth_key", "") or "").strip()
        if auth_key_single and auth_key_single not in auth_keys:
            auth_keys.insert(0, auth_key_single)

        self.bot.auth_keys = auth_keys
        self.bot.auth_key = auth_keys[0] if auth_keys else getattr(self.bot, "auth_key", "")

        self.bot.token_key = robot_stat.get("token_key", "") or getattr(self.bot, "token_key", "")
        self.bot.poll_key = robot_stat.get("poll_key", "") or getattr(self.bot, "poll_key", "")
        self.bot.display_uuid = robot_stat.get("display_uuid", "") or getattr(self.bot, "display_uuid", "")
        self.bot.login_tx_id = robot_stat.get("login_tx_id", "") or getattr(self.bot, "login_tx_id", "")
        self.bot.data62 = robot_stat.get("data62", "") or getattr(self.bot, "data62", "")
        self.bot.ticket = robot_stat.get("ticket", "") or getattr(self.bot, "ticket", "")
        self.bot.device_type = robot_stat.get("device_type", "") or getattr(self.bot, "device_type", "")
        self.bot.device_id = device_id

        wxid = robot_stat.get("wxid", "")
        if wxid:
            self.bot.wxid = wxid

        if await self.bot.is_logged_in(wxid):
            await self._apply_profile_from_bot()
        else:
            logger.info("869 会话未恢复，开始二维码登录流程")
            login_ok = False
            login_mode = "mac" if str(getattr(self.bot, "device_type", "")).lower() == "mac" else "ipad"

            while not login_ok:
                uuid, url = await self.bot.get_qr_code(
                    device_name=login_mode,
                    device_id=device_id,
                    print_qr=True,
                )

                logger.success("获取到登录uuid: {}（模式={}）", uuid, login_mode)
                logger.success("获取到登录二维码: {}（模式={}）", url, login_mode)

                self._save_robot_stat(
                    self.bot.wxid or wxid,
                    device_name,
                    device_id,
                    {
                        "auth_key": getattr(self.bot, "auth_key", "") or "",
                        "auth_keys": getattr(self.bot, "auth_keys", []) or [],
                        "token_key": getattr(self.bot, "token_key", ""),
                        "poll_key": getattr(self.bot, "poll_key", ""),
                        "display_uuid": getattr(self.bot, "display_uuid", ""),
                        "login_tx_id": getattr(self.bot, "login_tx_id", ""),
                        "data62": getattr(self.bot, "data62", "") or "",
                        "ticket": getattr(self.bot, "ticket", "") or "",
                        "device_type": getattr(self.bot, "device_type", "") or device_name,
                    },
                )

                self.update_status(
                    "waiting_login",
                    "等待微信扫码登录",
                    {
                        "qrcode_url": url,
                        "uuid": uuid,
                        "login_mode": login_mode,
                        "device_name": device_name,
                        "device_id": device_id,
                        "token_key": getattr(self.bot, "token_key", ""),
                        "poll_key": getattr(self.bot, "poll_key", ""),
                        "data62": getattr(self.bot, "data62", "") or "",
                        "ticket": getattr(self.bot, "ticket", "") or "",
                        "expires_in": 240,
                        "timestamp": time.time(),
                    },
                )
                self._verify_status_file(url, uuid)

                switch_to_mac = False
                for _ in range(120):
                    ok, data = await self.bot.check_login_uuid(getattr(self.bot, "poll_key", "") or uuid, device_id=device_id)
                    if ok:
                        login_ok = True
                        login_wxid = ""
                        if isinstance(data, dict):
                            login_wxid = (
                                data.get("wxid")
                                or data.get("Wxid")
                                or data.get("UserName")
                                or data.get("user_name")
                                or ""
                            )
                        if login_wxid:
                            self.bot.wxid = login_wxid
                        break

                    if isinstance(data, dict):
                        if login_mode != "mac" and self._need_switch_to_mac(data):
                            switch_to_mac = True
                            logger.warning("检测到无数字验证码验证场景，下一轮将切换为 mac 模式拉码")
                            break
                        if self._has_numeric_verify_code(data):
                            logger.warning(
                                "检测到数字安全码验证，请使用 VerifyCode：code=手机显示数字，data62={}, ticket={}",
                                data.get("data62") or data.get("Data62") or getattr(self.bot, "data62", ""),
                                data.get("ticket") or data.get("Ticket") or getattr(self.bot, "ticket", ""),
                            )
                    await asyncio.sleep(2)

                if not login_ok:
                    if switch_to_mac:
                        login_mode = "mac"
                        continue
                    logger.warning("869 二维码登录轮询超时，准备重新拉取二维码")

            await self._apply_profile_from_bot()

        self._save_robot_stat(
            self.bot.wxid,
            device_name,
            device_id,
            {
                "auth_key": getattr(self.bot, "auth_key", "") or "",
                "auth_keys": getattr(self.bot, "auth_keys", []) or [],
                "token_key": getattr(self.bot, "token_key", ""),
                "poll_key": getattr(self.bot, "poll_key", ""),
                "display_uuid": getattr(self.bot, "display_uuid", ""),
                "login_tx_id": getattr(self.bot, "login_tx_id", ""),
                "data62": getattr(self.bot, "data62", "") or "",
                "ticket": getattr(self.bot, "ticket", "") or "",
                "device_type": getattr(self.bot, "device_type", "") or device_name,
            },
        )

        logger.success("869 登录成功")
        self.update_status(
            "online",
            f"已登录：{self.bot.nickname}",
            {
                "nickname": self.bot.nickname,
                "wxid": self.bot.wxid,
                "alias": self.bot.alias,
                "device_name": device_name,
                "device_id": device_id,
                "device_type": getattr(self.bot, "device_type", "") or "",
            },
        )
        await self._start_auto_heartbeat()
        return True

    @staticmethod
    def _flatten_text(payload: Dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            return ""
        text = json.dumps(payload, ensure_ascii=False)
        return text.lower()

    def _has_numeric_verify_code(self, payload: Dict[str, Any]) -> bool:
        text = self._flatten_text(payload)
        return bool(re.search(r"\b\d{4,8}\b", text))

    def _need_switch_to_mac(self, payload: Dict[str, Any]) -> bool:
        text = self._flatten_text(payload)
        if not text:
            return False
        trigger_words = (
            "slideticket",
            "randstr",
            "verifycodeslide",
            "slide",
            "slider",
            "滑块",
            "captcha",
            "在新设备完成验证",
        )
        if any(word in text for word in trigger_words):
            return not self._has_numeric_verify_code(payload)
        return False

    def _load_robot_stat(self) -> Dict[str, Any]:
        robot_stat_path = self.script_dir / "resource" / "robot_stat.json"
        if not robot_stat_path.exists():
            default_config = {
                "wxid": "",
                "device_name": "",
                "device_id": "",
                "auth_key": "",
                "auth_keys": [],
                "token_key": "",
                "poll_key": "",
                "display_uuid": "",
                "login_tx_id": "",
                "data62": "",
                "ticket": "",
                "device_type": "",
            }
            robot_stat_path.parent.mkdir(parents=True, exist_ok=True)
            with open(robot_stat_path, "w", encoding="utf-8") as file:
                json.dump(default_config, file, ensure_ascii=False)
            return default_config

        with open(robot_stat_path, "r", encoding="utf-8") as file:
            return json.load(file)

    def _save_robot_stat(self, wxid: str, device_name: str, device_id: str, extra: Optional[Dict[str, Any]] = None):
        robot_stat = {
            "wxid": wxid,
            "device_name": device_name,
            "device_id": device_id,
        }
        if extra:
            robot_stat.update(extra)

        robot_stat_path = self.script_dir / "resource" / "robot_stat.json"
        with open(robot_stat_path, "w", encoding="utf-8") as file:
            json.dump(robot_stat, file, ensure_ascii=False)

    def _apply_profile(self, profile: Dict[str, Any]):
        user_info = profile.get("userInfo") if isinstance(profile.get("userInfo"), dict) else profile

        def _coerce_text(value: Any) -> str:
            if isinstance(value, dict):
                for key in ("string", "str", "value", "text"):
                    candidate = value.get(key)
                    if candidate not in (None, ""):
                        return str(candidate)
                return ""
            return str(value or "")

        wxid_value = (
            user_info.get("UserName")
            or user_info.get("userName")
            or user_info.get("Wxid")
            or user_info.get("wxid")
            or self.bot.wxid
        )
        nickname_value = user_info.get("NickName") or user_info.get("nickName") or user_info.get("nickname")
        alias_value = user_info.get("Alias") or user_info.get("alias")
        phone_value = user_info.get("BindMobile") or user_info.get("phone")

        self.bot.wxid = _coerce_text(wxid_value)
        self.bot.nickname = _coerce_text(nickname_value)
        self.bot.alias = _coerce_text(alias_value)
        self.bot.phone = _coerce_text(phone_value)

    async def _apply_profile_from_bot(self):
        profile = await self.bot.get_profile()
        if isinstance(profile, dict):
            self._apply_profile(profile)

        logger.info(
            "profile登录账号信息: wxid: {}  昵称: {}  微信号: {}  手机号: {}",
            self.bot.wxid,
            self.bot.nickname,
            self.bot.alias,
            self.bot.phone,
        )

    async def _handle_already_logged_in(self, wxid: str):
        self.bot.wxid = wxid
        await self._apply_profile_from_bot()

    async def _handle_new_login(
        self,
        wxid: Optional[str],
        device_name: Optional[str],
        device_id: Optional[str],
    ) -> Tuple[str, str]:
        while not await self.bot.is_logged_in(wxid):
            try:
                get_cached_info = await self.bot.get_cached_info(wxid)

                if get_cached_info:
                    device_name, device_id = await self._try_twice_login(wxid, device_name, device_id)
                else:
                    device_name, device_id = await self._qrcode_login(device_name, device_id)

            except Exception as error:
                logger.error("发生错误: {}", error)
                device_name, device_id = await self._qrcode_login(device_name, device_id)

            await self._wait_for_login_completion(device_name, device_id)

        return device_name, device_id

    async def _try_twice_login(
        self,
        wxid: str,
        device_name: Optional[str],
        device_id: Optional[str],
    ) -> Tuple[str, str]:
        twice = await self.bot.twice_login(wxid)
        logger.info("二次登录:{}", twice)

        if not twice:
            logger.error("二次登录失败，请检查微信是否在运行中，或重新启动机器人")
            logger.info("尝试唤醒登录...")

            try:
                device_name, device_id = await self._awaken_login(wxid, device_name, device_id)
            except Exception as error:
                logger.error("唤醒登录失败: {}", error)
                device_name, device_id = await self._qrcode_login(device_name, device_id)

        return device_name, device_id

    async def _awaken_login(
        self,
        wxid: str,
        device_name: Optional[str],
        device_id: Optional[str],
    ) -> Tuple[str, str]:
        async with aiohttp.ClientSession() as session:
            api_url = f"http://{self.api_host}:{self.api_port}/api/Login/LoginTwiceAutoAuth"
            json_param = {
                "OS": device_name if device_name else "iPad",
                "Proxy": {"ProxyIp": "", "ProxyPassword": "", "ProxyUser": ""},
                "Url": "",
                "Wxid": wxid,
            }

            logger.debug("发送唤醒登录请求到 {} 参数: {}", api_url, json_param)

            try:
                response = await session.post(api_url, json=json_param)
                if response.status != 200:
                    raise Exception(f"服务器返回状态码 {response.status}")

                json_resp = await response.json()
                logger.debug("唤醒登录响应: {}", json_resp)

                if json_resp and json_resp.get("Success"):
                    data = json_resp.get("Data", {})
                    qr_response = data.get("QrCodeResponse", {}) if data else {}
                    uuid = qr_response.get("Uuid", "") if qr_response else ""

                    if uuid:
                        logger.success("唤醒登录成功，获取到登录uuid: {}", uuid)
                        self.update_status("waiting_login", f"等待微信登录 (UUID: {uuid})")
                        return device_name, device_id

                    raise Exception("响应中没有有效的UUID")

                error_msg = json_resp.get("Message", "未知错误") if json_resp else "未知错误"
                raise Exception(error_msg)

            except Exception as error:
                logger.error("唤醒登录过程中出错: {}", error)
                logger.error("将尝试二维码登录")
                return await self._qrcode_login(device_name, device_id)

    async def _qrcode_login(self, device_name: Optional[str], device_id: Optional[str]) -> Tuple[str, str]:
        if not device_name:
            device_name = self.bot.create_device_name()
        if not device_id:
            device_id = self.bot.create_device_id()

        uuid, url = await self.bot.get_qr_code(device_id=device_id, device_name=device_name, print_qr=True)
        logger.success("获取到登录uuid: {}", uuid)
        logger.success("获取到登录二维码: {}", url)

        self.update_status(
            "waiting_login",
            "等待微信扫码登录",
            {
                "qrcode_url": url,
                "uuid": uuid,
                "device_name": device_name,
                "device_id": device_id,
                "expires_in": 240,
                "timestamp": time.time(),
            },
        )

        self._verify_status_file(url, uuid)
        logger.info("等待登录中，过期倒计时：240")

        return device_name, device_id

    def _verify_status_file(self, url: str, uuid: str):
        try:
            status_file = self.script_dir / "admin" / "bot_status.json"
            if status_file.exists():
                with open(status_file, "r", encoding="utf-8") as file:
                    current_status = json.load(file)
                    if current_status.get("qrcode_url") != url:
                        logger.warning("状态文件中的二维码URL与实际不符，尝试重新更新状态")
                        self.update_status(
                            "waiting_login",
                            "等待微信扫码登录",
                            {
                                "qrcode_url": url,
                                "uuid": uuid,
                                "expires_in": 240,
                                "timestamp": time.time(),
                            },
                        )
        except Exception as error:
            logger.error("检查状态文件失败: {}", error)

    async def _wait_for_login_completion(self, device_name: str, device_id: str):
        _ = (device_name, device_id)
        await asyncio.sleep(1)

    async def _start_auto_heartbeat(self):
        try:
            success = await self.bot.start_auto_heartbeat()
            if success:
                logger.success("已开启自动心跳")
            else:
                logger.warning("开启自动心跳失败")
        except ValueError:
            logger.warning("自动心跳已在运行")
        except Exception as error:
            logger.warning("自动心跳已在运行:{}", error)
