<!-- AUTO-DOC: Update me when project structure or architecture changes -->

# Architecture

AllBot is a multi-protocol bot with a FastAPI admin server and a plugin/adapter ecosystem.
Runtime updates are handled by the admin update subsystem (download -> backup -> apply).
The adapter layer now includes QQ, Telegram, Web, Win, wx-filehelper-api, and ocwx clawbot bridges.
Adapters are started inside bot core initialization before message listening starts.
Wechat login runs asynchronously to avoid blocking adapter message ingestion.
Wechat protocol access is encapsulated in `WechatAPI/`, including a dedicated 869 client.
Plugin market backend now supports dual-source aggregation (v.sxkiss.top + xbot API) with dedupe by higher version.

- `adapter/INDEX.md`: Multi-platform adapters and queue bridge contracts
- `admin/INDEX.md`: Admin server, update pipeline, and related APIs
- `bot_core/INDEX.md`: Core orchestrator, login, and message pipeline
- `plugins/INDEX.md`: Built-in plugins and lifecycle integration points
- `WechatAPI/INDEX.md`: Wechat protocol clients (legacy + 869)
- `utils/INDEX.md`: Shared utilities (config, protocol mapping, etc.)
