import aiohttp

class QQFriendMixin:
    async def get_friend_list(self) -> dict:
        """获取QQ好友列表"""
        async with aiohttp.ClientSession() as session:
            response = await session.get('http://localhost:5700/get_friend_list')
            return await response.json() 