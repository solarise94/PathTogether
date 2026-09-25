# -*- coding: utf-8 -*-
"""slide_store —— 切片资产的 PG 身份/状态权威（slide ID 化重构 P1-A）。

本模块是「代码旁合同」：以下状态机、锁顺序、接口语义镜像自
docs/slide-id-refactor-p1-contract-20260925.md（§3.1/§4/§5）与
docs/slide-id-storage-refactor-agent-plan-20260925.md §2.3；偏离需在 review
中显式裁决。schema 底座是 migrations/0067_slide_asset_identity.sql。
worker 可 import 本模块（不得 import app；本模块只依赖 pg_store/slide_storage）。

身份与 ID（合同 §1）
  - slide_id 沿用现行生成器 ``"sld_" + secrets.token_urlsafe(9)``（12 位
    urlsafe），服务端随机、DB 主键兜底唯一；不从原名/owner/内容哈希推导；
    不重置旧 ID。
  - 一次新的逻辑切片上传 = 一个新 slide_id；幂等重试复用原任务及其 ID
    （任务表 slide_id 列/upload_task_items 是持久绑定，P3 接线）；删除后
    重传用新 ID。
  - original_filename/display_name 只是展示；``legacy_filename`` 是冻结别名：
    新资产恒 NULL；已删除资产的 tombstone 行保留 legacy_filename
    （UNIQUE 约束天然阻止旧别名重绑新 ID）。

状态机与可见性（合同 §4）
::

    allocate_slide
        │
        ▼
     staging ──publish 成功──▶ ready ──request_delete──▶ deleting ──清理+结算完成──▶ deleted
        │                      │                            │
        ├─任务失败/取消─▶ failed│                            └─清理失败：停留 deleting 重试（slide_delete_jobs）
        │   （staging 清理后行可删或留 failed 证据） │
    legacy（0067 默认）──盘点/验证通过──▶ ready（layout 仍 'legacy'，P6 再迁 id_bundle）
    legacy ──验证失败（缺文件/归属歧义）──▶ failed（保留证据，不可读）

  - **DB ``asset_state='ready'`` 是唯一可见性开关**：所有读取入口（含旧端点经
    legacy alias 解析后）同受 authorize_read 门禁。
  - ``deleting`` 立即拒绝新读取授权；已取得的受控文件句柄可完成当次读取。
  - 不允许同一 slide_id 覆盖内容；换内容 = 新 slide_id。

锁顺序（合同 §5，全仓统一；任何新代码取多把锁必须按此顺序）::
::

    pg_advisory_xact_lock(hashtext('slide:' || slide_id))   ← 所有 publish/delete/recovery 的第一把锁（跨进程仲裁点；
                                                               'slide:' 前缀与既有 hashtext(job_id) 键空间隔离）
      → 任务行锁（upload_tasks / ingestion_jobs / conversion_jobs 行，SELECT ... FOR UPDATE）
      → slides 行锁
      → upload_reservations 行
      → upload_user_quotas 行
      → cos_pool_state 行

  已审计无环（合同 §5）：slides 行锁插在任务行之后不引入反向边；delete 路径
  只持 advisory+slides 行，不回头取任务锁。禁止「stat 相同就 unlink/转 owner」。

接口语义要点
  - ``allocate_slide``：创建 staging/id_bundle 资产行（storage_relpath 由
    slide_storage.entry_relpath 服务端派生）；**不查原名是否已存在**；owner
    必须显式（无 UID 的本地模式由调用方先解析配置 owner，不允许空 owner
    自动认领）。
  - ``resolve_slide_id``：校验存在性；缺失不凭 ``sld_`` 前缀当成功。
  - ``resolve_legacy_alias``：仅查 legacy_filename 冻结映射，再进入同一
    resolver 语义；无文件系统猜测。
  - ``authorize_read``：统一门禁（见函数 docstring 的权限序）；DB 异常按
    拒绝处理，不回退目录扫描。
  - 状态迁移原语（mark_ready/request_delete/mark_deleted/mark_failed）全部
    是带 ``expected_state`` 谓词的真实 SQL CAS，返回是否迁移成功。
  - 名称/元数据编辑（update_display_name/update_note/set_public）只动
    元数据：不动文件、不动 legacy_filename、不动授权。
"""

