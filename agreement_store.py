# -*- coding: utf-8 -*-
"""协议文档注册表服务（P0：docs/agent-plan-20260921-registration-consent-research.md §3）。

职责：

- **内置文稿**：``legal_docs/`` 下的版本化 Markdown 文件是内容唯一权威；
  本模块在 import 期读取并计算 SHA-256，向 ``agreement_documents`` 注册表
  登记（版本、hash、locale、不可变定位、发布状态）。
- **不可变语义**：同一 ``(document_type, version, locale)`` 已登记行与磁盘
  文件 hash 不一致 → 抛错（禁止内容修改后沿用相同 version）；内容实质变化
  必须发新版本。
- **发布状态机**：draft → published → retired；同一 (document_type, locale)
  至多一条 published（0060 迁移的部分唯一索引兜底，本模块在事务内先 retire
  旧条再发布新条）。P0 内置文稿默认以 **draft** 登记（草稿查看与正式
  published 状态分开，§3.2）；缺当前发布文稿时 accept/grant fail-closed。
- /legal/* 公开只读页面直接按内置文件渲染（不查库、GET 不创建用户、不记录
  同意）；注册表服务于服务端校验（接受凭据、研究授权版本/hash 校验）。
"""

import hashlib
from pathlib import Path

import psycopg

import pg_store

#: 文稿目录（<repo_root>/legal_docs）
_DOCS_DIR = Path(__file__).resolve().parent / "legal_docs"

#: 文档类型词表（与 0060 迁移 CHECK 一致）
DOCUMENT_TYPES = ("user_agreement", "research_sharing", "model_providers")

#: 当前内置文稿版本（协议文稿 2026-09-21-v4）
BUILTIN_VERSION = "2026-09-21-v4"

#: 首版以审核过的中文版本为准
DEFAULT_LOCALE = "zh-CN"

#: 内置文稿清单：document_type → (URL slug, 标题, 文件名)
_BUILTIN = {
    "user_agreement": (
        "user-agreement", "用户协议与数据处理说明",
        "user-agreement_%s.md" % BUILTIN_VERSION),
    "research_sharing": (
        "research-sharing", "数据共享与软件改进协议",
        "research-sharing_%s.md" % BUILTIN_VERSION),
    "model_providers": (
        "model-providers", "模型服务商披露",
        "model-providers_%s.md" % BUILTIN_VERSION),
}

#: slug → document_type 反查（/legal/<slug> 路由用）
SLUG_TO_TYPE = {v[0]: k for k, v in _BUILTIN.items()}


class AgreementStoreError(RuntimeError):
    """agreement_store 业务异常基类。"""

    code = "agreement_store_error"


class DocumentConflictError(AgreementStoreError):
    """已登记文稿与磁盘内容 hash 不一致（同 version 换内容）。"""

    code = "document_content_conflict"


class DocumentNotFoundError(AgreementStoreError):
    """注册表中没有该 (document_type, version, locale) 文稿。"""

    code = "document_not_found"


class DocumentNotPublishedError(AgreementStoreError):
    """没有当前 published 文稿，或所引用版本不是当前 published 版本。"""

    code = "document_not_published"


def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _read_builtin(filename) -> str:
    path = _DOCS_DIR / filename
    return path.read_text(encoding="utf-8")


