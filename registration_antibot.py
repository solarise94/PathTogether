# -*- coding: utf-8 -*-
"""注册防刷（Cloudflare Turnstile Siteverify）与注册入口一致性共享模块。

docs/registration-antibot-and-author-help-design-20261008.md §3/§6/§7/§8。

本模块同时服务 Flask app 层与 registration_mail_worker（worker 只用站点
映射，不 import Flask），自身无任何写副作用、不落库：

  - Turnstile 配置装配与校验（env only：``REGISTRATION_TURNSTILE_REQUIRED``
    / ``TURNSTILE_SITE_KEY`` / ``TURNSTILE_SECRET`` / ``TURNSTILE_HOSTNAMES``
    / ``REGISTRATION_TURNSTILE_ALLOW_TEST_KEYS``）。required 且未配齐/含
    Cloudflare 测试密钥（未显式允许）→ ``available=False``，发送路径
    fail-closed（不静默无校验放行）；
  - :func:`verify`：Siteverify 结果三分类 ok / rejected / unavailable。
    token 形状（非空 str 且 ≤2048）不合规**不打网络**直接 rejected；网络
    调用经 :func:`_siteverify_post`（requests），测试可 monkeypatch，绝不
    真实外网；
  - 注册入口站点映射（§6）：host → {origin, name, default_locale}。验证
    邮件链接、站点显示名、帮助页链接共用这一份映射；Host 只取请求 Host，
    绝不取 X-Forwarded-Host / Origin / Referer / next / 隐藏域。

安全红线：secret 绝不进日志/模板/异常原文；本模块不记录 token 明文。
"""

import hashlib
import hmac
import logging
import os
import uuid
from urllib.parse import urlparse

_log = logging.getLogger("svs.registration")

#: Cloudflare Turnstile 服务端校验端点（§7）
SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

#: 注册防刷的两种 action（§7）：首次提交 / 主动重发
ACTION_REGISTRATION_START = "registration_start"
ACTION_REGISTRATION_RESEND = "registration_resend"

#: 前端 token 的表单/JSON 字段名（Turnstile widget 自动注入）
TURNSTILE_TOKEN_FIELD = "cf-turnstile-response"

#: token 最大长度（Cloudflare 官方：2,048 字符；超限直接拒，不打网络）
TURNSTILE_TOKEN_MAX_CHARS = 2048

#: Siteverify 总等待预算 ≈5s：首次 3s + 一次重试 2s（同一 idempotency_key）
_SITEVERIFY_TIMEOUT_SECONDS = 3.0
_SITEVERIFY_RETRY_TIMEOUT_SECONDS = 2.0

#: Cloudflare 官方测试密钥（https://developers.cloudflare.com/turnstile/
#: troubleshooting/testing/）：生产配置出现即视为未配置（除非显式
#: REGISTRATION_TURNSTILE_ALLOW_TEST_KEYS，仅限开发/测试）
_CLOUDFLARE_TEST_SECRETS = frozenset({
    "1x0000000000000000000000000000000AA",  # always passes
    "2x0000000000000000000000000000000AA",  # always fails
    "3x0000000000000000000000000000000AA",  # yes/e2e
})
_CLOUDFLARE_TEST_SITE_KEY_PREFIXES = (
    "1x00000000000000000000",  # visible
    "2x00000000000000000000",  # block
    "3x00000000000000000000",  # invisible
)
#: 测试专用 hostname（生产白名单出现即视为测试配置）
_TEST_HOSTNAMES = frozenset({"localhost", "127.0.0.1"})