import contextlib
import logging
import re
import secrets
from dataclasses import dataclass

import psycopg

import pg_store
import slide_storage

_LOG = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
class SlideState:
    """slides.asset_state 的合法值（合同 §2.1/§4；与 0067 CHECK 一致）。"""

    STAGING = "staging"      # 已分配 ID、尚未发布（不可读）
    LEGACY = "legacy"        # 0067 默认：待验证历史资产（不可读，回填脚本迁移）
    READY = "ready"          # 已发布：唯一可读状态
    DELETING = "deleting"    # 已请求删除：立即拒绝新读取授权
    DELETED = "deleted"      # 物理清理+结算完成（tombstone；保留 legacy_filename）
    FAILED = "failed"        # 任务失败/验证失败（保留证据，不可读）

    ALL = frozenset({
        STAGING, LEGACY, READY, DELETING, DELETED, FAILED,
    })


class StorageLayout:
    """slides.storage_layout 的合法值（合同 §2.1）。"""

    LEGACY = "legacy"        # UPLOAD_DIR 根下历史平铺布局（过渡，R-16）
    ID_BUNDLE = "id_bundle"  # objects/<slide_id>/ 独占包布局


#: 平台管理角色（admin 语义 = user_store.ROLE_OWNER；本模块不 import
#: user_store，保持 worker 依赖最小，值由 0001 的角色词表固定为 'owner'）。
ROLE_ADMIN = "owner"

#: format_ext 白名单归一：小写字母数字、1..16 位（真实格式白名单校验在
#: 上传通道的格式注册器完成；此处是 DB 侧兜底归一）。
_FORMAT_EXT_RE = re.compile(r"^[a-z0-9]{1,16}$")

#: original_filename / display_name 的服务端边界（R-19：不接受目录语义、
#: 限制长度/控制字符；输出转义由 HTTP 层负责，防 CRLF 注入）。
_MAX_FILENAME_BYTES = 255
_MAX_DISPLAY_NAME_LEN = 200
_MAX_NOTE_LEN = 500


# --------------------------------------------------------------------------- #
# 连接（share_store_pg 同款：pg_store.connect + dict_row）
# --------------------------------------------------------------------------- #
def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


@contextlib.contextmanager
def _session(conn=None):
    """调用方事务复用：传入 conn 则原样 yield（不 commit/不关）；否则自建
    连接 + pg_store.transaction（commit on success / rollback on error）。"""
    if conn is not None:
        yield conn
        return
    c = _connect()
    try:
        with pg_store.transaction(c):
            yield c
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# descriptor
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SlideDescriptor:
    """切片资产的受控视图（合同 §3.1）。

    路径字段（storage_relpath）仅内部使用，**绝不序列化进 HTTP 响应**
    （R-20）；legacy_filename 是冻结别名，仅兼容解析用。
    """

    slide_id: str
    owner_user_id: str | None
    original_filename: str | None
    display_name: str
    legacy_filename: str | None
    format_ext: str | None
    asset_state: str
    storage_layout: str
    storage_relpath: str | None
    accounted_bytes: int | None
    public: bool
    note: str
    published_at: float | None
    deleted_at: float | None
    revision: str | None


_DESCRIPTOR_SQL = """
SELECT s.slide_id, s.owner_user_id, s.original_filename, s.display_name,
       s.legacy_filename, s.format_ext, s.asset_state, s.storage_layout,
       s.storage_relpath, s.accounted_bytes, s.public, s.note,
       extract(epoch from s.published_at)::float8 AS published_at,
       extract(epoch from s.deleted_at)::float8 AS deleted_at,
       (SELECT a.legacy_revision FROM slide_assets a
         WHERE a.slide_id = s.slide_id
         ORDER BY a.created_at DESC, a.asset_id DESC LIMIT 1) AS revision
  FROM slides s
"""