def content_sha256(text) -> str:
    """文稿正文的 SHA-256（注册表与页面展示的同一口径）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def builtin_documents() -> list:
    """内置文稿清单（含正文与 hash；不落库，/legal 页面直接用）。"""
    out = []
    for doc_type in DOCUMENT_TYPES:
        slug, title, filename = _BUILTIN[doc_type]
        content = _read_builtin(filename)
        out.append({
            "document_type": doc_type,
            "slug": slug,
            "title": title,
            "version": BUILTIN_VERSION,
            "locale": DEFAULT_LOCALE,
            "content": content,
            "content_sha256": content_sha256(content),
            "content_path": "legal_docs/%s" % filename,
        })
    return out


def get_builtin_document(slug, version=None) -> dict | None:
    """按 URL slug + 可选版本取内置文稿；未知 slug 或版本不匹配返回 None。"""
    doc_type = SLUG_TO_TYPE.get(slug)
    if doc_type is None:
        return None
    if version is not None and version != BUILTIN_VERSION:
        return None
    for doc in builtin_documents():
        if doc["document_type"] == doc_type:
            return doc
    return None  # pragma: no cover - slug 命中则必然有文稿


def ensure_builtin_documents(conn=None) -> None:
    """把内置文稿登记进注册表（幂等；status 缺省 draft，不覆盖已发布状态）。

    已存在同主键行时只校验 hash/定位一致——不一致抛 DocumentConflictError，
    绝不原地 UPDATE 内容字段（发布记录不可变）。已有 published/retired 状态
    的行保持原状态（重复登记不降级）。
    """
    own = conn is None
    c = conn or _connect()
    try:
        with pg_store.transaction(c) as tx:
            with tx.cursor() as cur:
                for doc in builtin_documents():
                    cur.execute(
                        "SELECT content_sha256, content_path FROM agreement_documents "
                        "WHERE document_type=%s AND version=%s AND locale=%s",
                        (doc["document_type"], doc["version"], doc["locale"]))
                    row = cur.fetchone()
                    if row is not None:
                        if (row["content_sha256"] != doc["content_sha256"]
                                or row["content_path"] != doc["content_path"]):
                            raise DocumentConflictError(
                                "协议文稿 %s/%s 已登记内容与磁盘文件不一致，"
                                "禁止沿用相同 version 发布不同内容"
                                % (doc["document_type"], doc["version"]))
                        continue
                    cur.execute(
                        "INSERT INTO agreement_documents "
                        "(document_type, version, locale, title, content_sha256, "
                        " content_path, status) "
                        "VALUES (%s,%s,%s,%s,%s,%s,'draft') "
                        "ON CONFLICT (document_type, version, locale) DO NOTHING",
                        (doc["document_type"], doc["version"], doc["locale"],
                         doc["title"], doc["content_sha256"], doc["content_path"]))
    finally:
        if own:
            c.close()


def get_document(document_type, version, locale=DEFAULT_LOCALE) -> dict | None:
    """按主键读注册表行（无行返回 None）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT document_type, version, locale, title, content_sha256, "
                "content_path, status, published_at, effective_at, "
                "requires_reconsent, created_at "
                "FROM agreement_documents "
                "WHERE document_type=%s AND version=%s AND locale=%s",
                (document_type, version, locale))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def current_published(document_type, locale=DEFAULT_LOCALE) -> dict | None:
    """当前 published 文稿（无则 None——调用方据此 fail-closed）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT document_type, version, locale, title, content_sha256, "
                "content_path, status, published_at, effective_at, "
                "requires_reconsent, created_at "
                "FROM agreement_documents "
                "WHERE document_type=%s AND locale=%s AND status='published'",
                (document_type, locale))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def list_documents(document_type=None) -> list:
    """注册表全量/按类型列表（后台核对用；按类型+发布时间排序）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if document_type is None:
                cur.execute(
                    "SELECT document_type, version, locale, title, content_sha256, "
                    "content_path, status, published_at, effective_at, "
                    "requires_reconsent, created_at FROM agreement_documents "
                    "ORDER BY document_type, created_at")
            else:
                cur.execute(
                    "SELECT document_type, version, locale, title, content_sha256, "
                    "content_path, status, published_at, effective_at, "
                    "requires_reconsent, created_at FROM agreement_documents "
                    "WHERE document_type=%s ORDER BY created_at", (document_type,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def publish_document(document_type, version, locale=DEFAULT_LOCALE,
                     requires_reconsent=False) -> dict:
    """发布一个已登记文稿：同事务 retire 同类型同语言旧 published 行。

    只能发布注册表里已存在（且 hash 与磁盘一致的）行；发布后
    ``current_published`` 返回新版本。retired 行永不再回到 published
    （同 version 重新发布须先人工核对状态机；本函数对 retired 行抛错，
    防止旧文稿被静默复活）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as tx:
            with tx.cursor() as cur:
                cur.execute(
                    "SELECT status, content_sha256, content_path, title "
                    "FROM agreement_documents "
                    "WHERE document_type=%s AND version=%s AND locale=%s "
                    "FOR UPDATE",
                    (document_type, version, locale))
                row = cur.fetchone()
                if row is None:
                    raise DocumentNotFoundError(
                        "协议文稿未登记：%s/%s/%s" % (document_type, version, locale))
                if row["status"] == "retired":
                    raise AgreementStoreError(
                        "已下线文稿不得重新发布（%s/%s）" % (document_type, version))
                # 防御：发布前再核对磁盘内容与注册表一致
                for doc in builtin_documents():
                    if doc["document_type"] == document_type and doc["version"] == version:
                        if doc["content_sha256"] != row["content_sha256"]:
                            raise DocumentConflictError(
                                "协议文稿 %s/%s 注册表 hash 与磁盘文件不一致，拒绝发布"
                                % (document_type, version))
                        break
                if row["status"] == "published":
                    pass  # 幂等：重复发布同一版本不改动
                else:
                    cur.execute(
                        "UPDATE agreement_documents SET status='retired' "
                        "WHERE document_type=%s AND locale=%s AND status='published'",
                        (document_type, locale))
                    cur.execute(
                        "UPDATE agreement_documents "
                        "SET status='published', published_at=now(), "
                        "    effective_at=now(), requires_reconsent=%s "
                        "WHERE document_type=%s AND version=%s AND locale=%s",
                        (bool(requires_reconsent), document_type, version, locale))
                cur.execute(
                    "SELECT document_type, version, locale, title, content_sha256, "
                    "content_path, status, published_at, effective_at, "
                    "requires_reconsent, created_at FROM agreement_documents "
                    "WHERE document_type=%s AND version=%s AND locale=%s",
                    (document_type, version, locale))
                return dict(cur.fetchone())
    finally:
        conn.close()


def require_published_document(document_type, version, sha256,
                               locale=DEFAULT_LOCALE) -> dict:
    """研究授权/接受凭据的文稿校验：必须是**当前 published** 版本且 hash 一致。

    - 无当前 published → DocumentNotPublishedError（缺当前发布文稿时不得
      接受/授权，§3.2 fail-closed）；
    - 引用版本 ≠ 当前 published 版本 → DocumentNotPublishedError（旧版本
      不能接受为新授权）；
    - hash 不一致 → DocumentNotPublishedError（版本/hash 不匹配拒绝）。
    """
    doc = current_published(document_type, locale)
    if doc is None or doc["version"] != version:
        raise DocumentNotPublishedError(
            "协议 %s 没有版本为 %s 的当前发布文稿" % (document_type, version))
    if (sha256 or "").strip().lower() != doc["content_sha256"]:
        raise DocumentNotPublishedError(
            "协议 %s 版本 %s 的内容摘要不匹配" % (document_type, version))
    return doc
