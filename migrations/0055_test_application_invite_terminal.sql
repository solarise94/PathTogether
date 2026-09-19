-- 0055：test_applications 新增显式终态 activated_by_invite（R7 修复 2026-09-19）。
--
-- 背景：修复前 registration_store.activate_registered_user 只改 users/邀请码/
-- 额度/审计，不更新 test_applications，留下「已提交申请 → 邀请码激活 → 申请
-- 仍 pending → 管理员审批报错（账号已激活或不可用）」的永久滞留状态。
--
-- 本迁移只扩展 status CHECK 词表；收口逻辑在应用层
-- activate_registered_user 单事务内完成（新激活不再产生滞留）；历史滞留行由
-- scripts/repair_invite_activated_applications.py（dry-run/apply、记真实修复
-- 审计）显式收口——迁移不改任何数据，不伪造 reviewed_by/reviewed_at。
--
-- 语义：
--   pending              已提交，等待管理员审核（唯一可审批态）
--   approved             管理员通过（人工审批，reviewed_by 非空）
--   rejected             管理员拒绝（人工审批，终态，邀请码激活绝不改写）
--   activated_by_invite  用户凭邀请码激活时同事务自动收口（非人工审批：
--                        reviewed_by/reviewed_at 保持 NULL，激活时间见
--                        users.activation_updated_at）
ALTER TABLE test_applications
    DROP CONSTRAINT IF EXISTS test_applications_status_check;
ALTER TABLE test_applications
    ADD CONSTRAINT test_applications_status_check
    CHECK (status IN ('pending','approved','rejected','activated_by_invite'));