def _row_to_descriptor(row):
    if row is None:
        return None
    return SlideDescriptor(
        slide_id=row["slide_id"],
        owner_user_id=row["owner_user_id"],
        original_filename=row["original_filename"],
        display_name=row["display_name"] or "",
        legacy_filename=row["legacy_filename"],
        format_ext=row["format_ext"],
        asset_state=row["asset_state"],
        storage_layout=row["storage_layout"],
        storage_relpath=row["storage_relpath"],
        accounted_bytes=(int(row["accounted_bytes"])
                         if row["accounted_bytes"] is not None else None),
        public=bool(row["public"]),
        note=row["note"] or "",
        published_at=row["published_at"],
        deleted_at=row["deleted_at"],
        revision=row["revision"],
    )


# --------------------------------------------------------------------------- #
# 输入归一（R-19 / R-02）
# --------------------------------------------------------------------------- #
def _new_slide_id() -> str:
    """沿用现行生成器（合同 §1；share_store_pg._new_slide_id 同款）。"""
    return "sld_" + secrets.token_urlsafe(9)  # 12 位 urlsafe


def sanitize_original_filename(name: str) -> str:
    """original_filename 展示快照的服务端边界（R-19）。

    不接受目录语义（含 ``/``、``\\``）、拒绝控制字符/空串/超长（>255 字节
    UTF-8）。违规抛 ValueError——上传通道应先做白名单校验，这里是资产层的
    最后闸门，绝不静默截取 basename。
    """
    if not isinstance(name, str):
        raise ValueError("original_filename 必须是字符串")
    s = name.strip()
    if not s:
        raise ValueError("original_filename 不能为空")
    if len(s.encode("utf-8")) > _MAX_FILENAME_BYTES:
        raise ValueError("original_filename 超过 %d 字节" % _MAX_FILENAME_BYTES)
    if "/" in s or "\\" in s:
        raise ValueError("original_filename 不接受目录语义")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in s):
        raise ValueError("original_filename 含控制字符")
    return s


def normalize_format_ext(ext) -> str:
    """format_ext 归一：去点、小写、白名单 ``^[a-z0-9]{1,16}$``（DB 兜底）。"""
    if not isinstance(ext, str):
        raise ValueError("format_ext 必须是字符串")
    e = ext.strip().lower().lstrip(".")
    if not _FORMAT_EXT_RE.match(e):
        raise ValueError("format_ext 非白名单小写扩展名：%r" % (ext,))
    return e


def _clean_display_name(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("display_name 必须是字符串")
    s = value.strip()
    if len(s) > _MAX_DISPLAY_NAME_LEN:
        raise ValueError("display_name 超过 %d 字符" % _MAX_DISPLAY_NAME_LEN)
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in s):
        raise ValueError("display_name 含控制字符")
    return s


def _clean_note(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("note 必须是字符串")
    s = value.strip()
    if len(s) > _MAX_NOTE_LEN:
        raise ValueError("note 超过 %d 字符" % _MAX_NOTE_LEN)
    return s


# --------------------------------------------------------------------------- #
# 资产分配与解析（合同 §3.1）
# --------------------------------------------------------------------------- #
def allocate_slide(owner_user_id, original_filename, format_ext, *,
                   display_name=None, note=None, conn=None) -> SlideDescriptor:
    """分配一个新切片资产行（asset_state='staging'、storage_layout='id_bundle'）。

    合同要点（§1/§3.1）：
      - **一次新的逻辑切片上传 = 一个新 slide_id**：本函数不查原名是否已
        存在——同账号/不同账号上传同名、同内容文件都是独立资产；幂等重试
        复用原任务及其 ID 的职责在任务表绑定（upload_tasks.slide_id /
        upload_task_items，P3 接线），不在本函数。
      - owner 必须显式非空：无 UID 的本地模式由调用方先解析配置 owner，
        不允许空 owner 自动认领（违规抛 ValueError）。
      - storage_relpath 由 slide_storage.entry_relpath 服务端派生
        （``objects/<slide_id>/data.<format_ext>``），唯一且不可变（R-20），
        客户端不可设置；legacy_filename 恒 NULL（新资产不写冻结别名）。
      - display_name 初始 = original_filename（R-02）。
    """
    if not isinstance(owner_user_id, str) or not owner_user_id.strip():
        raise ValueError("allocate_slide 需要显式 owner_user_id（不允许空 owner）")
    owner = owner_user_id.strip()
    orig = sanitize_original_filename(original_filename)
    ext = normalize_format_ext(format_ext)
    disp = _clean_display_name(display_name) or orig
    note_clean = _clean_note(note)

    with _session(conn) as c:
        slide_id = _new_slide_id()
        relpath = slide_storage.entry_relpath(slide_id, ext)
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO slides (slide_id, original_filename, display_name, "
                "note, owner_user_id, format_ext, asset_state, storage_layout, "
                "storage_relpath) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (slide_id, orig, disp, note_clean, owner, ext,
                 SlideState.STAGING, StorageLayout.ID_BUNDLE, relpath),
            )
        return _fetch_descriptor(c, slide_id)


