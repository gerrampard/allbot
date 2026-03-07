<!-- AUTO-DOC: Update me when files in this folder change -->

# routes

管理后台路由模块：FastAPI 端点按功能拆分注册，提供系统/联系人/文件/插件等页面与 API；部分路由包含 869 专属能力（仅在 869 客户端可用）。

## Files

| File | Role | Function |
|------|------|----------|
| registry.py | Core | 统一注册所有路由模块与顺序 |
| pages.py | UI | 页面路由（index/qrcode/system 等） |
| system.py | API | 系统状态与信息 API |
| contacts.py | API | 联系人/群聊/成员相关 API（含批量详情缓存兜底、群成员列表/详情） |
| qrcode_routes.py | Login | 二维码页面与登录辅助 API（获取二维码、验证码提交、mac 拉码、卡密/代理重入 869 登录） |
| files.py | API | 文件上传/下载/列表 API |
| plugins.py | API | 插件管理与插件市场 API（双市场聚合、去重保留高版本、双源提交与失败重试） |
| message_routes.py | Compat | 旧前端兼容：`/api/send_message`、`/api/group/announcement`；并补充 869 专属：撤回、拍一拍、同步、二维码、标签、群信息、表情下载、动态调用等端点（全部做 869 协议校验） |
