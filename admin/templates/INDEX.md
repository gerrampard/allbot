<!-- AUTO-DOC: Update me when files in this folder change -->

# templates

管理后台 Jinja2 页面模板目录：承载系统状态、联系人、插件与配置编辑等前端页面。

## Files

| File | Role | Function |
|------|------|----------|
| index.html | Dashboard | 首页总览与快捷操作（含系统配置弹窗编辑） |
| base.html | Layout | 管理后台基础布局与全局组件（含 Web 对话悬浮入口） |
| qrcode.html | Login | 微信登录二维码页面（自动刷新、验证码提交、卡密/代理补录并重入 869 流程） |
| contacts.html | UI | 联系人管理页面 |
| plugins.html | UI | 插件管理页面 |
| plugin_market.html | UI | 独立插件市场页面（分类/搜索/安装，提交插件弹窗） |
| settings.html | UI | 系统设置页（已改为 `main_config.toml` 原文编辑/保存） |