def _fetch_descriptor(conn, slide_id):
    """按主键取 descriptor（conn 是 _session yield 出的连接/事务）。"""
    with conn.cursor() as cur:
        cur.execute(_DESCRIPTOR_SQL + " WHERE s.slide_id=%s", (slide_id,))
        return _row_to_descriptor(cur.fetchone())


def resolve_slide_id(slide_id, *, conn=None):
    """slide_id → SlideDescriptor | None。

    校验存在性：缺失/未知 ID 返回 None，**不凭 ``sld_`` 前缀当成功**
    （合同 §3.1；与 share_store_pg.resolve_slide_ref 的前缀直返语义不同，
    那是 P1 起仅限迁移兼容层使用的旧口径）。本函数只解析、不鉴权——
    可见性门禁统一在 authorize_read。
    """
    if not isinstance(slide_id, str) or not slide_id.strip():
        return None
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(_DESCRIPTOR_SQL + " WHERE s.slide_id=%s",
                        (slide_id.strip(),))
            return _row_to_descriptor(cur.fetchone())


def resolve_legacy_alias(alias, *, conn=None):
    """冻结别名 → SlideDescriptor | None（合同 §3.1 / R-01）。

    仅查 ``slides.legacy_filename`` 冻结映射（新资产恒 NULL），无文件系统
    猜测、不做原名目录扫描；解析结果进入与 resolve_slide_id 完全相同的
    descriptor 语义（可见性仍由 authorize_read 门禁裁决）。旧值永不重绑
    新 ID——tombstone 行保留 legacy_filename，UNIQUE 约束兜底。
    """
    if not isinstance(alias, str) or not alias.strip():
        return None
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(_DESCRIPTOR_SQL + " WHERE s.legacy_filename=%s",
                        (alias.strip(),))
            return _row_to_descriptor(cur.fetchone())


# --------------------------------------------------------------------------- #
# 统一读门禁（合同 §3.1/§4）
# --------------------------------------------------------------------------- #
def authorize_read(desc, *, actor_user_id=None, actor_role=None,
                   demo_capability=False, conn=None) -> bool:
    """统一读取门禁：所有读取入口（新旧端点、分享进程、插件、AI）共用。

    判定序（合同 §3.1，顺序即裁决优先级）：
      0. ``asset_state == 'ready'`` 是唯一可见性开关——staging/legacy/
         deleting/deleted/failed 一律拒（legacy 也要等回填验证通过才 ready）。
         **门禁以 DB 当前行值为准**（重读 asset_state/owner/public——
         descriptor 只是解析快照，防快照过期/并发置 deleting 后仍放行）。
      1. admin 角色（平台 owner，管理面读）；
      2. owner（actor_user_id == 当前行 owner_user_id）；
      3. public（当前行值）；
      4. slide_id 级 slide_view_grants（**只认 slide_id 列**，不认
         slide_name——R-06：旧名授权不再匹配新内容）；
      5. share_slides ⋈ grants：已领取（grants.active）未撤销
         （shares.revoked=false）未过期（expires_at NULL 或 > now）且 grant
         含 view 权限；shares.slides JSONB 快照**不参与**判定（R-04）；
      6. demo_catalog capability（调用方显式声明 demo 通道；allowlist
         命中才放行——R-10：删除后 capability 不能读）。

    fail-closed：任何 DB 异常按拒绝处理，不回退目录扫描/名称猜测。
    desc 可传 SlideDescriptor 或 slide_id 字符串（内部走 resolve_slide_id，
    缺失即拒）。
    """
    try:
        if desc is None:
            return False
        if isinstance(desc, str):
            desc = resolve_slide_id(desc, conn=conn)
            if desc is None:
                return False
        # 0) 唯一可见性开关——以 DB 当前行值为准（重读防快照过期）
        with _session(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT asset_state, owner_user_id, public "
                    "FROM slides WHERE slide_id=%s", (desc.slide_id,))
                row = cur.fetchone()
        if row is None or row["asset_state"] != SlideState.READY:
            return False
        # 1) admin 角色
        if actor_role == ROLE_ADMIN:
            return True
        uid = (actor_user_id or "").strip() or None
        # 2) owner
        if uid and row["owner_user_id"] and uid == row["owner_user_id"]:
            return True
        # 3) public
        if row["public"]:
            return True
        # 4~5) 显式授权/share 成员：需要具体主体
        if uid:
            if _has_slide_view_grant(uid, desc.slide_id, conn=conn):
                return True
            if _has_active_share_membership(uid, desc.slide_id, conn=conn):
                return True
        # 6) demo capability（独立于主体——匿名 demo 通道也走 allowlist，
        #    命中才放行）
        if demo_capability and _in_demo_catalog(desc.slide_id, conn=conn):
            return True
        return False
    except Exception:  # noqa: BLE001 - DB 异常按拒处理（fail-closed）
        _LOG.warning("authorize_read fail-closed（slide_id=%s）",
                     getattr(desc, "slide_id", desc), exc_info=True)
        return False


