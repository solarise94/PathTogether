# -*- coding: utf-8 -*-
"""注册验证邮件 worker（I 线，设计文档第 8 节；表 registration_mail_jobs）。

命名红线：本模块与表名**禁止**叫 outbox（billing 语义已占用）。

职责边界（MailSender 适配边界）：
  - 本模块是「发送实现」的唯一适配层：registration_store / app.py 只入队
    （同事务 INSERT registration_mail_jobs），绝不直接触达任何邮件通道；
  - 发送实现经 :func:`get_sender` 按 env 装配：
      * ``REGISTRATION_MAIL_SENDER=fake`` → :class:`FakeMailSender`
        （测试假发送器：内存捕获，必须永不在生产配置）；
      * ``REGISTRATION_MAIL_SENDER=agent_mail_cli`` →
        :class:`AgentMailCliSender`（Agent Mail CLI 适配器：exec 风格
        **参数数组**、无 shell、两步 confirmation_token；凭据由 CLI 自身
        配置/环境持有，不进本仓库、不进日志）；
      * ``REGISTRATION_MAIL_SENDER=smtp`` → :class:`SmtpMailSender`
        （SSL/STARTTLS SMTP；账号口令只从环境变量读取，日志不回传）；
      * 未配置 → ``None``：``email_verify_invite_activation`` 模式的前置
        检查（app 层）据此 fail-closed——sender 未配置时新模式**不能开启**。
  - 载荷安全：验证链接含**明文 token**，冻结正文在入队前经
    :func:`encrypt_payload`（Fernet）加密后才落库；库内只有
    ``token_hash``（registration_store 的域分离 HMAC）与密文载荷。明文
    token 只存在于「入队返回值 → 邮件正文」一条路径上。

worker 循环：``python registration_mail_worker.py --loop``（生产）；``--once``
单次排水（部署钩子/测试）。app 层入队后另有线程内 best-effort 即时排水
（失败不重试路径安全：作业保持 queued，worker 循环是权威发送方）。

P1-1/P1-2 语义（review）：
  - 发送阶段划分：DATA/确认步**之前**的失败=确定未发出（failed，有界指数
    退避重试）；请求发出后未收到远端最终响应=不确定（uncertain，绝不自动
    重发，防重复邮件/重复建号，留人工核对）；
  - 注册模式停机：排水前与 app 层共用 registration_store 的生效模式判定，
    非 email_verify_invite_activation 时全部作业保留 queued 直接返回；
  - 恢复开放后只发未过期作业（expires_at 已过期的保持 queued，验证端报
    expired）。
"""

import argparse
import base64
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import psycopg

import pg_store

_log = logging.getLogger("svs.registration_mail")

#: 验证 token 有效期（设计文档第 8 节：30 分钟）
VERIFY_TOKEN_TTL_SECONDS = 30 * 60

#: 单批排水上限（单条独立事务，SMTP 慢不长期持锁跨作业）
_DRAIN_BATCH = 20

#: CLI 单步超时（秒）
_CLI_TIMEOUT_SECONDS = 20


class MailSenderError(RuntimeError):
    """发送**确定未发出**（DATA/确认步之前失败或远端明确回绝）。

    worker 按 status=failed 记录并有界重试（指数退避 scheduled_at，attempts
    达上限后保持 failed 不再发送）。"""


class MailSenderUncertainError(MailSenderError):
    """发送**结果不确定**（P1-1）：请求已完整发出但未收到远端最终接受/拒绝
    响应——远端**可能已接受**，用户可能已收到邮件。

    worker 置 status=uncertain 且**绝不自动重发**（防重复邮件/重复建号），
    只留状态供人工核对；验证端在有效期内接受 uncertain 作业的 token。
    注意：本类是 MailSenderError 的子类，调用方 except 顺序必须**先捕获本类**。
    """


