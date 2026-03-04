"""
@input: aiohttp/http 接口, pysilk 语音解码; bot_core 与插件层的调用契约
@output: Client869 全接口动态调用能力与 bot_core 兼容方法
@position: 869 协议专用客户端，隔离新协议实现，最小侵入接入现有框架
@auto-doc: Update header and folder INDEX.md when this file changes
"""

import asyncio
import base64
import hashlib
import io
import os
import string
import time
from random import choice
from typing import Any, Dict, Iterable, Optional, Tuple, Union
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import aiohttp
import pysilk
from loguru import logger
from pydub import AudioSegment

from WechatAPI.errors import UserLoggedOut


TEXT_VALUE_KEYS = ("string", "String", "str", "Str", "value", "Value", "text", "Text")

KEY_AUTH_KEY_CANDIDATES = ("AuthKey", "auth_key", "Key", "key")
KEY_TOKEN_CANDIDATES = ("TokenKey", "token_key", "tokenKey")
KEY_POLL_CANDIDATES = ("PollKey", "poll_key", "Uuid", "uuid")
KEY_DISPLAY_UUID_CANDIDATES = ("DisplayUuid", "display_uuid", "Uuid", "uuid")
KEY_LOGIN_TX_ID_CANDIDATES = ("LoginTxId", "login_tx_id")
KEY_QR_URL_CANDIDATES = ("QrCodeUrl", "QrUrl", "qr_code_url", "qr_url", "Url", "url")
KEY_UUID_CANDIDATES = ("Uuid", "uuid", "DisplayUuid", "display_uuid")

KEY_WXID_CANDIDATES = ("Wxid", "wxid", "UserName", "user_name", "UserNameStr", "FromUserName")
KEY_STATUS_CANDIDATES = ("Status", "status", "LoginStatus", "login_status", "state", "State")
KEY_LOGIN_BOOL_CANDIDATES = ("IsLogin", "is_login", "LoggedIn", "logged_in", "Status", "status")

KEY_DATA62_CANDIDATES = ("Data62", "data62")
KEY_TICKET_CANDIDATES = ("Ticket", "ticket")

KEY_LOGIN_STATE_CANDIDATES = ("loginState", "LoginState")
KEY_LOGIN_ERRMSG_CANDIDATES = ("loginErrMsg", "LoginErrMsg", "errMsg", "ErrMsg", "message", "Message", "Text", "text")

KEY_PROFILE_WXID_CANDIDATES = ("UserName", "userName", "Wxid", "wxid")
KEY_PROFILE_NICKNAME_CANDIDATES = ("NickName", "nickName", "nickname")
KEY_PROFILE_ALIAS_CANDIDATES = ("Alias", "alias", "Wechat", "wechat")
KEY_PROFILE_PHONE_CANDIDATES = ("BindMobile", "bindMobile", "Phone", "phone")

KEY_SEND_CLIENT_MSG_ID_CANDIDATES = ("ClientMsgid", "ClientMsgId", "clientMsgId", "client_msg_id")
KEY_SEND_CREATE_TIME_CANDIDATES = ("Createtime", "CreateTime", "createTime", "create_time")
KEY_SEND_NEW_MSG_ID_CANDIDATES = ("NewMsgId", "newMsgId", "new_msg_id")


def _extract_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, dict):
        for key in TEXT_VALUE_KEYS:
            candidate = value.get(key)
            if candidate not in (None, ""):
                return str(candidate)
        return default
    return str(value)


def _extract_uuid_from_qr_url(qr_url: str) -> str:
    if not qr_url:
        return ""
    candidate = qr_url
    parsed = urlparse(qr_url)
    query = parse_qs(parsed.query)
    for key in ("url", "u", "q"):
        value = query.get(key, [""])[0]
        if value and "weixin.qq.com/x/" in value:
            candidate = value
            break
    marker = "weixin.qq.com/x/"
    index = candidate.find(marker)
    if index == -1:
        return ""
    value = candidate[index + len(marker) :]
    value = value.split("?", 1)[0].split("&", 1)[0].strip()
    return value


def _extract_auth_keys(payload: Any) -> Iterable[str]:
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        if isinstance(item, str):
            value = item.strip()
            if value:
                yield value
            continue
        if isinstance(item, dict):
            value = _pick_first(item, KEY_AUTH_KEY_CANDIDATES, "")
            if value:
                yield str(value).strip()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _pick_first(mapping: Dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) not in (None, ""):
            return mapping.get(key)
    return default


def _looks_like_base64(content: str) -> bool:
    if not content or len(content) % 4 != 0:
        return False
    try:
        base64.b64decode(content, validate=True)
        return True
    except Exception:
        return False


class OperationGroupProxy:
    def __init__(self, client: "Client869", group: str):
        self._client = client
        self._group = group

    def __getattr__(self, action: str):
        async def _caller(body: Optional[Dict[str, Any]] = None, **kwargs: Any):
            method = kwargs.pop("method", None)
            key = kwargs.pop("key", None)
            params = kwargs.pop("params", None)
            raw = kwargs.pop("raw", False)
            payload = body if body is not None else (kwargs if kwargs else None)
            return await self._client.invoke(
                self._group,
                action,
                body=payload,
                method=method,
                key=key,
                params=params,
                raw=raw,
            )

        return _caller