def _has_slide_view_grant(user_id, slide_id, *, conn=None) -> bool:
    """slide_id 级显式授权（R-06：只认 slide_id 列，不回退 slide_name 匹配）。"""
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM slide_view_grants "
                "WHERE slide_id=%s AND user_id=%s LIMIT 1",
                (slide_id, user_id),
            )
            return cur.fetchone() is not None


def _has_active_share_membership(user_id, slide_id, *, conn=None) -> bool:
    """share_slides ⋈ grants 成员判定：已领取未撤销未过期且含 view 权限。"""
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM grants g "
                "JOIN shares sh ON sh.token = g.token "
                "JOIN share_slides ss ON ss.token = sh.token "
                "WHERE g.user_id=%s AND g.active "
                "AND ss.slide_id=%s "
                "AND sh.revoked = FALSE "
                "AND (sh.expires_at IS NULL OR sh.expires_at > now()) "
                "AND g.permissions @> '[\"view\"]'::jsonb "
                "LIMIT 1",
                (user_id, slide_id),
            )
            return cur.fetchone() is not None


def _in_demo_catalog(slide_id, *, conn=None) -> bool:
    """demo_catalog allowlist 命中（R-10；capability 通道的库层校验）。"""
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute("SELECT 1 FROM demo_catalog WHERE slide_id=%s LIMIT 1",
                        (slide_id,))
            return cur.fetchone() is not None


# --------------------------------------------------------------------------- #
# 状态迁移原语（合同 §3.1/§4：全部 expected_state 谓词 SQL CAS）
# --------------------------------------------------------------------------- #
def _cas_state(slide_id, expected_state, new_state, assignments="",
               assignment_params=(), *, conn=None) -> bool:
    """真实 CAS：UPDATE ... WHERE slide_id=%s AND asset_state=%s。

    assignments 是额外 SET 片段（如 accounted_bytes=%s），assignment_params
    是其占位参数。返回是否迁移成功（rowcount==1）；expected_state 不匹配
    即失败，不猜当前状态。
    """
    if expected_state not in SlideState.ALL:
        raise ValueError("未知 expected_state：%r" % (expected_state,))
    sql = ("UPDATE slides SET asset_state=%s, updated_at=now()"
           + ((", " + assignments) if assignments else "")
           + " WHERE slide_id=%s AND asset_state=%s")
    params = [new_state, *assignment_params, slide_id, expected_state]
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount == 1


