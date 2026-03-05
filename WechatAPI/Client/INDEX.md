<!-- AUTO-DOC: Update me when files in this folder change -->

# Client

旧协议客户端聚合层：由多个 mixin 组合 `WechatAPIClient`，覆盖登录、消息、联系人、群聊、工具与朋友圈能力。

## Files

| File | Role | Function |
|------|------|----------|
| __init__.py | Entry | 聚合旧协议 mixin，补充 `send_at_message` 与 869 专属方法占位（非 869 客户端显式报不支持） |
| base.py | Base | 基础数据结构与通用错误处理（`Proxy/Section/error_handler`） |
| login.py | Auth | 登录、心跳与设备信息能力 |
| message.py | Message | 文本/图片/语音/视频/文件/链接/撤回等消息能力 |
| friend.py | Contact | 好友相关能力（同意、查询、详情等） |
| chatroom.py | Group | 群聊管理能力（成员、公告、二维码等） |
| tool.py | Tool | 下载/上传、转码、代理、步数等工具能力 |
| tool_extension.py | ToolExt | 工具扩展能力（如图片二进制下载） |
| user.py | User | 个人信息、二维码、标签等用户能力 |
| pyq.py | SNS | 朋友圈能力 |
| hongbao.py | Pay | 红包相关能力 |