class MailSenderUnavailable(MailSenderError):
    """发送通道未配置/不可用（模式前置检查 fail-closed 依据）。"""


#: 确定失败的有界重试上限：attempts 达到后保持 status=failed 不再发送（P1-1）
_MAX_SEND_ATTEMPTS = 5
#: 指数退避基数（秒）：第 n 次失败（0 起数）后顺延 base * 2^n —— 30s/1m/2m/4m
_RETRY_BACKOFF_BASE_SECONDS = 30


# --------------------------------------------------------------------------- #
# 载荷加密（Fernet；cryptography 已在 requirements，与 AI api_key 加密同依赖）
# --------------------------------------------------------------------------- #
def _payload_secret() -> str:
    """载荷加密根 secret：REGISTRATION_MAIL_PAYLOAD_KEY → SECRET_KEY →
    数据目录 flask_secret.key（与 app secret 同源持久化，跨进程稳定）。"""
    for name in ("REGISTRATION_MAIL_PAYLOAD_KEY", "SECRET_KEY"):
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    data_dir = Path(
        os.environ.get("SHARE_DATA_DIR")
        or (Path.home() / "svs-viewer" / "share-data"))
    secret_file = data_dir / "flask_secret.key"
    try:
        v = secret_file.read_text(encoding="utf-8").strip()
    except OSError:
        v = ""
    if v:
        return v
    raise MailSenderUnavailable(
        "邮件载荷加密密钥不可用（REGISTRATION_MAIL_PAYLOAD_KEY / SECRET_KEY /"
        " flask_secret.key 均未配置）")


def payload_key_available() -> bool:
    """载荷加密密钥是否可用（email_verify_invite_activation 前置检查用）。"""
    try:
        _payload_secret()
        return True
    except Exception:
        return False


