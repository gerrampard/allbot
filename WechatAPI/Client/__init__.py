"""
@input: 旧协议 mixins（login/message/friend/chatroom/tool/user/pyq）与本地 contacts.db
@output: WechatAPIClient 统一旧协议客户端（供框架/插件调用）
@position: Legacy Client 聚合入口（Facade），在 869 以外协议下作为主客户端实现
@auto-doc: Update header and folder INDEX.md when this file changes
"""

from WechatAPI.errors import *
from .base import WechatAPIClientBase, Proxy, Section
from .chatroom import ChatroomMixin
from .friend import FriendMixin
from .hongbao import HongBaoMixin
from .login import LoginMixin
from .message import MessageMixin
from .protect import protector
from .tool import ToolMixin
from .tool_extension import ToolExtensionMixin
from .user import UserMixin
from .pyq import PyqMixin
import sqlite3
import os
from loguru import logger

class WechatAPIClient(LoginMixin, MessageMixin, FriendMixin, ChatroomMixin, UserMixin,
                      ToolMixin, ToolExtensionMixin, HongBaoMixin, PyqMixin):

    # 这里都是需要结合多个功能的方法
    
    def __init__(self, ip: str, port: int, protocol_version=None):
        super().__init__(ip, port)
        self.protocol_version = protocol_version
        self.contacts_db = None
    
    def get_contacts_db(self):
        """连接到contacts.db数据库"""
        if self.contacts_db is None:
            try:
                # 适配不同环境的路径
                if os.path.exists("/app/database"):
                    db_path = "/app/database/contacts.db"
                else:
                    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                    db_path = os.path.join(base_dir, "database", "contacts.db")
                
                self.contacts_db = sqlite3.connect(db_path)
                logger.info(f"联系人数据库初始化成功: {db_path}")
            except Exception as e:
                logger.error(f"初始化联系人数据库失败: {str(e)}")
                self.contacts_db = None
        return self.contacts_db
    
    def get_local_nickname(self, wxid: str, chatroom_id: str = None):
        """从本地contacts.db获取用户昵称"""
        if not wxid:
            return None
        
        # 从contacts.db的group_members表获取成员信息
        if chatroom_id and "@chatroom" in chatroom_id:
            try:
                conn = self.get_contacts_db()
                if conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                    SELECT member_wxid, nickname, display_name FROM group_members 
                    WHERE group_wxid = ? AND member_wxid = ?
                    """, (chatroom_id, wxid))
                    
                    result = cursor.fetchone()
                    if result:
                        # 优先使用display_name，如果为空再使用nickname
                        if result[2]:  # display_name
                            return result[2]
                        elif result[1]:  # nickname
                            return result[1]
            except Exception as e:
                logger.error(f"从contacts.db获取昵称失败: {str(e)}")
        
        return None

    async def send_at_message(self, wxid: str, content: str, at: list[str]) -> tuple[int, int, int]:
        """发送@消息

        Args:
            wxid (str): 接收人
            content (str): 消息内容
            at (list[str]): 要@的用户ID列表

        Returns:
            tuple[int, int, int]: 包含以下三个值的元组:
                - ClientMsgid (int): 客户端消息ID
                - CreateTime (int): 创建时间
                - NewMsgId (int): 新消息ID

        Raises:
            UserLoggedOut: 用户未登录时抛出
            BanProtection: 新设备登录4小时内操作时抛出
        """
        if not self.wxid:
            raise UserLoggedOut("请先登录")
        elif not self.ignore_protect and protector.check(14400):
            raise BanProtection("风控保护: 新设备登录后4小时内请挂机")

        output = ""
        for id in at:
            # 优先从contacts.db获取昵称
            local_nickname = self.get_local_nickname(id, wxid)
            if local_nickname:
                nickname = local_nickname
                logger.debug(f"使用本地数据库昵称 @{local_nickname}")
            else:
                # 如果本地数据库没有，再通过API获取
                nickname = await self.get_nickname(id)
                logger.debug(f"使用API昵称 @{nickname}")
            
            output += f"@{nickname}\u2005"

        output += content

        return await self.send_text_message(wxid, output, at)

    async def send_text(self, wxid: str, content: str, at="") -> tuple[int, int, int]:
        """兼容 869/插件：send_text -> send_text_message。"""
        return await self.send_text_message(wxid, content, at)

    async def send_pat(self, chatroom_wxid: str, to_wxid: str, scene: int = 0):
        """869 专属：群拍一拍（旧协议不支持）。"""
        raise NotImplementedError("send_pat 仅在 869 客户端可用")

    async def verify_code(self, code: str, *, data62: str = "", ticket: str = "", key: str = ""):
        """869 专属：验证码验证（旧协议不支持）。"""
        raise NotImplementedError("verify_code 仅在 869 客户端可用")

    async def verify_code_slide(
        self,
        slide_ticket: str,
        rand_str: str,
        *,
        data62: str = "",
        ticket: str = "",
        key: str = "",
    ):
        """869 专属：滑块验证（旧协议不支持）。"""
        raise NotImplementedError("verify_code_slide 仅在 869 客户端可用")

    async def ensure_auth_key(self):
        """869 专属：确保授权码（旧协议不支持）。"""
        raise NotImplementedError("ensure_auth_key 仅在 869 客户端可用")

    async def try_wakeup_login(self, *, attempts: int = 6, interval_seconds: float = 2.0) -> bool:
        """869 专属：免扫码唤醒登录（旧协议不支持）。"""
        raise NotImplementedError("try_wakeup_login 仅在 869 客户端可用")

    async def request(self, *args, **kwargs):
        """869 专属：Swagger 路径请求（旧协议不支持）。"""
        raise NotImplementedError("request 仅在 869 客户端可用")

    async def call_path(self, *args, **kwargs):
        """869 专属：Swagger 路径调用（旧协议不支持）。"""
        raise NotImplementedError("call_path 仅在 869 客户端可用")

    async def invoke(self, *args, **kwargs):
        """869 专属：Swagger 动态调用（旧协议不支持）。"""
        raise NotImplementedError("invoke 仅在 869 客户端可用")

    async def add_friend(self, wxid: str, verify_msg: str = "你好"):
        """869 专属：添加好友（旧协议请使用 SearchContact/VerifyUser 流程）。"""
        raise NotImplementedError("add_friend 仅在 869 客户端可用")

    async def delete_friend(self, wxid: str) -> bool:
        """869 专属：删除好友（旧协议可用 DelContact，但未统一封装为 delete_friend）。"""
        raise NotImplementedError("delete_friend 仅在 869 客户端可用")

    async def get_friends(self):
        """869 专属：获取好友列表（旧协议可用 GetContractList/GetTotalContractList）。"""
        raise NotImplementedError("get_friends 仅在 869 客户端可用")
        
    def __del__(self):
        """清理资源"""
        if hasattr(self, 'contacts_db') and self.contacts_db:
            try:
                self.contacts_db.close()
            except:
                pass
