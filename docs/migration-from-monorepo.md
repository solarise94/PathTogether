# 从一体仓迁移

旧仓库同时包含 PathTogether 平台、HistoPilot Node sidecar 和 UI bundle。拆分后的数据归属如下：

| 旧数据 | 新归属 |
|---|---|
| `uploads/` | PathTogether |
| `shares.json` / PostgreSQL 业务表 | PathTogether |
| 用户、分享、标注、评论、audit | PathTogether |
| `ai_sessions/` | HistoPilot |
| AI 模型配置与 provider key | HistoPilot；兼容期可继续由平台网关注入 |
| `plugins/histopilot/ui/` | HistoPilot release bundle |

## 迁移顺序

1. 停止旧容器并备份 uploads、share data、PostgreSQL 与 AI sessions。
2. 启动 PathTogether，挂载原 uploads/share data，先验证人工读片与分享。
3. 把 `ai_sessions` 复制到 HistoPilot 的独立 `HISTOPILOT_SESSIONS_DIR`。
4. 安装 HistoPilot UI bundle，建立/轮换 installation credential。
5. 启动 HistoPilot 并验证旧会话恢复、region 读取与标注写回。
6. 稳定后删除旧的一体容器；不要删除备份。

旧 `/internal/ai/*` 与 `/api/ai/*` 仅用于兼容迁移，新集成应使用 `/api/plugin/v1`。