class Client869:
    DEFAULT_GROUPS = {
        "admin",
        "applet",
        "equipment",
        "favor",
        "finder",
        "friend",
        "group",
        "label",
        "login",
        "message",
        "other",
        "pay",
        "qy",
        "shop",
        "sns",
        "user",
        "ws",
    }

    KNOWN_GET_OPERATIONS = {
        ("login", "checkloginstatus"),
        ("login", "getloginstatus"),
        ("user", "getprofile"),
        ("ws", "getsyncmsg"),
    }

    def __init__(
        self,
        ip: str,
        port: int,
        protocol_version: Optional[str] = None,
        admin_key: str = "",
        ws_url: str = "",
    ):
        self.ip = ip
        self.port = port
        self.protocol_version = (protocol_version or "869").lower()

        self.admin_key = admin_key
        self.auth_key = ""
        self.auth_keys: list[str] = []
        self.token_key = ""
        self.poll_key = ""
        self.display_uuid = ""
        self.login_tx_id = ""
        self.data62 = ""
        self.ticket = ""
        self.device_type = ""
        self.device_id = ""

        self.ws_url = ws_url

        self.wxid = ""
        self.nickname = ""
        self.alias = ""
        self.phone = ""

        self.ignore_protect = False
        self.reply_router = None

        self.api_prefix = "/api"
        self._api_prefix = "/api"

        self._operation_map_loaded = False
        self._operation_map: Dict[Tuple[str, str], Tuple[str, str]] = {}
        self._op_map_lock = asyncio.Lock()

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        lowered = name.lower()
        if lowered in self.DEFAULT_GROUPS:
            return OperationGroupProxy(self, lowered)
        raise AttributeError(name)

    @property
    def base_url(self) -> str:
        return f"http://{self.ip}:{self.port}"

    def set_reply_router(self, router: Any):
        self.reply_router = router

    def _should_route_via_reply_router(self, wxid: str) -> bool:
        """判断当前消息是否应走 ReplyRouter（多平台适配器队列）。

        约定：
        - 默认平台为 wechat（无前缀/或普通 wxid/chatroom），应直发 869；
        - 带前缀的平台（如 wxfilehelper-xxx、tg-xxx、web-xxx）才走 ReplyRouter。
        """
        if not self.reply_router:
            return False
        if not wxid:
            return False
        base_id = wxid[:-9] if str(wxid).endswith("@chatroom") else str(wxid)
        if "-" not in base_id:
            return False
        prefix = base_id.split("-", 1)[0].strip().lower()
        return bool(prefix) and prefix != "wechat"

    def _resolve_active_key(self) -> str:
        return self.token_key or self.auth_key

    async def ensure_auth_key(self) -> str:
        if self.auth_key:
            return self.auth_key
        if not self.admin_key:
            raise RuntimeError("缺少 admin_key，无法生成 AuthKey")

        if isinstance(self.auth_keys, list):
            self.auth_keys = [str(x).strip() for x in self.auth_keys if str(x).strip()]
        else:
            self.auth_keys = []

        if self.auth_keys:
            self.auth_key = self.auth_keys[0]
            return self.auth_key

        active_license_keys = await self.call_path("/admin/GetActiveLicenseKeys", method="GET", key=self.admin_key)
        for value in _extract_auth_keys(active_license_keys):
            if value not in self.auth_keys:
                self.auth_keys.append(value)

        if not self.auth_keys:
            generated = await self.call_path("/admin/GenAuthKey2", method="GET", key=self.admin_key)
            for value in _extract_auth_keys(generated):
                if value not in self.auth_keys:
                    self.auth_keys.append(value)

        if self.auth_keys:
            self.auth_key = self.auth_keys[0]
        if not self.auth_key:
            raise RuntimeError("生成 AuthKey 失败")
        return self.auth_key

    @staticmethod
    def _coerce_path(path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            return f"/{path}"
        return path

    async def _ensure_operation_map(self):
        if self._operation_map_loaded:
            return
        async with self._op_map_lock:
            if self._operation_map_loaded:
                return

            for candidate in ("/docs/swagger.json", "/swagger.json"):
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(f"{self.base_url}{candidate}", timeout=8) as response:
                            if response.status != 200:
                                continue
                            payload = await response.json(content_type=None)
                            paths = payload.get("paths", {})
                            if not isinstance(paths, dict):
                                continue

                            for path, methods in paths.items():
                                if not isinstance(methods, dict):
                                    continue
                                parts = [part for part in str(path).split("/") if part]
                                if len(parts) < 2:
                                    continue
                                group = parts[0].lower()
                                action = parts[1].lower()

                                http_method = ""
                                for method_name in ("get", "post", "put", "patch", "delete"):
                                    if method_name in methods:
                                        http_method = method_name.upper()
                                        break
                                if not http_method:
                                    continue

                                self._operation_map[(group, action)] = (path, http_method)
                except Exception as error:
                    logger.debug("加载 Swagger 失败 {}: {}", candidate, error)

                if self._operation_map:
                    break

            self._operation_map_loaded = True
            if self._operation_map:
                logger.info("Client869 已加载 {} 条 Swagger 接口映射", len(self._operation_map))
            else:
                logger.warning("Client869 未加载到 Swagger 映射，动态调用将使用路径推断")

    async def _resolve_operation(
        self,
        group: str,
        action: str,
        method: Optional[str] = None,
    ) -> Tuple[str, str]:
        if method:
            return f"/{group}/{action}", method.upper()

        await self._ensure_operation_map()
        key = (group.lower(), action.lower())
        if key in self._operation_map:
            return self._operation_map[key]

        inferred_method = "GET" if key in self.KNOWN_GET_OPERATIONS else "POST"
        return f"/{group}/{action}", inferred_method

    async def request(
        self,
        path: str,
        method: str = "POST",
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        key: Optional[str] = None,
        timeout: int = 30,
    ) -> Any:
        request_method = method.upper().strip()
        request_path = self._coerce_path(path)
        query: Dict[str, Any] = dict(params or {})

        active_key = key if key is not None else self._resolve_active_key()
        if active_key:
            query["key"] = active_key

        self._log_request_preview(request_method, request_path, body)

        request_url = request_path
        if not request_url.startswith("http://") and not request_url.startswith("https://"):
            request_url = f"{self.base_url}{request_path}"

        async with aiohttp.ClientSession() as session:
            if request_method == "GET":
                response = await session.get(request_url, params=query, timeout=timeout)
            else:
                response = await session.request(
                    request_method,
                    request_url,
                    params=query,
                    json=body if body is not None else {},
                    timeout=timeout,
                )

            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type or "json" in content_type:
                payload = await response.json(content_type=None)
            else:
                payload = await response.text()

        if isinstance(payload, dict):
            self._log_response_preview(request_method, request_path, payload)
            code = payload.get("Code")
            if code not in (None, 0, 200):
                raise RuntimeError(
                    payload.get("Text")
                    or payload.get("Message")
                    or payload.get("message")
                    or "869 接口请求失败"
                )
            if code is None and payload.get("Success") is False:
                raise RuntimeError(
                    payload.get("Text")
                    or payload.get("Message")
                    or payload.get("message")
                    or "869 接口请求失败"
                )
        return payload

    @staticmethod
    def _is_send_related_path(path: str) -> bool:
        if not path:
            return False
        lowered = path.lower()
        if lowered.startswith("/message/"):
            return any(
                token in lowered
                for token in (
                    "sendtextmessage",
                    "sendimagemessage",
                    "sendimagenewmessage",
                    "sendvoice",
                    "cdnuploadvideo",
                    "sendappmessage",
                    "sharecardmessage",
                    "sendemojimessage",
                    "forward",
                    "groupmassmsg",
                )
            )
        return lowered.startswith("/other/uploadappattach")

    def _log_request_preview(self, method: str, path: str, body: Optional[Dict[str, Any]]) -> None:
        if not self._is_send_related_path(path):
            return

        receiver = ""
        msg_type = ""
        if isinstance(body, dict):
            if isinstance(body.get("MsgItem"), list) and body["MsgItem"]:
                item = body["MsgItem"][0] if isinstance(body["MsgItem"][0], dict) else {}
                receiver = str(item.get("ToUserName") or "")
                msg_type = str(item.get("MsgType") or "")
            elif isinstance(body.get("AppList"), list) and body["AppList"]:
                item = body["AppList"][0] if isinstance(body["AppList"][0], dict) else {}
                receiver = str(item.get("ToUserName") or "")
                msg_type = f"app:{item.get('ContentType')}"
            elif isinstance(body.get("EmojiList"), list) and body["EmojiList"]:
                item = body["EmojiList"][0] if isinstance(body["EmojiList"][0], dict) else {}
                receiver = str(item.get("ToUserName") or "")
                msg_type = "emoji"
            else:
                receiver = str(body.get("ToUserName") or "")

        logger.info("Client869 请求: {} {} to={} type={}", method, path, receiver, msg_type)

    def _log_response_preview(self, method: str, path: str, payload: Dict[str, Any]) -> None:
        if not self._is_send_related_path(path):
            return

        code = payload.get("Code")
        text = payload.get("Text") or payload.get("Message") or ""
        success_flag = payload.get("Success")
        data = payload.get("Data")
        send_success = None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            send_success = data[0].get("isSendSuccess")

        logger.info(
            "Client869 响应: {} {} code={} success={} isSendSuccess={} text={}",
            method,
            path,
            code,
            success_flag,
            send_success,
            str(text)[:120],
        )

    async def call_path(
        self,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        method: str = "POST",
        key: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        raw: bool = False,
    ) -> Any:
        payload = await self.request(path, method=method, body=body, params=params, key=key)
        if raw:
            return payload
        if isinstance(payload, dict) and "Data" in payload:
            return payload.get("Data")
        return payload

    async def invoke(
        self,
        group: str,
        action: str,
        body: Optional[Dict[str, Any]] = None,
        method: Optional[str] = None,
        key: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        raw: bool = False,
    ) -> Any:
        path, request_method = await self._resolve_operation(group, action, method)
        return await self.call_path(
            path,
            body=body,
            method=request_method,
            key=key,
            params=params,
            raw=raw,
        )

    @staticmethod
    def create_device_name() -> str:
        first_names = [
            "Oliver", "Emma", "Liam", "Ava", "Noah", "Sophia", "Elijah", "Isabella",
            "James", "Mia", "William", "Amelia", "Benjamin", "Harper", "Lucas", "Evelyn",
        ]
        last_names = [
            "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
            "Rodriguez", "Martinez", "Wilson", "Anderson", "Taylor", "Moore", "Jackson", "Martin",
        ]
        return choice(first_names) + " " + choice(last_names) + "'s Pad"

    @staticmethod
    def create_device_id(seed: str = "") -> str:
        value = seed or "".join(choice(string.ascii_letters) for _ in range(15))
        md5_hash = hashlib.md5(value.encode()).hexdigest()
        return "49" + md5_hash[2:]

    def _sync_key_from_url(self, url: str):
        if not url:
            return
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        key = query.get("key", [""])[0]
        if key and not self.token_key:
            self.token_key = key

    def _append_key_to_ws_url(self, ws_url: str, key: str) -> str:
        parsed = urlparse(ws_url)
        query = parse_qs(parsed.query)
        if key and "key" not in query:
            query["key"] = [key]
        new_query = urlencode({k: v[-1] if isinstance(v, list) else v for k, v in query.items()})
        return urlunparse(parsed._replace(query=new_query))

    async def is_running(self) -> bool:
        try:
            async with aiohttp.ClientSession() as session:
                response = await session.get(f"{self.base_url}/docs", timeout=5)
            return response.status == 200
        except Exception:
            return False

    async def get_qr_code(
        self,
        device_name: str,
        device_id: str = "",
        proxy: Any = None,
        print_qr: bool = False,
    ) -> Tuple[str, str]:
        auth_key = await self.ensure_auth_key()
        proxy_value = ""
        if proxy is not None:
            proxy_ip = getattr(proxy, "ip", "")
            proxy_port = getattr(proxy, "port", "")
            proxy_user = getattr(proxy, "username", "")
            proxy_password = getattr(proxy, "password", "")
            if proxy_ip and proxy_port:
                auth_prefix = f"{proxy_user}:{proxy_password}@" if proxy_user else ""
                proxy_value = f"socks5://{auth_prefix}{proxy_ip}:{proxy_port}"

        requested_device = (device_name or "").strip().lower()
        login_device = "mac" if requested_device == "mac" else "ipad"

        payload = {"IpadOrmac": login_device, "Check": False}
        if proxy_value:
            payload["Proxy"] = proxy_value

        response = await self.request(
            "/login/GetLoginQrCodeNewDirect",
            method="POST",
            body=payload,
            key=auth_key,
        )
        data62 = _pick_first(response, KEY_DATA62_CANDIDATES, "")
        if data62:
            self.data62 = str(data62)
        data = response.get("Data", {}) if isinstance(response, dict) else {}

        token_key = _pick_first(data, KEY_TOKEN_CANDIDATES, "")
        poll_key = _pick_first(data, KEY_POLL_CANDIDATES, "")
        display_uuid = _pick_first(data, KEY_DISPLAY_UUID_CANDIDATES, "")
        login_tx_id = _pick_first(data, KEY_LOGIN_TX_ID_CANDIDATES, "")

        qr_url = _pick_first(data, KEY_QR_URL_CANDIDATES, "")
        uuid = _pick_first(data, KEY_UUID_CANDIDATES, "") or _extract_uuid_from_qr_url(str(qr_url))
        if not qr_url and uuid:
            qr_url = f"http://weixin.qq.com/x/{uuid}"

        auth_key_from_data = _pick_first(data, KEY_AUTH_KEY_CANDIDATES, "")
        if auth_key_from_data:
            self.auth_key = str(auth_key_from_data)

        if token_key:
            self.token_key = str(token_key)
        self.poll_key = str(poll_key or self.poll_key or self.auth_key)
        if display_uuid:
            self.display_uuid = str(display_uuid)
        elif uuid:
            self.display_uuid = str(uuid)
        if login_tx_id:
            self.login_tx_id = str(login_tx_id)

        self.device_type = device_name or self.device_type
        if device_id:
            self.device_id = device_id
        self.device_type = login_device

        self._sync_key_from_url(qr_url)

        if print_qr and qr_url:
            logger.info("869 二维码链接: {}", qr_url)

        return str(uuid), str(qr_url)

    async def check_login_uuid(self, uuid: str, device_id: str = "") -> Tuple[bool, Union[Dict[str, Any], int, str]]:
        check_key = self.token_key or self.poll_key or self.auth_key or uuid
        if not check_key:
            return False, "缺少登录 Key"

        try:
            response = await self.request(
                "/login/CheckLoginStatus",
                method="GET",
                key=str(check_key),
            )
        except Exception as error:
            return False, str(error)

        ticket = _pick_first(response, KEY_TICKET_CANDIDATES, "")
        if ticket:
            self.ticket = str(ticket)

        data = response.get("Data", {}) if isinstance(response, dict) else {}
        if not isinstance(data, dict):
            data = {}

        login_wxid = _pick_first(data, KEY_WXID_CANDIDATES, "")

        status_code = _safe_int(_pick_first(data, KEY_STATUS_CANDIDATES, 0), 0)
        is_logged = bool(login_wxid) or status_code in {1, 2, 200, 201}

        key_from_data = _pick_first(data, KEY_TOKEN_CANDIDATES, "")
        if key_from_data:
            self.token_key = str(key_from_data)

        if login_wxid:
            self.wxid = str(login_wxid)

        if device_id:
            self.device_id = device_id

        merged: Dict[str, Any] = dict(data)
        if self.data62 and "data62" not in merged and "Data62" not in merged:
            merged["data62"] = self.data62
        if self.ticket and "ticket" not in merged and "Ticket" not in merged:
            merged["ticket"] = self.ticket
        return is_logged, merged if merged else data if data else response

    async def verify_code(self, code: str, *, data62: str = "", ticket: str = "", key: str = "") -> Dict[str, Any]:
        payload = {
            "code": str(code or "").strip(),
            "data62": str(data62 or self.data62 or "").strip(),
            "ticket": str(ticket or self.ticket or "").strip(),
        }
        active_key = key or self.token_key or self.poll_key or self.auth_key
        return await self.request("/login/VerifyCode", method="POST", body=payload, key=active_key)

    async def verify_code_slide(
        self,
        slide_ticket: str,
        rand_str: str,
        *,
        data62: str = "",
        ticket: str = "",
        key: str = "",
    ) -> Dict[str, Any]:
        payload = {
            "slideticket": str(slide_ticket or "").strip(),
            "randstr": str(rand_str or "").strip(),
            "data62": str(data62 or self.data62 or "").strip(),
            "ticket": str(ticket or self.ticket or "").strip(),
        }
        active_key = key or self.token_key or self.poll_key or self.auth_key
        return await self.request("/login/VerifyCodeSlide", method="POST", body=payload, key=active_key)

    async def is_logged_in(self, wxid: Optional[str] = None) -> bool:
        key = self._resolve_active_key()
        if not key:
            return False

        try:
            response = await self.request("/login/GetLoginStatus", method="GET", key=key)
        except Exception:
            return False

        data = response.get("Data") if isinstance(response, dict) else None
        if isinstance(data, dict):
            remote_wxid = _pick_first(data, KEY_WXID_CANDIDATES, "")
            status = _pick_first(data, KEY_LOGIN_BOOL_CANDIDATES, False)
            login_state = _safe_int(_pick_first(data, KEY_LOGIN_STATE_CANDIDATES, 0), 0)
            login_msg = _extract_text(_pick_first(data, KEY_LOGIN_ERRMSG_CANDIDATES, ""))
            if remote_wxid:
                self.wxid = str(remote_wxid)
            if wxid and remote_wxid and str(wxid) != str(remote_wxid):
                return False
            if login_state in {1, 2, 200, 201}:
                return True
            if isinstance(status, (int, float)) and int(status) in {1, 2, 200, 201}:
                return True
            if bool(status) or bool(remote_wxid):
                return True
            if "在线" in login_msg or "online" in login_msg.lower():
                return True
            return False

        if isinstance(data, bool):
            return data

        if isinstance(response, dict):
            return bool(response.get("Success"))

        return False

    def _apply_profile(self, profile: Dict[str, Any]):
        user_info = profile.get("userInfo", {}) if isinstance(profile.get("userInfo"), dict) else {}
        src = user_info if user_info else profile

        wxid = _pick_first(src, KEY_PROFILE_WXID_CANDIDATES, "")
        nickname = _extract_text(_pick_first(src, KEY_PROFILE_NICKNAME_CANDIDATES, ""))
        alias = _extract_text(_pick_first(src, KEY_PROFILE_ALIAS_CANDIDATES, ""))
        phone = _extract_text(_pick_first(src, KEY_PROFILE_PHONE_CANDIDATES, ""))

        if wxid:
            self.wxid = str(wxid)
        if nickname:
            self.nickname = nickname
        if alias:
            self.alias = alias
        if phone:
            self.phone = phone

    async def get_profile(self) -> Dict[str, Any]:
        data = await self.call_path("/user/GetProfile", method="GET")
        profile = data if isinstance(data, dict) else {}

        if "userInfo" not in profile:
            profile = {
                "userInfo": {
                    "UserName": profile.get("Wxid") or profile.get("wxid") or self.wxid,
                    "NickName": {"string": profile.get("NickName") or profile.get("nickname") or self.nickname},
                    "Alias": profile.get("Alias") or profile.get("alias") or self.alias,
                    "BindMobile": {"string": profile.get("BindMobile") or profile.get("phone") or self.phone},
                }
            }

        self._apply_profile(profile)
        return profile

    async def heartbeat(self) -> bool:
        return await self.is_logged_in(self.wxid)

    async def start_auto_heartbeat(self) -> bool:
        return await self.is_logged_in(self.wxid)

    async def stop_auto_heartbeat(self) -> bool:
        return True

    async def get_cached_info(self, wxid: str = "") -> Dict[str, Any]:
        return {}

    async def twice_login(self, wxid: str = "") -> bool:
        return False

    async def awaken_login(self, wxid: str = "") -> str:
        """唤醒登录（尽量免扫码）并返回可能的 uuid/url。"""
        try:
            active_key = self._resolve_active_key() or self.admin_key
            if not active_key:
                return ""

            device_type = str(getattr(self, "device_type", "") or "").strip().lower()
            login_device = "mac" if device_type == "mac" else "ipad"

            payload = {"IpadOrmac": login_device, "Check": False}
            data = await self.request("/login/WakeUpLogin", method="POST", body=payload, key=active_key)

            # 兼容 Data/顶层字段
            src = data.get("Data") if isinstance(data, dict) else None
            if not isinstance(src, dict):
                src = data if isinstance(data, dict) else {}

            token_key = _pick_first(src, KEY_TOKEN_CANDIDATES, "") or _pick_first(data, KEY_TOKEN_CANDIDATES, "")
            poll_key = _pick_first(src, KEY_POLL_CANDIDATES, "") or _pick_first(data, KEY_POLL_CANDIDATES, "")
            if token_key:
                self.token_key = str(token_key)
            if poll_key:
                self.poll_key = str(poll_key)

            candidate = _pick_first(src, ("Uuid", "uuid", "Url", "url"), "") or _pick_first(data, ("Uuid", "uuid", "Url", "url"), "")
            return str(candidate or "")
        except Exception as error:
            logger.debug("869 唤醒登录失败: {}", error)
            return ""

    async def try_wakeup_login(self, *, attempts: int = 6, interval_seconds: float = 2.0) -> bool:
        """在已存在 token_key/auth_key 的情况下尝试唤醒登录，避免触发扫码流程。"""
        active_key = self._resolve_active_key() or self.admin_key
        if not active_key:
            return False

        _ = await self.awaken_login(self.wxid)

        for _ in range(max(1, int(attempts))):
            try:
                if await self.is_logged_in(self.wxid or None):
                    await self.get_profile()
                    return True
            except Exception:
                pass
            await asyncio.sleep(interval_seconds)
        return False

    async def log_out(self) -> bool:
        key = self._resolve_active_key()
        if not key:
            return False
        try:
            await self.request("/login/LogOut", method="GET", key=key)
            self.wxid = ""
            return True
        except Exception:
            return False

    async def get_contact(self, wxid: Union[str, Iterable[str]]) -> Union[Dict[str, Any], list[Dict[str, Any]]]:
        details = await self.get_contract_detail(wxid)
        if isinstance(wxid, str):
            return details[0] if details else {}
        return details

    @staticmethod
    def _extract_contact_username(item: Dict[str, Any]) -> str:
        return _extract_text(
            _pick_first(
                item,
                (
                    "UserName",
                    "Username",
                    "userName",
                    "user_name",
                    "Wxid",
                    "wxid",
                    "FromUserName",
                ),
                "",
            )
        )

    @classmethod
    def _normalize_contract_detail_item(cls, item: Dict[str, Any]) -> Dict[str, Any]:
        """将联系人详情中的文本字段统一补齐为 {string: ...} 结构，兼容旧调用方。"""
        if not isinstance(item, dict):
            return item

        for key in ("NickName", "Remark", "DisplayName", "Signature"):
            value = item.get(key)
            if not isinstance(value, dict):
                continue
            if "string" in value and value.get("string") not in (None, ""):
                continue
            text = _extract_text(value, "")
            if text:
                value["string"] = text
        return item

    @classmethod
    def _normalize_contract_list_payload(cls, payload: Any) -> Dict[str, Any]:
        """将 /friend/GetContactList 的返回归一为旧 Client 约定字段。

        旧约定（部分调用方依赖）：
        - ContactUsernameList: list[str]
        - CurrentWxcontactSeq: int
        - CurrentChatroomContactSeq: int
        """
        if isinstance(payload, list):
            contact_list = [x for x in payload if isinstance(x, dict)]
            return {
                "ContactList": contact_list,
                "ContactUsernameList": [cls._extract_contact_username(x) for x in contact_list if cls._extract_contact_username(x)],
                "CurrentWxcontactSeq": 0,
                "CurrentChatroomContactSeq": 0,
            }

        if not isinstance(payload, dict):
            return {"ContactUsernameList": [], "CurrentWxcontactSeq": 0, "CurrentChatroomContactSeq": 0}

        result: Dict[str, Any] = dict(payload)

        # seq 字段：869 Swagger 使用 CurrentChatRoomContactSeq（R 大写），旧逻辑常用 CurrentChatroomContactSeq
        if "CurrentChatroomContactSeq" not in result and "CurrentChatRoomContactSeq" in result:
            result["CurrentChatroomContactSeq"] = result.get("CurrentChatRoomContactSeq")
        if "CurrentChatRoomContactSeq" not in result and "CurrentChatroomContactSeq" in result:
            result["CurrentChatRoomContactSeq"] = result.get("CurrentChatroomContactSeq")

        # 联系人列表：可能是 ContactUsernameList 或 ContactList
        usernames = result.get("ContactUsernameList")
        if not isinstance(usernames, list):
            contact_list = result.get("ContactList")
            if isinstance(contact_list, list):
                derived = []
                for item in contact_list:
                    if not isinstance(item, dict):
                        continue
                    wxid = cls._extract_contact_username(item)
                    if wxid:
                        derived.append(wxid)
                result["ContactUsernameList"] = derived
            else:
                result["ContactUsernameList"] = []

        # 兜底 seq 字段
        if "CurrentWxcontactSeq" not in result:
            result["CurrentWxcontactSeq"] = 0
        if "CurrentChatroomContactSeq" not in result:
            result["CurrentChatroomContactSeq"] = 0

        return result

    async def get_contract_detail(self, wxid: Union[str, Iterable[str]], chatroom: str = "") -> list[Dict[str, Any]]:
        usernames = [wxid] if isinstance(wxid, str) else [item for item in wxid]
        payload = {
            "UserNames": usernames,
            "RoomWxIDList": [chatroom] if chatroom else [],
        }

        data = await self.call_path("/friend/GetContactDetailsList", body=payload)
        if isinstance(data, dict):
            for key in ("ContactList", "List", "Items", "Data"):
                value = data.get(key)
                if isinstance(value, list):
                    return [self._normalize_contract_detail_item(item) for item in value if isinstance(item, dict)]
        if isinstance(data, list):
            return [self._normalize_contract_detail_item(item) for item in data if isinstance(item, dict)]
        return []

    async def get_contract_list(self, wx_seq: int = 0, chatroom_seq: int = 0) -> Dict[str, Any]:
        payload = {
            "CurrentWxcontactSeq": wx_seq,
            "CurrentChatRoomContactSeq": chatroom_seq,
        }
        data = await self.call_path("/friend/GetContactList", body=payload)
        return self._normalize_contract_list_payload(data)

    async def get_total_contract_list(
        self,
        wx_seq: int = 0,
        chatroom_seq: int = 0,
        offset: int = 0,
        limit: int = 0,
    ) -> Dict[str, Any]:
        payload = {
            "CurrentWxcontactSeq": wx_seq,
            "CurrentChatRoomContactSeq": chatroom_seq,
            "Offset": offset,
            "Limit": limit,
        }
        data = await self.call_path("/friend/GetContactList", body=payload)
        return self._normalize_contract_list_payload(data)

    async def get_nickname(self, wxid: Union[str, list[str]]) -> Union[str, list[str]]:
        details = await self.get_contract_detail(wxid)

        def _extract_nickname(item: Dict[str, Any]) -> str:
            return _extract_text(
                _pick_first(
                    item,
                    ("NickName", "nickname", "DisplayName", "display_name", "Remark", "remark"),
                    "",
                )
            )

        if isinstance(wxid, str):
            return _extract_nickname(details[0]) if details else ""

        detail_map: Dict[str, str] = {}
        for item in details:
            key = self._extract_contact_username(item)
            if key and key not in detail_map:
                detail_map[key] = _extract_nickname(item)

        result: list[str] = []
        for item in wxid:
            result.append(detail_map.get(str(item), ""))
        return result

    async def get_chatroom_member_list(self, group_wxid: str) -> list[Dict[str, Any]]:
        # 869 Swagger: /group/GetChatroomMemberDetail（注意 room 大小写）
        detail_data = await self.call_path("/group/GetChatroomMemberDetail", body={"ChatRoomName": group_wxid})
        if isinstance(detail_data, dict):
            new_data = detail_data.get("NewChatroomData")
            if isinstance(new_data, dict):
                members = new_data.get("ChatRoomMember")
                if isinstance(members, list):
                    return members
            members = detail_data.get("ChatRoomMember") or detail_data.get("MemberList")
            if isinstance(members, list):
                return members

        # 兼容兜底：部分实现仍从 GetChatRoomInfo 返回成员列表
        payload = {"ChatRoomWxIdList": [group_wxid]}
        data = await self.call_path("/group/GetChatRoomInfo", body=payload)
        if isinstance(data, dict):
            members = data.get("MemberList") or data.get("ChatRoomMemberList")
            if isinstance(members, list):
                return members
            if isinstance(data.get("ChatRoomInfo"), list):
                room_info = data.get("ChatRoomInfo")[0] if data.get("ChatRoomInfo") else {}
                room_members = room_info.get("MemberList")
                if isinstance(room_members, list):
                    return room_members
        return []

    async def get_chatroom_members(self, group_wxid: str) -> list[Dict[str, Any]]:
        return await self.get_chatroom_member_list(group_wxid)

    async def add_friend(self, wxid: str, verify_msg: str = "你好") -> Dict[str, Any]:
        payload = {"UserName": wxid, "Tg": verify_msg, "FromScene": 0}
        data = await self.call_path("/friend/VerifyUser", body=payload)
        return data if isinstance(data, dict) else {}

    async def delete_friend(self, wxid: str) -> bool:
        payload = {"Wxid": wxid}
        await self.call_path("/friend/DelContact", body=payload)
        return True

    async def get_friends(self) -> list[Dict[str, Any]]:
        data = await self.call_path(
            "/friend/GetContactList",
            body={"CurrentWxcontactSeq": 0, "CurrentChatRoomContactSeq": 0},
        )
        if isinstance(data, dict) and isinstance(data.get("ContactList"), list):
            return data["ContactList"]
        if isinstance(data, list):
            return data
        return []

    def _coerce_binary_to_base64(self, payload: Union[str, bytes, os.PathLike]) -> str:
        if isinstance(payload, bytes):
            return base64.b64encode(payload).decode()

        if isinstance(payload, os.PathLike):
            with open(payload, "rb") as file:
                return base64.b64encode(file.read()).decode()

        if isinstance(payload, str):
            if os.path.exists(payload):
                with open(payload, "rb") as file:
                    return base64.b64encode(file.read()).decode()
            if _looks_like_base64(payload):
                return payload
            return base64.b64encode(payload.encode("utf-8")).decode()

        raise ValueError("不支持的二进制数据类型")

    @staticmethod
    def _extract_send_tuple(data: Any) -> Tuple[int, int, int]:
        now = int(time.time())

        # 869 的部分接口返回 Data 为 list，元素中包含 resp.chat_send_ret_list
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                resp = first.get("resp")
                if isinstance(resp, dict):
                    chat_list = resp.get("chat_send_ret_list")
                    if isinstance(chat_list, list) and chat_list:
                        data = chat_list[0]
                    else:
                        data = first
                else:
                    data = first

        if not isinstance(data, dict):
            return 0, now, 0

        candidate = data
        if isinstance(data.get("List"), list) and data.get("List"):
            candidate = data["List"][0]

        # 兼容 resp.chat_send_ret_list 结构
        if isinstance(candidate.get("resp"), dict):
            chat_list = candidate["resp"].get("chat_send_ret_list")
            if isinstance(chat_list, list) and chat_list:
                candidate = chat_list[0] if isinstance(chat_list[0], dict) else candidate

        client_msg_id = _safe_int(
            _pick_first(
                candidate,
                KEY_SEND_CLIENT_MSG_ID_CANDIDATES,
                _pick_first(data, KEY_SEND_CLIENT_MSG_ID_CANDIDATES, 0),
            ),
            0,
        )
        create_time = _safe_int(
            _pick_first(candidate, KEY_SEND_CREATE_TIME_CANDIDATES, now),
            now,
        )
        new_msg_id = _safe_int(
            _pick_first(
                candidate,
                KEY_SEND_NEW_MSG_ID_CANDIDATES,
                _pick_first(data, KEY_SEND_NEW_MSG_ID_CANDIDATES, 0),
            ),
            0,
        )
        return client_msg_id, create_time, new_msg_id

    async def send_text_message(self, wxid: str, content: str, at: Union[list[str], str] = "") -> Tuple[int, int, int]:
        if self._should_route_via_reply_router(wxid):
            return await self.reply_router.send_text(wxid, content, at)

        at_list: list[str] = []
        if isinstance(at, str) and at:
            at_list = [item for item in at.split(",") if item]
        elif isinstance(at, list):
            at_list = at

        payload = {
            "MsgItem": [
                {
                    "ToUserName": wxid,
                    "MsgType": 1,
                    "TextContent": content,
                    "AtWxIDList": at_list,
                }
            ]
        }
        data = await self.call_path("/message/SendTextMessage", body=payload)
        return self._extract_send_tuple(data)

    async def send_at_message(self, wxid: str, content: str, at: list[str]) -> Tuple[int, int, int]:
        return await self.send_text_message(wxid, content, at)

    async def send_image_message(self, wxid: str, image: Union[str, bytes, os.PathLike]) -> Dict[str, Any]:
        if self._should_route_via_reply_router(wxid):
            return await self.reply_router.send_image(wxid, image)

        image_base64 = self._coerce_binary_to_base64(image)
        payload = {
            "MsgItem": [
                {
                    "ToUserName": wxid,
                    "MsgType": 2,
                    "ImageContent": image_base64,
                }
            ]
        }

        try:
            data = await self.call_path("/message/SendImageMessage", body=payload)
        except Exception:
            data = await self.call_path("/message/SendImageNewMessage", body=payload)

        return data if isinstance(data, dict) else {"Data": data}

    async def send_voice_message(
        self,
        wxid: str,
        voice: Union[str, bytes, os.PathLike],
        format: str = "amr",
    ) -> Tuple[int, int, int]:
        if self._should_route_via_reply_router(wxid):
            return await self.reply_router.send_voice(wxid, voice, format)

        voice_base64 = self._coerce_binary_to_base64(voice)
        format_mapping = {"amr": 0, "wav": 4, "mp3": 4}
        payload = {
            "ToUserName": wxid,
            "VoiceData": voice_base64,
            "VoiceFormat": format_mapping.get(format.lower(), 0),
            "VoiceSecond": 2,
            "VoiceSecond,": 2,
        }

        data = await self.call_path("/message/SendVoice", body=payload)
        return self._extract_send_tuple(data if isinstance(data, dict) else {})

    async def send_video_message(
        self,
        wxid: str,
        video: Union[str, bytes, os.PathLike],
        image: Union[str, bytes, os.PathLike] = "",
    ) -> Dict[str, Any]:
        if self._should_route_via_reply_router(wxid):
            return await self.reply_router.send_video(wxid, video, image)

        video_bytes = base64.b64decode(self._coerce_binary_to_base64(video))
        thumb_bytes = base64.b64decode(self._coerce_binary_to_base64(image)) if image else b""

        payload = {
            "ToUserName": wxid,
            "VideoData": list(video_bytes),
            "ThumbData": list(thumb_bytes),
        }
        data = await self.call_path("/message/CdnUploadVideo", body=payload)
        return data if isinstance(data, dict) else {"Data": data}

    async def send_file_message(
        self,
        wxid: str,
        file_data: Union[str, bytes, os.PathLike],
        file_name: str = "",
    ) -> Dict[str, Any]:
        if self._should_route_via_reply_router(wxid):
            # ReplyRouter 目前没有 file 类型，避免误发为图片：退化为文本提示交由上层处理
            client_msg_id, create_time, new_msg_id = await self.reply_router.send_text(
                wxid,
                f"[file] {file_name or 'file'}",
                None,
            )
            return {"client_msg_id": client_msg_id, "create_time": create_time, "new_msg_id": new_msg_id}

        file_info = await self.upload_file(file_data)
        media_id = (
            file_info.get("mediaId")
            or file_info.get("MediaId")
            or file_info.get("attachId")
            or file_info.get("AttachId")
            or ""
        )
        total_len = _safe_int(file_info.get("totalLen") or file_info.get("TotalLen") or 0, 0)

        resolved_name = (file_name or file_info.get("fileName") or file_info.get("FileName") or "file").strip()
        file_extension = ""
        if "." in resolved_name:
            file_extension = resolved_name.rsplit(".", 1)[-1].strip().lower()

        xml = (
            "<appmsg appid=\"\" sdkver=\"0\">"
            f"<title>{resolved_name}</title><des></des><action></action>"
            "<type>6</type><showtype>0</showtype><content></content><url></url>"
            "<appattach>"
            f"<totallen>{total_len}</totallen>"
            f"<attachid>{media_id}</attachid>"
            f"<fileext>{file_extension}</fileext>"
            "</appattach><md5></md5></appmsg>"
        )
        await self.send_app_message(wxid, xml, 6)
        return {"mediaId": media_id, "totalLen": total_len, "fileName": resolved_name}

    async def send_link_message(
        self,
        wxid: str,
        url: str,
        title: str = "",
        description: str = "",
        thumb_url: str = "",
    ) -> Tuple[int, int, int]:
        if self._should_route_via_reply_router(wxid):
            return await self.reply_router.send_link(wxid, url, title, description, thumb_url)

        xml_payload = (
            "<msg><appmsg appid='' sdkver='0'>"
            f"<title>{title}</title><des>{description}</des><url>{url}</url>"
            f"<thumburl>{thumb_url}</thumburl><type>5</type></appmsg></msg>"
        )
        payload = {
            "AppList": [
                {
                    "ToUserName": wxid,
                    "ContentType": 5,
                    "ContentXML": xml_payload,
                }
            ]
        }
        data = await self.call_path("/message/SendAppMessage", body=payload)
        return self._extract_send_tuple(data if isinstance(data, dict) else {})

    async def send_app_message(self, wxid: str, xml: str, type: int) -> Tuple[int, int, int]:
        """兼容旧插件：发送 appmsg(xml)。"""
        if self._should_route_via_reply_router(wxid):
            return await self.reply_router.send_text(wxid, xml, None)

        payload = {
            "AppList": [
                {
                    "ToUserName": wxid,
                    "ContentType": int(type),
                    "ContentXML": xml,
                }
            ]
        }
        data = await self.call_path("/message/SendAppMessage", body=payload)
        return self._extract_send_tuple(data if isinstance(data, dict) else {})

    async def send_card_message(
        self,
        wxid: str,
        card_wxid: str,
        card_nickname: str,
        card_alias: str = "",
        card_flag: int = 0,
    ) -> Tuple[int, int, int]:
        """兼容旧插件：分享名片消息。"""
        if self._should_route_via_reply_router(wxid):
            return await self.reply_router.send_text(wxid, f"[card] {card_nickname}({card_wxid})", None)

        payload = {
            "ToUserName": wxid,
            "CardWxId": card_wxid,
            "CardNickName": card_nickname,
            "CardAlias": card_alias,
            "CardFlag": int(card_flag),
        }
        data = await self.call_path("/message/ShareCardMessage", body=payload)
        return self._extract_send_tuple(data if isinstance(data, dict) else {})

    async def send_emoji_message(self, wxid: str, md5: str, total_length: int) -> Dict[str, Any]:
        """兼容旧插件：发送表情（md5 + size）。"""
        if self._should_route_via_reply_router(wxid):
            client_msg_id, create_time, new_msg_id = await self.reply_router.send_text(
                wxid,
                f"[emoji] md5={md5} size={total_length}",
                None,
            )
            return {"client_msg_id": client_msg_id, "create_time": create_time, "new_msg_id": new_msg_id}

        payload = {"EmojiList": [{"ToUserName": wxid, "EmojiMd5": md5, "EmojiSize": int(total_length)}]}
        data = await self.call_path("/message/SendEmojiMessage", body=payload)
        return data if isinstance(data, dict) else {"Data": data}

    async def _send_cdn_file_msg(self, wxid: str, xml: str) -> Dict[str, Any]:
        """兼容旧插件：以 XML 发送文件（appmsg type=6）。"""
        _client_msg_id, _create_time, _new_msg_id = await self.send_app_message(wxid, xml, 6)
        return {"success": True, "client_msg_id": _client_msg_id, "create_time": _create_time, "new_msg_id": _new_msg_id}

    async def revoke_message(
        self,
        wxid: str,
        client_msg_id: int,
        create_time: int,
        new_msg_id: int,
    ) -> bool:
        payload = {
            "ToUserName": wxid,
            "ClientMsgId": client_msg_id,
            "CreateTime": create_time,
            "NewMsgId": str(new_msg_id),
            "ClientImgIdStr": str(client_msg_id),
            "IsImage": False,
        }
        await self.call_path("/message/RevokeMsg", body=payload)
        return True

    @staticmethod
    def _coerce_base64_payload(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (bytes, bytearray)):
            try:
                return bytes(value).decode("utf-8", errors="ignore").strip()
            except Exception:
                return ""
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()

    @classmethod
    def _extract_base64_from_payload(cls, payload: Any) -> str:
        if isinstance(payload, str):
            return cls._coerce_base64_payload(payload)
        if not isinstance(payload, dict):
            return ""

        direct_candidates = (
            "FileData",
            "fileData",
            "Data",
            "data",
            "Buffer",
            "buffer",
            "Image",
            "image",
            "VideoData",
            "video",
            "voice",
            "VoiceData",
        )
        for key in direct_candidates:
            if key in payload and payload.get(key) not in (None, ""):
                value = payload.get(key)
                if isinstance(value, dict):
                    nested = cls._extract_base64_from_payload(value)
                    if nested:
                        return nested
                else:
                    return cls._coerce_base64_payload(value)

        # 常见旧格式：{"data": {"buffer": "..."}}
        nested_data = payload.get("data")
        if isinstance(nested_data, dict):
            nested = cls._extract_base64_from_payload(nested_data)
            if nested:
                return nested

        return ""

    async def _send_cdn_download(self, aes_key: str, file_url: str, file_type: int) -> str:
        """调用 869 Swagger 的 /message/SendCdnDownload，返回 base64(FileData)。

        file_type:
          - 2: 图片高清
          - 3: 图片缩略
          - 5: 文件/附件
        """
        if not aes_key or not file_url:
            return ""
        payload = {"AesKey": aes_key, "FileURL": file_url, "FileType": int(file_type)}
        data = await self.call_path("/message/SendCdnDownload", body=payload)
        return self._extract_base64_from_payload(data)

    async def get_msg_image(self, aeskey: str, cdnmidimgurl: str) -> bytes:
        try:
            base64_payload = await self._send_cdn_download(aeskey, cdnmidimgurl, 2)
            if base64_payload:
                return base64.b64decode(base64_payload)
        except Exception:
            pass

        try:
            base64_payload = await self._send_cdn_download(aeskey, cdnmidimgurl, 3)
            if base64_payload:
                return base64.b64decode(base64_payload)
        except Exception:
            pass

        return b""

    async def download_image(self, aeskey: str, cdnmidimgurl: str) -> str:
        try:
            base64_payload = await self._send_cdn_download(aeskey, cdnmidimgurl, 2)
            if base64_payload:
                return base64_payload
        except Exception:
            pass

        image_bytes = await self.get_msg_image(aeskey, cdnmidimgurl)
        return base64.b64encode(image_bytes).decode() if image_bytes else ""

    async def upload_file(self, file_data: Union[str, bytes, os.PathLike]) -> Dict[str, Any]:
        """上传文件并返回 mediaId 等信息（优先走 869 Swagger 接口）。"""
        file_base64 = self._coerce_binary_to_base64(file_data)
        response = await self.request("/other/UploadAppAttach", method="POST", body={"fileData": file_base64})
        if not isinstance(response, dict):
            return {"raw": response}
        data = response.get("Data") if isinstance(response.get("Data"), dict) else response
        if not isinstance(data, dict):
            return {"raw": data}
        # 归一化字段，便于旧插件使用
        normalized = dict(data)
        if "mediaId" not in normalized and "MediaId" in normalized:
            normalized["mediaId"] = normalized.get("MediaId")
        if "totalLen" not in normalized and "TotalLen" in normalized:
            normalized["totalLen"] = normalized.get("TotalLen")
        return normalized

    async def download_voice(self, msg_id: Union[str, int], voiceurl: str, length: int) -> str:
        old_payload = {
            "Wxid": self.wxid,
            "MsgId": str(msg_id),
            "Voiceurl": voiceurl,
            "Length": length,
        }
        try:
            response = await self.request("/api/Tools/DownloadVoice", method="POST", body=old_payload)
            if isinstance(response, dict):
                data = response.get("Data", {})
                if isinstance(data, dict):
                    nested = data.get("data", {}) if isinstance(data.get("data"), dict) else data
                    voice_data = nested.get("buffer")
                    if isinstance(voice_data, str) and voice_data:
                        return voice_data
        except Exception:
            pass

        fallback_payload = {
            "ToUserName": self.wxid,
            "NewMsgId": str(msg_id),
            "Bufid": voiceurl,
            "Length": int(length),
        }
        data = await self.call_path("/message/GetMsgVoice", body=fallback_payload)
        base64_payload = self._extract_base64_from_payload(data)
        return base64_payload or ""

    async def download_video(self, msg_id: Union[str, int]) -> str:
        old_payload = {"Wxid": self.wxid, "MsgId": msg_id}
        try:
            response = await self.request("/api/Tools/DownloadVideo", method="POST", body=old_payload)
            if isinstance(response, dict):
                data = response.get("Data", {})
                if isinstance(data, dict):
                    nested = data.get("data", {}) if isinstance(data.get("data"), dict) else data
                    video_data = nested.get("buffer")
                    if isinstance(video_data, str) and video_data:
                        return video_data
        except Exception:
            pass

        fallback_payload = {
            "MsgId": _safe_int(msg_id),
            "FromUserName": self.wxid,
            "ToUserName": self.wxid,
            "TotalLen": 0,
            "CompressType": 0,
            "Section": {"DataLen": 0, "StartPos": 0},
        }
        data = await self.call_path("/message/GetMsgVideo", body=fallback_payload)
        base64_payload = self._extract_base64_from_payload(data)
        return base64_payload or ""

    async def download_attach(self, attach_id: str) -> str:
        file_url = ""
        aes_key = ""

        if isinstance(attach_id, str) and attach_id.startswith("@cdn_"):
            raw = attach_id[len("@cdn_") :]
            parts = [p for p in raw.split("_") if p]
            if len(parts) >= 3:
                aes_key = parts[-2]
                file_url = "_".join(parts[:-2])

        if aes_key and file_url:
            try:
                base64_payload = await self._send_cdn_download(aes_key, file_url, 5)
                if base64_payload:
                    return base64_payload
            except Exception:
                pass

        try:
            response = await self.request(
                "/api/Tools/DownloadFile",
                method="POST",
                body={"Wxid": self.wxid, "AttachId": attach_id},
                timeout=300,
            )
            if isinstance(response, dict):
                data = response.get("Data", {})
                if isinstance(data, dict):
                    nested = data.get("data", {}) if isinstance(data.get("data"), dict) else data
                    buffer_data = nested.get("buffer")
                    if isinstance(buffer_data, str) and buffer_data:
                        return buffer_data
        except Exception:
            pass

        return ""

    @staticmethod
    async def silk_base64_to_wav_byte(silk_base64: str) -> bytes:
        return await pysilk.async_decode(base64.b64decode(silk_base64), to_wav=True)

    async def get_pyq_list(self, wxid: str = "", max_id: int = 0) -> Dict[str, Any]:
        payload = {"Towxid": wxid or self.wxid, "Maxid": max_id}
        data = await self.call_path("/sns/GetSnsSync", body=payload)
        return data if isinstance(data, dict) else {}

    async def get_pyq_detail(self, wxid: str = "", Towxid: str = "", max_id: int = 0) -> Dict[str, Any]:
        payload = {"Towxid": Towxid or wxid or self.wxid, "Maxid": max_id}
        data = await self.call_path("/sns/SendSnsUserPage", body=payload)
        return data if isinstance(data, dict) else {}

    async def put_pyq_comment(
        self,
        wxid: str = "",
        id: str = "",
        Content: str = "",
        type: int = 0,
        ReplyCommnetId: int = 0,
    ) -> Dict[str, Any]:
        payload = {
            "ID": id,
            "Towxid": wxid or self.wxid,
            "Content": Content,
            "Type": type,
            "ReplyCommnetId": ReplyCommnetId,
        }
        data = await self.call_path("/sns/SendSnsComment", body=payload)
        return data if isinstance(data, dict) else {}

    async def pyq_sync(self, wxid: str = "") -> Dict[str, Any]:
        data = await self.call_path("/sns/GetSnsSync", body={"Towxid": wxid or self.wxid})
        return data if isinstance(data, dict) else {}

    @staticmethod
    def byte_to_base64(data: bytes) -> str:
        return base64.b64encode(data).decode("utf-8")

    @staticmethod
    def base64_to_byte(base64_str: str) -> bytes:
        payload = base64_str.split(",", 1)[1] if "," in base64_str else base64_str
        return base64.b64decode(payload)

    @staticmethod
    def base64_to_file(base64_str: str, file_name: str, file_path: str) -> bool:
        try:
            os.makedirs(file_path, exist_ok=True)
            payload = base64_str.split(",", 1)[1] if "," in base64_str else base64_str
            full_path = os.path.join(file_path, file_name)
            with open(full_path, "wb") as file:
                file.write(base64.b64decode(payload))
            return True
        except Exception:
            return False

    @staticmethod
    def file_to_base64(file_path: str) -> str:
        with open(file_path, "rb") as file:
            return base64.b64encode(file.read()).decode()

    @staticmethod
    def wav_byte_to_amr_byte(wav_byte: bytes) -> bytes:
        audio = AudioSegment.from_wav(io.BytesIO(wav_byte))
        audio = audio.set_frame_rate(8000).set_channels(1)
        output = io.BytesIO()
        audio.export(output, format="amr")
        return output.getvalue()

    @staticmethod
    async def wav_byte_to_silk_byte(wav_byte: bytes) -> bytes:
        audio = AudioSegment.from_wav(io.BytesIO(wav_byte))
        return await pysilk.async_encode(audio.raw_data, data_rate=audio.frame_rate, sample_rate=audio.frame_rate)

    @staticmethod
    async def wav_byte_to_silk_base64(wav_byte: bytes) -> str:
        return base64.b64encode(await Client869.wav_byte_to_silk_byte(wav_byte)).decode()
