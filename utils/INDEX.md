<!-- AUTO-DOC: Update me when files in this folder change -->

# utils

通用基础设施与业务工具集合。此次变更更新了协议配置与配置管理，支持 869 协议与 `admin-key` 配置读取。

## Files

| File | Role | Function |
|------|------|----------|
| config_manager.py | Config Core | TOML/环境变量加载与配置校验（新增 869/admin-key） |
| protocol_config.py | Protocol Map | 协议版本与 API 前缀映射（新增 869） |
| xybot/ | Domain | XYBot 业务域模块（联系人/路由/权限等，含 869 兼容联系人查询） |
| xybot_legacy.py | Legacy | 旧版消息处理实现（core 通过委托保持兼容；含图片/语音/文件下载与落盘到 files/；引用消息补齐 `Ats` 解析用于 AT 判定） |
