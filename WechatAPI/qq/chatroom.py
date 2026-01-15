import aiohttp

class QQChatroomMixin:
    async def get_group_list(self) -> dict:
        """获取QQ群列表"""
        async with aiohttp.ClientSession() as session:
            response = await session.get('http://localhost:5700/get_group_list')
            return await response.json()

    async def get_group_member_list(self, group_id: str) -> dict:
        """获取QQ群成员列表"""
        async with aiohttp.ClientSession() as session:
            params = {"group_id": group_id}
            response = await session.get('http://localhost:5700/get_group_member_list', params=params)
            return await response.json() 