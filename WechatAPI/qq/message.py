import aiohttp

class QQMessageMixin:
    async def send_text_message(self, user_id: str, content: str) -> dict:
        """发送QQ文本消息"""
        async with aiohttp.ClientSession() as session:
            json_param = {"user_id": user_id, "message": [{"type": "text", "data": {"text": content}}]}
            response = await session.post('http://localhost:5700/send_private_msg', json=json_param)
            return await response.json()

    async def send_group_message(self, group_id: str, content: str) -> dict:
        """发送QQ群文本消息"""
        async with aiohttp.ClientSession() as session:
            json_param = {"group_id": group_id, "message": [{"type": "text", "data": {"text": content}}]}
            response = await session.post('http://localhost:5700/send_group_msg', json=json_param)
            return await response.json() 