"""Verified-user test applications, optional research consent, and owner review.

No research export/collection is enabled by this module. Any future research job
must check current consent at time of use; an old email is not consent authority.
"""
import os
import secrets
from datetime import datetime, timezone

import pg_store
import registration_store as registration
import registration_mail_worker as mail
import spend_store

DIRECTIONS = {'model_plant': '模式植物', 'model_animal': '模式动物',
              'clinical_pathology': '临床病理', 'other': '其他'}
CONSENT_VERSION = 'research-data-20260916-v1'
CONSENT_TEXT = '我愿意向研究团队分享我的切片、分析结果及使用行为数据，用于软件改进和科学研究。'


def admin_email():
    value = (os.environ.get('TEST_APPLICATION_ADMIN_EMAIL')
             or os.environ.get('FORMAT_REQUEST_ADMIN_EMAIL')
             or 'solarise94@gmail.com')
    return registration.validate_email(value) if value else None


def validate(direction, share):
    if direction not in DIRECTIONS or not isinstance(share, bool):
        raise ValueError('请选择研究方向，并确认数据分享选项')
    return {'research_direction': direction, 'share_research_data': share,
            'consent_version': CONSENT_VERSION}


def _queue_tx(cur, recipient, purpose, subject, body):
    payload = mail.encrypt_payload({'subject': subject, 'body': body})
    cur.execute('INSERT INTO registration_mail_jobs '
                '(job_id,purpose,email_normalized,token_hash,payload_enc,status,expires_at) '
                "VALUES (%s,%s,%s,%s,%s,'queued',now()+interval '7 days')",
                ('rmj_'+secrets.token_urlsafe(12), purpose, recipient,
                 registration.verify_token_hash(secrets.token_urlsafe(32)), payload))


def submit_tx(cur, user_id, direction, share):
    """Same transaction as verification or an authenticated application submission."""
    data = validate(direction, share)
    recipient = admin_email()
    if not recipient:
        raise ValueError('测试申请通知邮箱尚未配置，请稍后重试')
    cur.execute('SELECT user_id,email_normalized,email_verified_at,disabled,activation_state '
                'FROM users WHERE user_id=%s FOR UPDATE', (user_id,))
    user = cur.fetchone()
    if not user or user['disabled'] or not user['email_verified_at'] or user['activation_state'] != 'pending_activation':
        raise ValueError('仅已验证邮箱的待激活账号可申请测试')
    cur.execute('INSERT INTO test_applications '
                '(user_id,research_direction,share_research_data,consent_version) '
                'VALUES (%s,%s,%s,%s) ON CONFLICT (user_id) DO NOTHING RETURNING user_id',
                (user_id, direction, share, CONSENT_VERSION))
    if cur.fetchone() is None:
        return False  # Idempotent; repeated clicks do not email the administrator again.
    base = mail.public_base_url()
    body = (f'新测试申请\n邮箱：{user["email_normalized"]}\n研究方向：{DIRECTIONS[direction]}\n'
            f'自愿分享研究数据：{"同意" if share else "未同意"}\n'
            '数据分享选择不影响测试申请的审批。\n'
            f'请登录管理页审核：{base}/admin/test-applications\n'
            '邮件链接仅打开审核列表，不会直接激活账号。')
    _queue_tx(cur, recipient, 'test_application', 'HistoPilot · 新测试申请', body)
    registration._insert_audit(cur, 'test_application.submit', user_id, 'user', user_id, data)
    return True


def submit(user_id, direction, share):
    conn = registration._connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return submit_tx(cur, user_id, direction, share)
    finally:
        conn.close()


def get(user_id):
    conn = registration._connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM test_applications WHERE user_id=%s', (user_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def list_applications(direction=None):
    if direction and direction not in DIRECTIONS:
        raise ValueError('研究方向无效')
    conn = registration._connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT t.*,u.email_normalized,u.display_name,u.activation_state '
                        'FROM test_applications t JOIN users u USING(user_id) '
                        'WHERE (%s::text IS NULL OR t.research_direction=%s) '
                        "ORDER BY (t.status='pending') DESC,t.created_at DESC LIMIT 500",
                        (direction or None, direction or None))
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def set_consent(user_id, share):
    if not isinstance(share, bool):
        raise ValueError('数据分享选择无效')
    conn = registration._connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute('UPDATE test_applications SET share_research_data=%s, '
                            'consent_version=%s,consent_updated_at=now() WHERE user_id=%s RETURNING user_id',
                            (share, CONSENT_VERSION, user_id))
                if not cur.fetchone():
                    raise ValueError('尚无测试申请记录')
                registration._insert_audit(cur, 'research_consent.update', user_id, 'user', user_id,
                                           {'share_research_data': share, 'consent_version': CONSENT_VERSION})
    finally:
        conn.close()


def review(user_id, actor_id, decision, ai_access=True):
    """Atomic provisioning/decision/mail; a repeated approval never adds credits."""
    if decision not in ('approved', 'rejected') or not isinstance(ai_access, bool):
        raise ValueError('审核操作无效')
    conn = registration._connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if spend_store.is_dispatch_maintenance_tx(cur):
                    raise ValueError('系统维护中，请稍后审核')
                spend_store.acquire_user_provisioning_lock_tx(cur)
                if spend_store.is_dispatch_maintenance_tx(cur):
                    raise ValueError('系统维护中，请稍后审核')
                cur.execute("SELECT user_id FROM users WHERE user_id=%s AND role='owner' AND NOT disabled", (actor_id,))
                if not cur.fetchone():
                    raise PermissionError('仅管理员可审核')
                cur.execute('SELECT * FROM users WHERE user_id=%s FOR UPDATE', (user_id,))
                user = cur.fetchone()
                cur.execute('SELECT * FROM test_applications WHERE user_id=%s FOR UPDATE', (user_id,))
                application = cur.fetchone()
                if not user or not application:
                    raise ValueError('申请不存在')
                if application['status'] != 'pending':
                    return False
                if user['disabled'] or user['activation_state'] != 'pending_activation':
                    raise ValueError('账号已激活或不可用，请刷新后核对')
                if decision == 'approved':
                    limit, _, version = spend_store._resolve_total_default_tx(cur, datetime.now(timezone.utc))
                    if limit is None:
                        raise ValueError('请先在管理工作台设置新用户默认总额度')
                    spend_store.create_user_total_allowance_tx(cur, user_id, limit, source='admin_create',
                                                               default_version=version, updated_by=actor_id)
                    cur.execute("UPDATE users SET activation_state='active',activation_source='admin',"
                                'activation_updated_at=now(),auth_version=auth_version+1,ai_access=%s '
                                'WHERE user_id=%s', (ai_access, user_id))
                cur.execute('UPDATE test_applications SET status=%s,reviewed_at=now(),reviewed_by=%s '
                            'WHERE user_id=%s', (decision, actor_id, user_id))
                registration._insert_audit(cur, 'test_application.review', actor_id, 'user', user_id,
                                           {'decision': decision, 'ai_access': ai_access if decision == 'approved' else False})
                outcome = '已通过测试申请，账号已激活。' if decision == 'approved' else '本次测试申请暂未通过。如需了解详情，请联系管理员。'
                _queue_tx(cur, user['email_normalized'], 'test_decision', 'HistoPilot · 测试申请审核结果',
                          outcome+'\n'+mail.public_base_url()+'/login')
                return True
    finally:
        conn.close()
