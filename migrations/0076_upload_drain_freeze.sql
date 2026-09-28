-- 0076：旧上传链路排空冻结清单（COS 统一上传 U4 检查点 A，
-- docs/cos-only-upload-agent-plan-20260928.md §5/§U4）。
--
-- 切换「排空版」（PT_UPLOAD_LEGACY_MODE=drain）时，运维先以
-- scripts/upload_drain.py freeze 对**切换前已持久化**的 V2 任务拍照：
-- 只有清单内的 upload_id 在 drain 模式下继续享受状态/续传/提交/取消；
-- 清单外（含切换后伪造/新出现）一律 410 upload_migration——不以客户端
-- 可提交的时间戳证明旧任务资格（§5：以冻结清单验证资格）。
--
-- V1（单请求 multipart）无持久任务号，drain 模式直接 410——在途请求必须
-- 在停写切换窗口内结束（边缘/nginx 停止转发后自然收口），不在新部署中
-- 「继续一次旧 POST」。V1 已持久化的 committing 任务（请求中断留库）由
-- 既有惰性恢复扫描/核账工具收口，不经 HTTP。
--
-- frozen_at 是**可信持久切换边界**：排空证明（report）以它为界核对
-- 切换前责任全部收口、切换后无新增旧链路责任。

CREATE TABLE IF NOT EXISTS upload_drain_freeze (
    upload_id      TEXT        PRIMARY KEY,
    state_at_freeze TEXT       NOT NULL,
    owner_user_id  TEXT        NOT NULL DEFAULT '',
    frozen_at      timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE upload_drain_freeze IS
    '检查点 A 排空冻结清单（0076）：drain 模式下 V2 控制面只服务清单内任务；'
    'freeze 幂等（重复执行不覆盖既有行）';
