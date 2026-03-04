<!-- AUTO-DOC: Update me when files in this folder change -->

# WechatAPI

协议封装层：统一导出旧版 `Client` 与新增 `Client869`，并包含对应的服务端/错误定义。

## Files

| File | Role | Function |
|------|------|----------|
| __init__.py | Entry | 统一导出 `Client`、`Client869`、`Server`、`errors` |
| Client/ | Legacy Client | 旧协议客户端实现（mixins） |
| Client869/ | New Client | 869 协议全接口动态客户端与兼容层 |
| Server/ | Server | WechatAPI 服务端封装 |
| errors.py | Shared | 协议层异常定义 |
