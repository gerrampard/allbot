<!-- AUTO-DOC: Update me when files in this folder change -->

# plugins

插件集合：各子目录为一个独立插件，遵循 `PluginBase` 生命周期，提供扩展能力（群监控、欢迎语、提醒等）。

## Files

| File | Role | Function |
|------|------|----------|
| README.md | Doc | 插件总览与使用说明 |
| Claw/ | Plugin | OpenClaw 网关通信（WS 持久连接、触发词自动转发、RPC、runId 实时事件回推、媒体公网链接输出） |
| GroupMonitor/ | Plugin | 退群提醒（群成员快照对比，已适配复用 bot 客户端接口） |
| GroupWelcome/ | Plugin | 入群欢迎语（进群事件处理，使用框架方法发送卡片与取群成员头像） |
| Reminder/ | Plugin | 定时提醒 |
| Dify/ | Plugin | Dify 接入相关插件 |
| Protocol869Demo/ | Plugin | 869 客户端能力示例（拍一拍/撤回/二维码/标签/群信息/动态调用） |
| RevokeBotMessage/ | Plugin | 撤回机器人消息（引用机器人消息 + 发送“撤回”撤回引用消息；记录发送回执） |