def _fernet():
    from cryptography.fernet import Fernet
    digest = hashlib.sha256(
        ("reg-mail-payload-v1:" + _payload_secret()).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_payload(payload: dict) -> str:
    """冻结正文（dict，含验证链接=含明文 token）→ Fernet 密文字符串。"""
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return _fernet().encrypt(blob.encode("utf-8")).decode("ascii")


def decrypt_payload(payload_enc: str) -> dict:
    """密文 → 冻结正文 dict；解密/解析失败抛 MailSenderError（记 failed）。"""
    try:
        blob = _fernet().decrypt(str(payload_enc).encode("ascii"))
        out = json.loads(blob.decode("utf-8"))
    except Exception as exc:
        raise MailSenderError(
            "payload_decrypt_failed（%s）" % exc.__class__.__name__) from exc
    if not isinstance(out, dict):
        raise MailSenderError("payload_shape_invalid")
    return out


# --------------------------------------------------------------------------- #
# 正文渲染（token 只进返回值；调用方加密后才落库）
# --------------------------------------------------------------------------- #
def build_verify_email_body(email, token, base_url):
    """构造验证邮件冻结正文。返回 (subject, body)。

    链接 = ``<base_url>/verify-email?token=<明文 token>``；GET /verify-email
    只展示不消费（消费在 POST /api/registration/verify）。
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise MailSenderUnavailable("PUBLIC_BASE_URL 未配置，无法构造验证链接")
    link = base + "/verify-email?token=" + str(token)
    subject = "PathTogether 邮箱验证（30 分钟内有效）"
    body = (
        "你好，\n\n"
        "有人（通常是你本人）刚用邮箱 %s 请求注册 PathTogether。\n"
        "请在 30 分钟内打开下面的链接完成邮箱验证：\n\n"
        "%s\n\n"
        "该链接只能使用一次。验证邮箱后，你还需要管理员发放的邀请码才能激活"
        "账号；验证邮箱本身不会授予任何工作区、AI 或额度权限。\n\n"
        "如果你没有请求过注册，请忽略本邮件。\n"
        % (str(email), link))
    return subject, body


# --------------------------------------------------------------------------- #
# MailSender 适配边界
# --------------------------------------------------------------------------- #
class FakeMailSender:
    """测试假发送器（必须提供的首个实现）：内存捕获，永不真实外发。

    仅限测试装配（``REGISTRATION_MAIL_SENDER=fake`` 或直接 monkeypatch
    :func:`get_sender`）；生产前置检查把 ``fake`` 视为**未配置生产通道**
    （sender_configured(production=True)=False），防止测试通道漏进生产。
    """

    def __init__(self):
        self.sent = []  # [(to, subject, body)]

    def send(self, to, subject, body):
        self.sent.append((str(to), str(subject), str(body)))

    def clear(self):
        self.sent.clear()


#: 模块级单例（测试经 install_fake_sender 取用）
_fake_sender = FakeMailSender()


def install_fake_sender():
    """返回模块级 FakeMailSender 单例（测试注入入口）。"""
    global _fake_sender
    return _fake_sender


class AgentMailCliSender:
    """Agent Mail CLI 适配器（两步 confirmation_token）。

    - ``subprocess.run`` **参数数组**（无 shell、无字符串拼接命令行）；
    - 两步：``send-request``（stdin 传正文）→ 解析 JSON 输出里的
      ``confirmation_token`` → ``send-confirm`` 提交确认；第二步确认后才
      真正外发（幂等防护由 CLI 侧负责）；
    - 凭据：由 CLI 自身配置/环境持有；本类**不**接收、不拼接、不记录任何
      凭据，日志与异常文本只含类别（argv/env 绝不进日志）。
    """

    def __init__(self, cli_path, sender=None):
        self.cli_path = str(cli_path or "").strip()
        self.sender = str(sender or "").strip() or None
        if not self.cli_path:
            raise MailSenderUnavailable("REGISTRATION_AGENT_MAIL_CLI 未配置")

    def _run(self, argv, stdin_text=None, confirm_phase=False):
        """执行一步 CLI（参数数组，无 shell）。

        ``confirm_phase=True`` 标注第二步 send-confirm：该步请求已提交、若未
        收到 CLI 响应（超时/执行中断）则**远端可能已确认发送**——按 P1-1 划
        分为不确定（MailSenderUncertainError），绝不当 failed 自动重试。
        """
        try:
            proc = subprocess.run(
                [self.cli_path] + list(argv),  # 参数数组；绝无 shell
                input=stdin_text, capture_output=True, text=True,
                timeout=_CLI_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            # CLI 缺失=确认步根本未发出（确定未发送），两阶段同语义
            raise MailSenderUnavailable(
                "agent_mail_cli_not_found") from exc
        except subprocess.TimeoutExpired as exc:
            if confirm_phase:
                raise MailSenderUncertainError(
                    "agent_mail_cli_confirm_timeout") from exc
            raise MailSenderError("agent_mail_cli_timeout") from exc
        except OSError as exc:
            if confirm_phase:
                raise MailSenderUncertainError(
                    "agent_mail_cli_confirm_no_response（%s）"
                    % exc.__class__.__name__) from exc
            raise MailSenderError(
                "agent_mail_cli_exec_failed（%s）" % exc.__class__.__name__
            ) from exc
        if proc.returncode != 0:
            # CLI 已给出明确回绝（exit code=收到了响应）→ 确定未发出；
            # 不回传 stderr 原文（可能含本机路径/凭据线索），只记类别
            raise MailSenderError(
                "agent_mail_cli_exit_%d" % int(proc.returncode))
        return proc.stdout

    def send(self, to, subject, body):
        # 阶段 1（确定未发出区间）：send-request 未成功、确认令牌未拿到——
        # 远端两步协议下必未真正外发，一切失败按 MailSenderError（可重试）
        req_argv = ["send-request", "--to", str(to), "--subject",
                    str(subject)]
        if self.sender:
            req_argv += ["--from", self.sender]
        out = self._run(req_argv, stdin_text=str(body))
        try:
            payload = json.loads(out)
            token = str(payload["confirmation_token"])
        except Exception as exc:
            raise MailSenderError(
                "agent_mail_cli_bad_response（%s）"
                % exc.__class__.__name__) from exc
        if not token:
            raise MailSenderError("agent_mail_cli_empty_confirmation_token")
        # 阶段 2（不确定窗口）：send-confirm 已提交、响应未收到=远端可能已
        # 确认外发（P1-1：置 uncertain 不自动重发）；exit code 非零=CLI 明确
        # 回绝（确定未发出，仍按 failed）
        self._run(["send-confirm", "--confirmation-token", token],
                  confirm_phase=True)


class SmtpMailSender:
    """SSL/STARTTLS SMTP 发送器（163 等 IMAP/SMTP 授权码通道）。

    环境变量（均 trim；口令绝不进日志/异常原文）：
      REGISTRATION_SMTP_HOST / REGISTRATION_SMTP_PORT（默认 465）
      REGISTRATION_SMTP_USER / REGISTRATION_SMTP_PASSWORD
      REGISTRATION_SMTP_FROM（缺省=USER）
      REGISTRATION_SMTP_STARTTLS=1 时走 587 STARTTLS，否则 SMTP_SSL。

    P1-1 不确定态的阶段划分（见 :meth:`_transmit`）：DATA 结束符发出**之前**
    的任何失败=确定未发出（failed，可重试）；结束符发出后等远端最终响应期间
    的超时/断连=结果不确定（uncertain，绝不自动重发）。
    """

    def __init__(self, environ=None):
        env = os.environ if environ is None else environ
        self.host = (env.get("REGISTRATION_SMTP_HOST") or "").strip()
        self.user = (env.get("REGISTRATION_SMTP_USER") or "").strip()
        self.password = env.get("REGISTRATION_SMTP_PASSWORD") or ""
        self.from_addr = ((env.get("REGISTRATION_SMTP_FROM") or "").strip()
                          or self.user)
        try:
            self.port = int((env.get("REGISTRATION_SMTP_PORT") or "465").strip()
                            or "465")
        except ValueError as exc:
            raise MailSenderUnavailable("registration_smtp_port_invalid") from exc
        self.starttls = (env.get("REGISTRATION_SMTP_STARTTLS") or "").strip().lower() in (
            "1", "true", "yes", "on")
        if not self.host or not self.user or not self.password:
            raise MailSenderUnavailable("registration_smtp_not_configured")

    def _transmit(self, smtp, msg_str, to_addr, ctx):
        """分阶段 SMTP 事务（P1-1 划分边界）。

        阶段 1（确定未发出区间）：EHLO / STARTTLS / AUTH / MAIL FROM /
        RCPT TO / DATA(354) 与正文写出——此段任何失败（含正文 socket 写失败：
        sendall 语义保证数据未完整送达、远端事务中止）都意味着邮件**确定未
        发出**，抛 MailSenderError（worker 记 failed 可重试）；
        阶段 2（不确定窗口）：正文与结束符 ``.`` 已全部写出、等待远端对整个
        事务的最终响应——此段超时/断连意味着远端**可能已接受**，抛
        MailSenderUncertainError（worker 置 uncertain，绝不自动重发）。
        """
        import smtplib

        # ---- 阶段 1 ----
        code, _resp = smtp.ehlo()
        if code != 250:
            raise MailSenderError("smtp_ehlo_rejected_%d" % int(code))
        if self.starttls:
            code, _resp = smtp.starttls(context=ctx)
            if code != 220:
                raise MailSenderError(
                    "smtp_starttls_rejected_%d" % int(code))
            code, _resp = smtp.ehlo()
            if code != 250:
                raise MailSenderError("smtp_ehlo_rejected_%d" % int(code))
        # 认证失败 → SMTPAuthenticationError（send 层统一译为 smtp_auth_failed）
        smtp.login(self.user, self.password)
        code, _resp = smtp.docmd("MAIL", "FROM:<%s>" % self.from_addr)
        if code != 250:
            raise MailSenderError("smtp_mail_from_rejected_%d" % int(code))
        code, _resp = smtp.docmd("RCPT", "TO:<%s>" % to_addr)
        if code not in (250, 251):
            raise MailSenderError("smtp_rcpt_rejected_%d" % int(code))
        code, _resp = smtp.docmd("DATA")
        if code != 354:
            raise MailSenderError("smtp_data_start_rejected_%d" % int(code))
        # 正文写出（与 smtplib.SMTP.data 对 str 的处理同款：CRLF 归一 + 行首
        # 点引用/转义；本处本地实现等价逻辑，避免依赖 smtplib 私有助手）
        import re as _re
        data = _re.sub(br"(?:\r\n|\n|\r(?!\n))", b"\r\n",
                       msg_str.encode("ascii"))
        data = _re.sub(br"(?m)^\.", b"..", data)
        smtp.send(data)
        smtp.send(b".\r\n")
        # ---- 阶段 2（不确定窗口）：等待远端最终响应 ----
        try:
            code, _resp = smtp.getreply()
        except Exception as exc:
            raise MailSenderUncertainError(
                "smtp_final_response_missing（%s）"
                % exc.__class__.__name__) from exc
        if code != 250:
            # 远端明确回绝（未接受）→ 仍属确定未发出
            raise MailSenderError("smtp_data_rejected_%d" % int(code))

    def send(self, to, subject, body):
        import smtplib
        import ssl
        from email.header import Header
        from email.mime.text import MIMEText
        from email.utils import formataddr, formatdate, make_msgid

        to_addr = str(to).strip()
        if not to_addr or "\n" in to_addr or "\r" in to_addr:
            raise MailSenderError("smtp_recipient_invalid")
        msg = MIMEText(str(body), "plain", "utf-8")
        msg["Subject"] = Header(str(subject), "utf-8")
        msg["From"] = formataddr(("PathTogether", self.from_addr))
        msg["To"] = to_addr
        msg["Date"] = formatdate(localtime=True)
        domain = self.from_addr.rsplit("@", 1)[-1] if "@" in self.from_addr else "localhost"
        msg["Message-ID"] = make_msgid(domain=domain)
        ctx = ssl.create_default_context()
        try:
            if self.starttls:
                smtp = smtplib.SMTP(self.host, self.port, timeout=30)
            else:
                smtp = smtplib.SMTP_SSL(self.host, self.port, context=ctx,
                                        timeout=30)
        except OSError as exc:
            raise MailSenderError(
                "smtp_connect_failed（%s）" % exc.__class__.__name__) from exc
        try:
            with smtp:
                self._transmit(smtp, msg.as_string(), to_addr, ctx)
        except MailSenderError:
            raise  # 含 MailSenderUncertainError（子类）：阶段语义由 _transmit 决定，原样透传
        except smtplib.SMTPAuthenticationError as exc:
            raise MailSenderError("smtp_auth_failed") from exc
        except smtplib.SMTPException as exc:
            raise MailSenderError(
                "smtp_send_failed（%s）" % exc.__class__.__name__) from exc
        except OSError as exc:
            # DATA 之前的连接级错误（确定未发出；正文写失败已在 sendall 语义
            # 下归入本类）
            raise MailSenderError(
                "smtp_send_failed（%s）" % exc.__class__.__name__) from exc


def sender_configured(environ=None, production=True) -> bool:
    """邮件发送通道是否已配置（email_verify_invite_activation 前置检查）。

    ``production=True``（默认）：``fake`` 不算已配置（测试通道不放大到生产
    前置）；测试可显式传 ``production=False`` 或 monkeypatch get_sender。
    """
    env = os.environ if environ is None else environ
    kind = (env.get("REGISTRATION_MAIL_SENDER") or "").strip()
    if not kind:
        return False
    if kind == "fake":
        return not production
    if kind == "agent_mail_cli":
        return bool((env.get("REGISTRATION_AGENT_MAIL_CLI") or "").strip())
    if kind == "smtp":
        return bool((env.get("REGISTRATION_SMTP_HOST") or "").strip()
                    and (env.get("REGISTRATION_SMTP_USER") or "").strip()
                    and (env.get("REGISTRATION_SMTP_PASSWORD") or "").strip())
    return False


def get_sender(environ=None):
    """按 env 装配发送器；未配置返回 None（入队照常，发送 fail 记录）。"""
    env = os.environ if environ is None else environ
    kind = (env.get("REGISTRATION_MAIL_SENDER") or "").strip()
    if kind == "fake":
        return _fake_sender
    if kind == "agent_mail_cli":
        return AgentMailCliSender(
            env.get("REGISTRATION_AGENT_MAIL_CLI"),
            env.get("REGISTRATION_AGENT_MAIL_FROM"))
    if kind == "smtp":
        try:
            return SmtpMailSender(env)
        except MailSenderUnavailable:
            return None
    return None


# --------------------------------------------------------------------------- #
# 排水（权威发送循环）
# --------------------------------------------------------------------------- #
def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def drain_once(limit=_DRAIN_BATCH, sender=None, environ=None) -> int:
    """处理至多 limit 条待发作业；返回成功发送条数。

    P1-2 模式停机语义：排水前查**生效注册模式**——与 app 层共用
    registration_store.resolve_effective_registration_mode 的权威判定（worker
    绝不 import Flask app）。非 email_verify_invite_activation → 全部作业保留
    queued 直接返回 0（注册暂停是运维动作，不是作业失败：不 fail、不计时、
    不改状态）。

    每条作业独立事务：``SELECT ... FOR UPDATE SKIP LOCKED``（行锁只在单条
    发送期间持有）→ 解密载荷 → sender.send → 按阶段分类落状态（P1-1）：

    - 成功 → status=sent；
    - :class:`MailSenderUncertainError`（远端可能已接受）→ status=uncertain，
      **不自动重发**（防重复邮件/重复建号），只留状态供人工核对；
    - :class:`MailSenderError`（确定未发出）→ status=failed，attempts+1，
      scheduled_at 按指数退避顺延（上限 :data:`_MAX_SEND_ATTEMPTS`，达上限
      保持 failed 不再发送）。

    领取范围只含 ``scheduled_at <= now()`` 且 ``expires_at > now()`` 的
    queued/未达上限 failed 行——恢复开放后只发未过期作业；已过期的保持
    queued（不发送、不 fail），由验证端报 expired。
    发送通道未配置 → 全部保留 queued（worker 前置未满足是部署问题，不是
    作业失败）。
    """
    import registration_store
    mode, _failures = registration_store.resolve_effective_registration_mode(
        environ)
    if mode != registration_store.MODE_EMAIL_VERIFY_INVITE_ACTIVATION:
        _log.info("生效注册模式为 %s（非 email_verify_invite_activation）："
                  "作业保留 queued，本轮不发送", mode)
        return 0
    snd = sender if sender is not None else get_sender()
    if snd is None:
        _log.warning("邮件发送通道未配置（REGISTRATION_MAIL_SENDER），%d 条"
                     "作业保留 queued", limit)
        return 0
    sent = 0
    conn = _connect()
    try:
        for _ in range(max(1, min(int(limit), 200))):
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    cur.execute(
                        "SELECT job_id, email_normalized, payload_enc, "
                        "payload_enc IS NULL AS bad_payload, attempts "
                        "FROM registration_mail_jobs "
                        "WHERE (status='queued' OR (status='failed' "
                        "AND attempts < %s)) "
                        "AND scheduled_at <= now() AND expires_at > now() "
                        "ORDER BY created_at, job_id LIMIT 1 FOR UPDATE "
                        "SKIP LOCKED",
                        (_MAX_SEND_ATTEMPTS,))
                    row = cur.fetchone()
                    if row is None:
                        break
                    try:
                        payload = decrypt_payload(row["payload_enc"])
                        subject = str(payload.get("subject") or "")
                        body = str(payload.get("body") or "")
                        snd.send(row["email_normalized"], subject, body)
                    except MailSenderUncertainError as exc:
                        # 先于 MailSenderError 捕获（子类）：远端可能已接受，
                        # 置 uncertain 且绝不自动重发（uncertain 不在领取
                        # 范围内），只留状态供人工核对
                        cur.execute(
                            "UPDATE registration_mail_jobs SET "
                            "status='uncertain', attempts=attempts+1, "
                            "last_error=%s WHERE job_id=%s",
                            (str(exc)[:120], row["job_id"]))
                        _log.warning("验证邮件发送结果不确定（job=%s 类别见 "
                                     "last_error）：置 uncertain，不自动"
                                     "重发", row["job_id"])
                        continue
                    except MailSenderError as exc:
                        attempts = int(row["attempts"] or 0)
                        if attempts + 1 >= _MAX_SEND_ATTEMPTS:
                            # 达重试上限：保持 failed，不再顺延 scheduled_at
                            # （领取范围含 attempts<上限，自然停发）
                            cur.execute(
                                "UPDATE registration_mail_jobs SET "
                                "status='failed', attempts=attempts+1, "
                                "last_error=%s WHERE job_id=%s",
                                (str(exc)[:120], row["job_id"]))
                        else:
                            # 确定未发出（DATA/确认步之前或远端明确回绝）：
                            # 指数退避后有界重试
                            delay = _RETRY_BACKOFF_BASE_SECONDS \
                                * (2 ** attempts)
                            cur.execute(
                                "UPDATE registration_mail_jobs SET "
                                "status='failed', attempts=attempts+1, "
                                "last_error=%s, scheduled_at=now() + "
                                "(%s * interval '1 second') "
                                "WHERE job_id=%s",
                                (str(exc)[:120], delay, row["job_id"]))
                        _log.warning("验证邮件发送失败（job=%s 第 %d/%d 次"
                                     "尝试，类别见 last_error）",
                                     row["job_id"], attempts + 1,
                                     _MAX_SEND_ATTEMPTS)
                        continue
                    cur.execute(
                        "UPDATE registration_mail_jobs SET status='sent', "
                        "attempts=attempts+1, sent_at=now(), last_error=NULL "
                        "WHERE job_id=%s", (row["job_id"],))
            sent += 1
    finally:
        conn.close()
    return sent


_drain_lock = threading.Lock()


def drain_async():
    """入队后的 best-effort 即时排水（后台守护线程；失败留 queued）。"""
    def _run():
        try:
            drain_once()
        except Exception:
            _log.warning("即时排水失败（作业保持 queued）", exc_info=True)

    if _drain_lock.acquire(blocking=False):
        try:
            threading.Thread(target=_run, daemon=True,
                             name="reg-mail-drain").start()
        finally:
            _drain_lock.release()


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true",
                        help="持续循环（生产 worker）")
    parser.add_argument("--once", action="store_true",
                        help="单次排水后退出（部署钩子/测试）")
    parser.add_argument("--interval", type=int, default=5,
                        help="循环轮询间隔秒（默认 5）")
    args = parser.parse_args(argv)
    if not (args.loop or args.once):
        parser.error("需要 --loop 或 --once 之一")
    if args.once:
        print("drained=%d" % drain_once())
        return 0
    while True:
        try:
            drain_once()
        except Exception:
            _log.exception("worker 循环排水异常（继续）")
        time.sleep(max(1, int(args.interval)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