def mark_ready(slide_id, *, accounted_bytes=None,
               expected_state=SlideState.STAGING, conn=None) -> bool:
    """→ ready（合同 §4：publish 成功 / legacy 验证通过）。

    - 新资产：publish 编排在短事务内调用（expected_state=STAGING），并把
      ``accounted_bytes`` 与 ready 同事务设置（R-12：删除结算用它）。
    - legacy 回填：scripts/backfill_slide_asset_state.py 以
      expected_state=LEGACY 调用（layout 保持 legacy，P6 再迁 id_bundle）。
    一次性 CAS：staging/legacy 之外的状态（含重复 publish）返回 False。
    """
    if accounted_bytes is not None and int(accounted_bytes) < 0:
        raise ValueError("accounted_bytes 不能为负")
    assignments = "published_at=now()"
    params = []
    if accounted_bytes is not None:
        assignments += ", accounted_bytes=%s"
        params.append(int(accounted_bytes))
    return _cas_state(slide_id, expected_state, SlideState.READY,
                      assignments, params, conn=conn)


def request_delete(slide_id, *, expected_state=SlideState.READY, conn=None) -> bool:
    """ready → deleting：立即拒绝后续新读取授权（合同 §4）。

    只做 CAS；幂等清理工作（slide_delete_jobs）的生成在 P5 删除编排接线。
    """
    return _cas_state(slide_id, expected_state, SlideState.DELETING,
                      conn=conn)


def mark_deleted(slide_id, *, expected_state=SlideState.DELETING, conn=None) -> bool:
    """deleting → deleted：物理清理+结算完成后由删除 worker 调用。

    tombstone 行保留（含 legacy_filename，阻止旧别名重绑新 ID）；结算
    （按 accounted_bytes 幂等减少 used_bytes）在删除编排事务内完成（P5）。
    """
    return _cas_state(slide_id, expected_state, SlideState.DELETED,
                      "deleted_at=now()", conn=conn)


def mark_failed(slide_id, *, expected_state=SlideState.STAGING, conn=None) -> bool:
    """→ failed：staging 任务失败/取消，或 legacy 验证失败（合同 §4）。

    failed 保留证据、不可读；staging 残留文件的清理由任务侧负责
    （行可删或留 failed 证据）。
    """
    return _cas_state(slide_id, expected_state, SlideState.FAILED,
                      conn=conn)


# --------------------------------------------------------------------------- #
# 元数据编辑（合同 §3.1：只动元数据，不动文件/legacy_filename/授权）
# --------------------------------------------------------------------------- #
def _update_meta_column(slide_id, column, value, *, conn=None) -> bool:
    """UPDATE 单列元数据；返回行是否存在（rowcount==1）。不改 asset_state
    语义、不触碰 legacy_filename/授权/存储。"""
    sql = ("UPDATE slides SET %s=%%s, updated_at=now() WHERE slide_id=%%s"
           % column)
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(sql, (value, slide_id))
            return cur.rowcount == 1


def update_display_name(slide_id, display_name, *, conn=None) -> bool:
    """改显示名（R-02：display_name 是唯一可编辑展示名）。

    不动文件、不动 legacy_filename、不动授权、不触发内容 revision 变化
    （改名不串片）。行缺失返回 False。
    """
    return _update_meta_column(slide_id, "display_name",
                               _clean_display_name(display_name), conn=conn)


def update_note(slide_id, note, *, conn=None) -> bool:
    """改备注：只动元数据。行缺失返回 False。"""
    return _update_meta_column(slide_id, "note", _clean_note(note), conn=conn)


def set_public(slide_id, public, *, conn=None) -> bool:
    """切换 public 可见位：只动元数据（授权关系不受影响）。行缺失返回 False。"""
    return _update_meta_column(slide_id, "public", bool(public), conn=conn)


# --------------------------------------------------------------------------- #
# 跨进程仲裁锁（合同 §5 的第一把锁；publish/delete/recovery 共用）
# --------------------------------------------------------------------------- #
def acquire_slide_lock(cur, slide_id):
    """事务级 advisory 锁：``pg_advisory_xact_lock(hashtext('slide:' || slide_id))``。

    所有 publish/delete/recovery 的**第一把锁**（跨进程仲裁点）。'slide:'
    前缀与既有 ``hashtext(job_id)`` 键空间隔离；随事务结束自动释放。
    必须在取任务行锁/slides 行锁之前调用（锁序见模块 docstring）。
    cur 是已开启事务的 cursor（调用方管理事务边界）。
    """
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('slide:' || %s))",
                (slide_id,))
