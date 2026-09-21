# -*- coding: utf-8 -*-
"""协议页面的最小 Markdown 子集渲染器（P0，legal_docs/*.md → 安全 HTML）。

只支持本站协议文稿实际用到的语法：``#``/``##``/``###`` 标题、``| ... |``
表格、``**加粗**``、``[文字](https://链接)``、有序/无序列表、``---`` 分隔线、
普通段落。所有文本先 HTML 转义再做行内变换，不引入第三方依赖、不执行任何
 raw HTML；文稿之外的输入不会产出可注入标记（未知语法按普通段落原样转义）。
"""

import html
import re

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_ORDERED_RE = re.compile(r"^\d+\.\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\|[\s:|-]+\|$")


def _inline(text) -> str:
    """行内渲染：先整段转义，再处理 **加粗** 与 [文字](URL)（URL 只允许
    http/https/以 / 开头的站内路径，其余退化为纯文本）。"""
    out = html.escape(text, quote=True)

    def _bold(m):
        return "<strong>%s</strong>" % m.group(1)

    out = re.sub(r"\*\*(.+?)\*\*", _bold, out)

    def _link(m):
        label, url = m.group(1), m.group(2)
        # out 已转义，url 里的 &quot; 等不会破环属性；协议层面再限制协议白名单
        raw_url = html.unescape(url)
        if not (raw_url.startswith("https://") or raw_url.startswith("http://")
                or raw_url.startswith("/")):
            return label
        return '<a href="%s" rel="noopener noreferrer">%s</a>' % (url, label)

    out = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _link, out)
    return out


def _render_table(lines):
    """渲染一个表格块（lines 为含表头/分隔/数据行的完整块）。"""
    def _cells(row):
        return [c.strip() for c in row.strip().strip("|").split("|")]

    header = _cells(lines[0])
    body = [ln for ln in lines[2:] if ln.strip().startswith("|")]
    parts = ["<table><thead><tr>"]
    parts += ["<th>%s</th>" % _inline(c) for c in header]
    parts.append("</tr></thead><tbody>")
    for ln in body:
        parts.append("<tr>")
        parts += ["<td>%s</td>" % _inline(c) for c in _cells(ln)]
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def render_markdown(text) -> str:
    """把协议文稿的 Markdown 子集渲染为 HTML 字符串。"""
    lines = (text or "").splitlines()
    out = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        m = _HEADING_RE.match(stripped)
        if m:
            level = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (level, _inline(m.group(2)), level))
            i += 1
            continue
        if stripped in ("---", "***"):
            out.append("<hr />")
            i += 1
            continue
        # 表格：| 表头 | / | --- | / 数据行……
        if stripped.startswith("|") and i + 1 < n and _TABLE_SEP_RE.match(
                lines[i + 1].strip()):
            block = [stripped, lines[i + 1].strip()]
            i += 2
            while i < n and lines[i].strip().startswith("|"):
                block.append(lines[i].strip())
                i += 1
            out.append(_render_table(block))
            continue
        # 无序列表
        if stripped.startswith("- "):
            out.append("<ul>")
            while i < n and lines[i].strip().startswith("- "):
                out.append("<li>%s</li>" % _inline(lines[i].strip()[2:]))
                i += 1
            out.append("</ul>")
            continue
        # 有序列表
        if _ORDERED_RE.match(stripped):
            out.append("<ol>")
            while i < n:
                mm = _ORDERED_RE.match(lines[i].strip())
                if not mm:
                    break
                out.append("<li>%s</li>" % _inline(mm.group(1)))
                i += 1
            out.append("</ol>")
            continue
        # 普通段落：合并连续非空行（Markdown 语义）
        buf = [stripped]
        i += 1
        while i < n:
            nxt = lines[i].strip()
            if (not nxt or _HEADING_RE.match(nxt) or nxt in ("---", "***")
                    or nxt.startswith("|") or nxt.startswith("- ")
                    or _ORDERED_RE.match(nxt)):
                break
            buf.append(nxt)
            i += 1
        out.append("<p>%s</p>" % _inline(" ".join(buf)))
    return "\n".join(out)
