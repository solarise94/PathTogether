# -*- coding: utf-8 -*-
"""百度分享文本确定性解析器（W5，docs/agent-implementation-account-import-project-ui-2026-09-14.md §6.2）。

只做纯字符串解析，**绝不发起任何网络请求**（不抓分享页、不解析 HTML）：

- 从用户粘贴的整段文本提取规范分享 URL，仅允许 ``https://pan.baidu.com/s/<id>``；
- 提取码来源三处（query ``?pwd=xxxx`` / 正文 ``提取码：xxxx`` / 显式入参），
  多来源值不一致 → ``extraction_code_conflict``（400，提示用户修正，不猜测）；
- 非允许域名/形态的 URL → ``unsupported_share``；
- 文本中没有任何 URL → ``invalid_share_text``。

错误消息**不含**用户原文、完整分享 URL 或提取码（防日志泄敏）。
"""

from __future__ import annotations

import re

#: 允许的分享域名（精确 host 匹配；子域/后缀仿冒一律拒绝）
ALLOWED_HOSTS = frozenset({"pan.baidu.com"})

#: 提取码形态：4–8 位字母数字（大小写不敏感，统一小写落库）
_CODE_RE = re.compile(r"^[A-Za-z0-9]{4,8}$")

#: 分享 id：百度短链 slug（字母/数字/下划线/连字符）
_SHARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_URL_RE = re.compile(r"https?://[^\s'\"<>，。；、）)\]]+", re.IGNORECASE)

#: 正文提取码模式（对整段粘贴文本）：``提取码: abcd`` / ``提取碼：abcd`` /
#: 独立的 ``pwd=abcd``（前面必须是空白/行首，避免命中 URL 内的 ?pwd=）
_TEXT_CODE_PATTERNS = (
    re.compile(r"提取码\s*[:：]\s*([A-Za-z0-9]{4,8})", re.IGNORECASE),
    re.compile(r"提取碼\s*[:：]\s*([A-Za-z0-9]{4,8})", re.IGNORECASE),
    re.compile(r"(?:^|[\s>])pwd\s*[=:]\s*([A-Za-z0-9]{4,8})(?=$|[\s<])",
               re.IGNORECASE),
)

#: 输入文本上限（粘贴板炸药防护；超限直接拒绝）
MAX_SHARE_TEXT_BYTES = 4096


class ShareParseError(ValueError):
    """分享文本解析失败。``code`` 稳定：unsupported_share /
    invalid_share_text / extraction_code_conflict / extraction_code_invalid。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _norm_code(code):
    """提取码规范化：去空白、转小写；形态非法 → None（由调用方决定报错）。"""
    if code is None:
        return None
    code = str(code).strip().lower()
    return code if _CODE_RE.match(code) else None


def parse_share_text(text, extraction_code=None) -> dict:
    """解析粘贴文本 + 可选显式提取码。

    返回 ``{"share_url": "https://pan.baidu.com/s/<id>",
    "share_id": "<id>", "extraction_code": "<code>"|None}``。

    失败抛 :class:`ShareParseError`（code 见类 docstring）。
    显式入参与 query 码冲突、或与正文提取码冲突，都按
    ``extraction_code_conflict`` 拒绝；显式入参形态非法按
    ``extraction_code_invalid`` 拒绝（fail-closed，不猜测）。
    """
    if not isinstance(text, str) or not text.strip():
        raise ShareParseError("invalid_share_text", "分享文本为空")
    if len(text.encode("utf-8", "replace")) > MAX_SHARE_TEXT_BYTES:
        raise ShareParseError("invalid_share_text", "分享文本过长")

    explicit = None
    if extraction_code is not None:
        explicit = _norm_code(extraction_code)
        if explicit is None:
            raise ShareParseError(
                "extraction_code_invalid", "提取码形态非法（4-8 位字母数字）")

    share_url = None
    share_id = None
    pwd_from_query = None
    saw_url = False
    for m in _URL_RE.finditer(text):
        raw = m.group(0).rstrip(").,;]")  # 去掉贴文尾随标点
        host, path, query = _split_url(raw)
        if host is None:
            continue
        saw_url = True
        if host not in ALLOWED_HOSTS:
            continue
        mpath = re.match(r"^/s/([A-Za-z0-9_-]+)$", path)
        if not mpath:
            continue
        share_url = "https://pan.baidu.com/s/" + mpath.group(1)
        share_id = mpath.group(1)
        pwd_from_query = _pwd_from_query(query)
        break  # 只取第一个合法分享链接

    if share_url is None:
        if saw_url:
            raise ShareParseError(
                "unsupported_share", "仅支持 pan.baidu.com 的 /s/ 分享链接")
        raise ShareParseError("invalid_share_text", "未找到分享链接")

    if not _SHARE_ID_RE.match(share_id):
        # 理论不可达（regex 已限定），防御性保留
        raise ShareParseError("unsupported_share", "分享链接形态不支持")

    # 正文提取码（独立于 URL query）
    text_code = None
    for pat in _TEXT_CODE_PATTERNS:
        tm = pat.search(text)
        if tm:
            cand = _norm_code(tm.group(1))
            if cand is not None:
                text_code = cand
                break

    # 冲突判定：所有非空来源必须一致
    sources = {
        "query": pwd_from_query,
        "text": text_code,
        "explicit": explicit,
    }
    present = {v for v in sources.values() if v is not None}
    if len(present) > 1:
        raise ShareParseError(
            "extraction_code_conflict",
            "提取码来源冲突（query/正文/入参不一致），请修正后重试")

    code = present.pop() if present else None
    return {
        "share_url": share_url,
        "share_id": share_id,
        "extraction_code": code,
    }


def _split_url(raw):
    """裸切 URL → (host, path, query)。解析失败返回 (None, None, None)。

    不用 urllib.parse.urlsplit 一步到位是因为 host 规范化（大小写、
    默认端口、尾部点）必须自己收紧；这里保持最小实现。
    """
    s = raw.strip()
    m = re.match(r"^(https?)://([^/?#]+)([^?#]*)(?:\?([^#]*))?", s,
                 re.IGNORECASE)
    if not m:
        return None, None, None
    host = m.group(2).lower().rstrip(".")
    return host, m.group(3) or "/", m.group(4) or ""


def _pwd_from_query(query):
    """从 query 串提取 ``pwd`` 参数（规范化；无/非法返回 None）。"""
    if not query:
        return None
    for part in query.split("&"):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        if k.strip().lower() == "pwd":
            return _norm_code(v)
    return None
