<!-- AUTO-DOC: Update me when files in this folder change -->

# Client869

869 协议专用客户端实现：提供 Swagger 全接口动态调用，并通过兼容方法对接现有 bot_core/插件调用习惯。

## Files

| File | Role | Function |
|------|------|----------|
| __init__.py | Entry | 导出 `Client869` |
| client.py | Core | 869 动态接口调用、AuthKey 生成、登录（含 data62/ticket 验证码参数）；发送分流（wechat 直发/多平台入队）；媒体下载兼容（图片/附件通过 `/message/SendCdnDownload`）；补齐联系人接口兼容映射，并提供掉线后免扫码唤醒登录能力（`try_wakeup_login`） |