def _env_truthy(env, name) -> bool:
    return (env.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


# =========================================================================== #
# 入口站点映射（§6）：host → {origin, name, default_locale}
# =========================================================================== #
#: 统一站点映射：验证邮件链接、邮件站点名、帮助页链接、默认语言共用。
#: 旧 pt.solarise94.fun 入口按 .cn 处理（nginx 已 308 跳 .cn 保留路径，
#: 旧邮件链接继续兼容；新邮件一律 .cn）。
ENTRY_SITES = {
    "histopilot.cn": {
        "origin": "https://histopilot.cn",
        "name": "HistoPilot.cn",
        "default_locale": "zh",
    },
    "histopilot.com": {
        "origin": "https://histopilot.com",
        "name": "HistoPilot.com",
        "default_locale": "en",
    },
    "pt.solarise94.fun": {
        "origin": "https://histopilot.cn",
        "name": "HistoPilot.cn",
        "default_locale": "zh",
    },
}

#: 生产部署可发信的受信任 origin 白名单（§6 信任边界：固定 HTTPS 白名单）
TRUSTED_ENTRY_ORIGINS = frozenset(
    {site["origin"] for site in ENTRY_SITES.values()})

#: 映射里“生产入口”的 Host（PUBLIC_BASE_URL 命中其一 → 视为生产部署：
#: 未知 Host 不得回退别的域名发信）
_PRODUCTION_ENTRY_HOSTS = frozenset(
    {"histopilot.cn", "histopilot.com", "pt.solarise94.fun"})

#: 表单语言白名单（hidden form_locale；缺省/非法 → 入口默认语言）
FORM_LOCALES = ("zh", "en")


def request_hostname(raw_host) -> str:
    """请求 Host 头 → 规范化 hostname（去端口、小写、去尾点）。

    只接受 Host 头本身；调用方绝不传 X-Forwarded-Host / Origin / Referer。
    解析失败返回空串（按未知 Host 处理）。
    """
    raw = (raw_host or "").strip()
    if not raw:
        return ""
    try:
        return ((urlparse("//" + raw).hostname or "").lower().rstrip("."))
    except ValueError:
        return ""


def entry_site_for_host(hostname) -> dict:
    """hostname → 站点映射 dict（origin/name/default_locale）；未映射 None。"""
    return ENTRY_SITES.get(request_hostname(hostname))


def entry_site_for_origin(origin) -> dict:
    """origin（https://host 形式）→ 站点映射 dict；未映射 None。"""
    if not origin:
        return None
    raw = str(origin).strip().rstrip("/")
    hostname = request_hostname(urlparse(raw).netloc or raw)
    site = ENTRY_SITES.get(hostname)
    if site and site["origin"] == raw:
        return site
    # 允许裸 origin 匹配（历史回填场景只比对映射值本身）
    for site in ENTRY_SITES.values():
        if site["origin"] == raw:
            return site
    return None


def site_name_for_origin(origin) -> str:
    """origin → 站点显示名；未映射回退 "HistoPilot"（不外泄 Host 头）。"""
    site = entry_site_for_origin(origin)
    return site["name"] if site else "HistoPilot"


def normalize_form_locale(value, default="zh") -> str:
    """form_locale 白名单化：zh|en；缺省/非法 → default（入口默认语言）。"""
    v = str(value or "").strip().lower()
    if v in FORM_LOCALES:
        return v
    d = str(default or "zh").strip().lower()
    return d if d in FORM_LOCALES else "zh"


def resolve_entry_site(host, public_base_url) -> dict:
    """按请求 Host 解析本次注册入口站点。

    返回 ``{"origin", "name", "default_locale", "mapped": bool}``：

    - Host 命中映射 → 对应站点（旧 .fun 入口归一为 .cn）；
    - 未命中且 ``PUBLIC_BASE_URL`` 的 host 是映射内生产入口（即生产部署）
      → 返回 ``{"refused": True}``：已知生产入口匹配不到映射属配置错误，
      不静默回退别的域名（§6 信任边界）；
    - 其余（开发/测试默认地址）→ 回退 PUBLIC_BASE_URL 作为 today 的行为
      （name 由 host 派生，不把任意 Host 反射进邮件——origin 只取部署配置
      的 canonical 值）。
    """
    hostname = request_hostname(host)
    site = ENTRY_SITES.get(hostname)
    if site is not None:
        out = dict(site)
        out["mapped"] = True
        return out
    base = (public_base_url or "").strip().rstrip("/")
    base_host = request_hostname(urlparse(base).netloc if "://" in base
                                 else base)
    if base_host in _PRODUCTION_ENTRY_HOSTS:
        return {"refused": True}
    if base:
        fallback = {
            "origin": base,
            "name": "HistoPilot",
            "default_locale": "zh",
            "mapped": False,
        }
        return fallback
    # 未配置 PUBLIC_BASE_URL：本地无入口形态（调用方 fail-closed）
    return {"refused": True}


# =========================================================================== #
# Turnstile 配置（§7/§8）
# =========================================================================== #
class TurnstileConfig:
    """Turnstile 配置快照（env only；secret 绝不进日志/模板）。"""

    __slots__ = ("required", "site_key", "secret", "hostnames",
                 "allow_test_keys")

    def __init__(self, required=False, site_key="", secret="",
                 hostnames=(), allow_test_keys=False):
        self.required = bool(required)
        self.site_key = str(site_key or "").strip()
        self.secret = str(secret or "").strip()
        self.hostnames = frozenset(
            request_hostname(h) for h in hostnames if request_hostname(h))
        self.allow_test_keys = bool(allow_test_keys)

    @property
    def uses_test_keys(self) -> bool:
        """配置里出现 Cloudflare 测试密钥 / 测试 hostname。"""
        if self.secret in _CLOUDFLARE_TEST_SECRETS:
            return True
        if self.site_key.startswith(_CLOUDFLARE_TEST_SITE_KEY_PREFIXES):
            return True
        if self.hostnames & _TEST_HOSTNAMES:
            return True
        return False

    @property
    def configured(self) -> bool:
        """sitekey/secret/hostname 白名单齐备且不含（未允许的）测试密钥。"""
        if not (self.site_key and self.secret and self.hostnames):
            return False
        if self.uses_test_keys and not self.allow_test_keys:
            return False
        return True

    @property
    def available(self) -> bool:
        """校验可用：required 且 configured（未 required 时无需挑战）。"""
        return bool(self.required and self.configured)


def load_turnstile_config(environ=None) -> TurnstileConfig:
    """从 env 装配 Turnstile 配置（纯函数；缺项不抛异常，由 available 表达）。"""
    env = os.environ if environ is None else environ
    raw_hostnames = (env.get("TURNSTILE_HOSTNAMES") or "")
    hostnames = [h for h in
                 (t.strip() for t in raw_hostnames.split(",")) if h]
    return TurnstileConfig(
        required=_env_truthy(env, "REGISTRATION_TURNSTILE_REQUIRED"),
        site_key=env.get("TURNSTILE_SITE_KEY") or "",
        secret=env.get("TURNSTILE_SECRET") or "",
        hostnames=hostnames,
        allow_test_keys=_env_truthy(
            env, "REGISTRATION_TURNSTILE_ALLOW_TEST_KEYS"),
    )


class TurnstileResult:
    """Siteverify 三分类结果：status ∈ ok / rejected / unavailable。"""

    __slots__ = ("status", "reason")

    def __init__(self, status, reason=""):
        self.status = str(status)
        self.reason = str(reason or "")

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def __repr__(self):  # pragma: no cover - 调试友好；不含 secret/token
        return "TurnstileResult(%r, %r)" % (self.status, self.reason)


class _SiteverifyNetworkError(RuntimeError):
    """Siteverify 网络层失败（连接/超时等；可携同一 idempotency_key 重试一次）。"""


def _siteverify_post(url, data, timeout):
    """默认 HTTP 实现（requests，已在 requirements）。测试 monkeypatch 本函数。

    返回 ``(status_code, parsed_json_or_None)``；网络异常抛
    :class:`_SiteverifyNetworkError`（不把 requests 异常原文带出——可能含
    代理/端点线索）。
    """
    import requests
    try:
        resp = requests.post(url, data=data, timeout=timeout)
    except Exception as exc:
        raise _SiteverifyNetworkError(exc.__class__.__name__) from exc
    try:
        return int(resp.status_code), resp.json()
    except ValueError:
        return int(resp.status_code), None


def verify(token, *, action, remoteip=None, expected_hostname=None,
           config=None, http_post=None) -> TurnstileResult:
    """校验一次 Turnstile token（§7 服务端 Siteverify）。

    - ``token``：非空 str 且 ≤2048 字符，否则 **rejected（无网络调用）**；
    - 未 required / 未配齐（含未允许的测试密钥）→ **unavailable(not_
      configured)**（无网络调用）——调用方 fail-closed，不得静默放行；
    - POST form-encoded ``secret``/``response``/``remoteip``（仅在拿到可信
      客户端 IP 时携带）/``idempotency_key``（uuid4；唯一一次重试复用同一
      key）到 SITEVERIFY_URL；总等待 ≈5s（3s + 重试 2s）；
    - 网络错误 / 非 200 / 响应非 JSON → **unavailable**（不判定“机器人”）；
    - ``success is True`` 且 ``action`` 等于预期 且 ``hostname`` 在
      TURNSTILE_HOSTNAMES 白名单 且（给出 expected_hostname 时）等于本次
      实际入口 host → ok；否则 **rejected**（细分 reason：
      invalid_token/replayed/action_mismatch/hostname_not_allowed/
      hostname_mismatch/challenge_failed）。
    """
    cfg = config if config is not None else load_turnstile_config()
    if not cfg.required:
        return TurnstileResult("ok", "not_required")
    if not cfg.configured:
        return TurnstileResult("unavailable", "not_configured")
    if not isinstance(token, str) or not token.strip() \
            or len(token) > TURNSTILE_TOKEN_MAX_CHARS:
        return TurnstileResult("rejected", "invalid_token")
    post = http_post if http_post is not None else _siteverify_post
    idempotency_key = str(uuid.uuid4())
    data = {"secret": cfg.secret, "response": token,
            "idempotency_key": idempotency_key}
    ip = str(remoteip or "").strip()
    if ip:
        data["remoteip"] = ip
    payload = None
    for timeout in (_SITEVERIFY_TIMEOUT_SECONDS,
                    _SITEVERIFY_RETRY_TIMEOUT_SECONDS):
        try:
            status, payload = post(SITEVERIFY_URL, data, timeout)
        except _SiteverifyNetworkError:
            payload = None
            continue  # 网络错误：同一 idempotency_key 重试一次（§7）
        if status != 200:
            return TurnstileResult("unavailable", "http_%d" % status)
        break
    if payload is None:
        return TurnstileResult("unavailable", "network")
    if not isinstance(payload, dict):
        return TurnstileResult("unavailable", "bad_response")
    if payload.get("success") is not True:
        codes = payload.get("error-codes") or []
        if isinstance(codes, list) and any(
                str(c) in ("timeout-or-duplicate",) for c in codes):
            return TurnstileResult("rejected", "replayed")
        return TurnstileResult("rejected", "challenge_failed")
    if payload.get("action") != action:
        return TurnstileResult("rejected", "action_mismatch")
    hostname = request_hostname(payload.get("hostname"))
    if not hostname or hostname not in cfg.hostnames:
        return TurnstileResult("rejected", "hostname_not_allowed")
    expected = request_hostname(expected_hostname)
    if expected and hostname != expected:
        return TurnstileResult("rejected", "hostname_mismatch")
    return TurnstileResult("ok", "")


def turnstile_widget_context(config=None) -> dict:
    """注册弹窗的 Turnstile 渲染上下文（sitekey 公开；secret 绝不出现）。

    ``enabled`` 仅在 required 且 configured 时为 True；required 但未配齐时
    为 False（widget 不渲染，发送路径一律 fail-closed 到 unavailable 态）。
    """
    cfg = config if config is not None else load_turnstile_config()
    return {
        "enabled": cfg.available,
        "site_key": cfg.site_key if cfg.available else "",
        "action": ACTION_REGISTRATION_START,
    }


def salted_email_tag(email) -> str:
    """日志用带盐邮箱标识（12 hex）：绝不记录完整邮箱（§8）。

    盐链与 verify_token_hash 同源（REGISTRATION_VERIFY_HASH_SALT →
    AUTH_SUBJECT_HASH_SALT → SECRET_KEY → 固定域常量）。
    """
    for name in ("REGISTRATION_VERIFY_HASH_SALT", "AUTH_SUBJECT_HASH_SALT",
                 "SECRET_KEY"):
        v = (os.environ.get(name) or "").strip()
        if v:
            salt = v
            break
    else:
        salt = "pt-registration-verify-v1"
    return hmac.new(("reglog:" + salt).encode("utf-8"),
                    str(email or "").encode("utf-8"),
                    hashlib.sha256).hexdigest()[:12]
