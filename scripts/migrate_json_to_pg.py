#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JSON → PostgreSQL 一次性迁移工具（Stage 3b-3）。

把 SHARE_DATA_DIR 下的 ``shares.json`` / ``users.json`` 全量导入 PostgreSQL，
作为把 ``STORAGE_BACKEND`` 从 ``json`` 切到 ``postgres`` 的过渡。与 3b-2 的
dispatcher / PG 仓储实现配套使用。

账户系统批次 C（docs §4.2）：users.json 记录键与 PG 物理列均已收口为
``login_id``（0016）。读侧兼容旧格式 json（记录只有 ``email`` 键时读为
login_id，口径同 user_store_json._login_id_of）。

**前置假设（工具自身不强制，由运维保证）**：迁移在「停写窗口」执行——
迁移期间 json 写路径必须停止（关停 gunicorn / share_server），避免边导边写。
本工具**只读 json**（``json.load`` 直读，不经 dispatcher，与 STORAGE_BACKEND 无关），
所有写操作落 PG。

子命令：
  dry-run    只读 json + 磁盘，输出将导入的实体计数与潜在问题清单，**不写 pg**。
  apply      执行导入（单事务；幂等——重跑不产生重复，用 ON CONFLICT / 删后重建）。
             --backup-dir 指定回滚备份目录（默认 SHARE_DATA_DIR/migration-backup-<ts>/）。
             导入前把源 json 原样复制进备份目录；导入完成后把身份映射表存成 mapping.json。
  verify     双读核对：逐实体对比 json 与 pg（计数 + 关键字段），输出差异报告；
             0 差异打印 OK，有差异 exit 2 并列前 20 条。
  rollback   从备份目录把 json 拷回 SHARE_DATA_DIR，并打印切回 json 的指引。

用法示例::

    python3 scripts/migrate_json_to_pg.py dry-run
    python3 scripts/migrate_json_to_pg.py apply
    python3 scripts/migrate_json_to_pg.py verify
    python3 scripts/migrate_json_to_pg.py apply --backup-dir /tmp/bk
    python3 scripts/migrate_json_to_pg.py rollback --backup-dir /tmp/bk --yes

连接配置走 pg_store（DATABASE_URL 或 PGHOST/... 环境变量）。
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

# 把仓库根加入 sys.path 以便 import pg_store（脚本可从任意 cwd 运行）。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pg_store  # noqa: E402  （sys.path 已含仓库根）


# --------------------------------------------------------------------------- #
# 路径解析
# --------------------------------------------------------------------------- #
def _default_share_data_dir():
    return Path(os.environ.get("SHARE_DATA_DIR")
                or (Path.home() / "svs-viewer" / "share-data"))


def _default_upload_dir():
    return Path(os.environ.get("UPLOAD_DIR")
                or (Path.home() / "svs-viewer" / "uploads"))


def _shares_file(share_data_dir):
    return Path(share_data_dir) / "shares.json"


def _users_file(share_data_dir):
    return Path(share_data_dir) / "users.json"


# --------------------------------------------------------------------------- #
# JSON 读取（与 share_store_json._load_locked 的兜底结构对齐，但不触发迁移）
# --------------------------------------------------------------------------- #
def _empty_shares():
    return {"shares": {}, "rois": [], "projects": {}, "slide_meta": {},
            "change_seq_by_slide": {}, "grants": []}


def _empty_users():
    return {"users": {}, "meta": {"schema_version": 1}}


def _load_json(path):
    """原样 json.load；文件缺失/空/损坏返回空结构（不备份——工具只读）。"""
    p = Path(path)
    if not p.is_file():
        return None
    raw = p.read_text(encoding="utf-8")
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _load_shares(share_data_dir):
    data = _load_json(_shares_file(share_data_dir))
    if data is None:
        return _empty_shares()
    if not isinstance(data, dict):
        return _empty_shares()
    for k, default in (("shares", {}), ("rois", []), ("projects", {}),
                       ("slide_meta", {}), ("change_seq_by_slide", {}),
                       ("grants", [])):
        if not isinstance(data.get(k), type(default)):
            data[k] = default
    return data


def _load_users(share_data_dir):
    data = _load_json(_users_file(share_data_dir))
    if data is None:
        return _empty_users()
    if not isinstance(data, dict):
        return _empty_users()
    if not isinstance(data.get("users"), dict):
        data["users"] = {}
    return data


def _login_id_of(u) -> str:
    """users.json 记录读侧兼容（批次 C）：键 ``login_id`` 优先，旧格式记录
    只有 ``email`` 键时读为 login_id（口径同 user_store_json._login_id_of）。"""
    if not isinstance(u, dict):
        return ""
    v = u.get("login_id")
    if v is None and "email" in u:
        v = u.get("email")
    return str(v or "").strip().lower()


# --------------------------------------------------------------------------- #
# 校验（dry-run：纯 json + 磁盘，不碰 pg）
# --------------------------------------------------------------------------- #
_GEOM_KEYS = ("x", "y", "side_px", "size_mm", "x1", "y1", "x2", "y2", "points")


def _all_referenced_slides(shares):
    """汇总所有被引用的 legacy_filename（slide_meta ∪ shares.slides ∪
    projects.slides ∪ rois.slide）。"""
    names = set()
    for n in (shares.get("slide_meta") or {}):
        if isinstance(n, str):
            names.add(n)
    for sh in (shares.get("shares") or {}).values():
        for s in (sh.get("slides") or []) if isinstance(sh, dict) else []:
            if isinstance(s, str):
                names.add(s)
    for proj in (shares.get("projects") or {}).values():
        for s in (proj.get("slides") or []) if isinstance(proj, dict) else []:
            if isinstance(s, str):
                names.add(s)
    for r in shares.get("rois") or []:
        s = r.get("slide") if isinstance(r, dict) else None
        if isinstance(s, str):
            names.add(s)
    return names


def _scan_problems(shares, users, upload_dir):
    """json + 磁盘层面的潜在问题（不碰 pg）。返回 list[str]。"""
    problems = []

    # 1. rois annotation_id 重复（UNIQUE 约束会失败）
    seen_aid = {}
    for i, r in enumerate(shares.get("rois") or []):
        aid = r.get("annotation_id") if isinstance(r, dict) else None
        if not aid:
            problems.append("rois[%d] 缺少 annotation_id（跳过该条）" % i)
            continue
        if aid in seen_aid:
            problems.append("rois annotation_id 重复：%s（rois[%d] 与 rois[%d]）"
                            % (aid, seen_aid[aid], i))
        else:
            seen_aid[aid] = i

    # 2. users login_id 重复（lower(login_id) 唯一索引会失败）。旧格式记录
    #    只有 email 键时读为 login_id（批次 C 读侧兼容）
    seen_login_id = {}
    for uid, u in (users.get("users") or {}).items():
        login_id = _login_id_of(u)
        login_id = str(login_id).strip().lower()
        if not login_id:
            problems.append("user %s 缺少 login_id（跳过）" % uid)
            continue
        if login_id in seen_login_id:
            problems.append("user login_id 重复：%s（%s 与 %s）"
                            % (login_id, seen_login_id[login_id], uid))
        else:
            seen_login_id[login_id] = uid

    # 3. grants 引用未知 share_token（grants.token 有 FK → shares.token）
    share_tokens = set((shares.get("shares") or {}).keys())
    for i, g in enumerate(shares.get("grants") or []):
        tok = g.get("share_token") if isinstance(g, dict) else None
        if tok and tok not in share_tokens:
            problems.append("grants[%d] 引用未知 share_token：%s（FK 会失败）"
                            % (i, tok))

    # 4. 被引用切片的磁盘文件缺失（content_sha256 无法填，不阻塞）
    if upload_dir is not None:
        up = Path(upload_dir)
        for name in sorted(_all_referenced_slides(shares)):
            p = up / name
            if not p.exists():
                problems.append("切片文件缺失（仅 content_sha256 留空，不阻塞）：%s"
                                % name)

    return problems


def _counts(shares, users):
    """各实体计数（供 dry-run / 报告）。change_log = roi 派生行数。"""
    return {
        "users": len(users.get("users") or {}),
        "shares": len(shares.get("shares") or {}),
        "grants": len(shares.get("grants") or []),
        "rois": len(shares.get("rois") or []),
        "slide_meta": len(shares.get("slide_meta") or {}),
        "projects": len(shares.get("projects") or {}),
        "change_log": len(shares.get("rois") or []),
    }


# --------------------------------------------------------------------------- #
# PG 连接 + schema
# --------------------------------------------------------------------------- #
def _connect():
    """建连接并确保 schema 已应用。

    注意：pg_store.ensure_schema 的内部查询按元组行（row[0]）读取 schema_migrations，
    故**先**以默认元组行执行 ensure_schema，**再**切到 dict_row 供本工具的查询使用。
    """
    import psycopg
    conn = pg_store.connect()
    pg_store.ensure_schema(conn)
    conn.row_factory = psycopg.rows.dict_row
    return conn


# 稳定 slide_id：对 legacy_filename 取 sha256 前 16 位（重跑/部分失败后可复现）。
# 迁移跑在空库上；若 slides 行已存在则复用其 slide_id（见 _resolve_slide），不与此处冲突。
def _det_slide_id(name):
    h = hashlib.sha256(str(name).encode("utf-8")).hexdigest()
    return "sld_" + h[:16]


def _det_roi_id(annotation_id):
    h = hashlib.sha256(str(annotation_id).encode("utf-8")).hexdigest()
    return "roi_" + h[:16]


def _slide_file_stats(upload_dir, name):
    """返回 (legacy_revision, content_sha256)；文件缺失返回 (None, None)。"""
    p = Path(upload_dir) / name
    try:
        st = p.stat()
    except OSError:
        return None, None
    legacy_rev = "%s:%s" % (st.st_mtime_ns, st.st_size)
    sha = None
    if p.is_file():
        try:
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            sha = "sha256:" + h.hexdigest()
        except OSError:
            sha = None
    return legacy_rev, sha


# --------------------------------------------------------------------------- #
# 应用计划构建（只读 json + 现有 pg 行，不写）
# --------------------------------------------------------------------------- #
class _Plan:
    def __init__(self):
        self.counts = {}
        self.problems = []          # json/磁盘问题（dry-run 可见）
        self.pg_conflicts = []      # apply 期检测的 pg 同名冲突（owner 矛盾）
        self.slide_map = {}         # legacy_filename → slide_id
        self.user_map = {}          # login_id → user_id
        self.tokens = []            # share token 清单
        self.annotation_ids = []    # roi annotation_id 清单
        self.skipped_slides = set()  # 因冲突跳过 identity/asset 的切片名


def _build_plan(shares, users, upload_dir, conn):
    plan = _Plan()
    plan.counts = _counts(shares, users)
    plan.problems = _scan_problems(shares, users, upload_dir)

    # user_map：login_id → user_id（旧格式记录 email 键读为 login_id）
    for uid, u in (users.get("users") or {}).items():
        login_id = _login_id_of(u)
        if login_id:
            plan.user_map[login_id] = uid

    # tokens
    plan.tokens = sorted((shares.get("shares") or {}).keys())

    # annotation_ids
    plan.annotation_ids = [r.get("annotation_id") for r in (shares.get("rois") or [])
                           if isinstance(r, dict) and r.get("annotation_id")]

    # slide_map + 同名冲突检测（需读现有 pg slides 行）
    slide_meta = shares.get("slide_meta") or {}
    if conn is not None:
        for name in sorted(_all_referenced_slides(shares)):
            sid, conflict = _resolve_slide(conn, name, slide_meta.get(name))
            if conflict:
                plan.pg_conflicts.append(conflict)
                plan.skipped_slides.add(name)
            elif sid:
                plan.slide_map[name] = sid
    return plan


def _resolve_slide(conn, name, meta):
    """返回 (slide_id|None, conflict_msg|None)。

    - 现有 slides 行 owner 与 json slide_meta owner 均非空且不同 → 冲突（不猜测覆盖）。
    - 否则：现有则复用其 slide_id；无则用确定性 slide_id（apply 时据此 INSERT）。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT slide_id, owner_user_id FROM slides "
                    "WHERE legacy_filename=%s", (name,))
        row = cur.fetchone()
    if row is not None:
        existing_owner = row["owner_user_id"]
        json_owner = None
        if isinstance(meta, dict):
            json_owner = meta.get("owner_user_id")
        if (existing_owner is not None and json_owner is not None
                and str(existing_owner) != str(json_owner)):
            return None, ("同名切片 %s 在 PG 已有不同归属（pg owner=%s, json owner=%s），"
                          "拒绝猜测覆盖，跳过该切片"
                          % (name, existing_owner, json_owner))
        return row["slide_id"], None
    return _det_slide_id(name), None


# --------------------------------------------------------------------------- #
# apply：导入（单事务）
# --------------------------------------------------------------------------- #
def _import_users(cur, users):
    import psycopg
    n = 0
    for uid, u in (users.get("users") or {}).items():
        if not isinstance(u, dict):
            continue
        login_id = _login_id_of(u) or uid
        created = u.get("created_at") or time.time()
        cur.execute(
            "INSERT INTO users "
            "(user_id, login_id, display_name, password_hash, role, "
            " created_at, disabled, ai_config) "
            "VALUES (%s,%s,%s,%s,%s, to_timestamp(%s), %s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            " login_id=EXCLUDED.login_id, display_name=EXCLUDED.display_name, "
            " password_hash=EXCLUDED.password_hash, role=EXCLUDED.role, "
            " created_at=EXCLUDED.created_at, disabled=EXCLUDED.disabled, "
            " ai_config=EXCLUDED.ai_config",
            (uid, login_id, str(u.get("display_name") or "") or login_id,
             str(u.get("password_hash") or ""), str(u.get("role") or "user"),
             created, bool(u.get("disabled", False)),
             psycopg.types.json.Jsonb(u.get("ai_config") or {})),
        )
        n += 1
    return n


def _import_slides(cur, shares, upload_dir, skipped):
    """为所有被引用切片建/更新 slides 行（跳过 skipped），返回 {name: slide_id}。

    同名冲突切片已由 _build_plan 标记 skipped——这里对其 identity 行不动（保留 pg
    现状），但仍允许其 rois/shares 按名引用（它们无 FK 依赖 slides 行）。
    """
    import psycopg
    slide_meta = shares.get("slide_meta") or {}
    slide_map = {}
    names = sorted(_all_referenced_slides(shares))
    for name in names:
        if name in skipped:
            # 复用现有 slide_id（若否则 None——slide_assets 跳过）
            cur.execute("SELECT slide_id FROM slides WHERE legacy_filename=%s",
                        (name,))
            row = cur.fetchone()
            slide_map[name] = row["slide_id"] if row else None
            continue
        meta = slide_meta.get(name) if isinstance(slide_meta, dict) else None
        meta = meta if isinstance(meta, dict) else {}
        alias = str(meta.get("alias") or "")
        note = str(meta.get("note") or "")
        public = bool(meta.get("public", False))
        owner = meta.get("owner_user_id")
        sid = _det_slide_id(name)
        cur.execute(
            "INSERT INTO slides "
            "(slide_id, legacy_filename, display_name, alias, note, "
            " owner_user_id, public) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (legacy_filename) DO UPDATE SET "
            " display_name=EXCLUDED.display_name, alias=EXCLUDED.alias, "
            " note=EXCLUDED.note, public=EXCLUDED.public, "
            " owner_user_id=COALESCE(slides.owner_user_id, EXCLUDED.owner_user_id)",
            (sid, name, alias or "", alias, note, owner, public),
        )
        slide_map[name] = sid
    return slide_map


def _import_shares(cur, shares):
    import psycopg
    n = 0
    for tok, sh in (shares.get("shares") or {}).items():
        if not isinstance(sh, dict):
            continue
        cur.execute(
            "INSERT INTO shares "
            "(token, slides, permissions, roi_sizes, expires_at, revoked, "
            " created_at, creator_user_id) "
            "VALUES (%s,%s,%s,%s, to_timestamp(%s), %s, to_timestamp(%s), %s) "
            "ON CONFLICT (token) DO UPDATE SET "
            " slides=EXCLUDED.slides, permissions=EXCLUDED.permissions, "
            " roi_sizes=EXCLUDED.roi_sizes, expires_at=EXCLUDED.expires_at, "
            " revoked=EXCLUDED.revoked, created_at=EXCLUDED.created_at, "
            " creator_user_id=EXCLUDED.creator_user_id",
            (tok,
             psycopg.types.json.Jsonb(sh.get("slides") or []),
             psycopg.types.json.Jsonb(sh.get("permissions") or []),
             psycopg.types.json.Jsonb(sh.get("roi_sizes") or []),
             sh.get("expires_at"), bool(sh.get("revoked", False)),
             sh.get("created_at") or time.time(),
             sh.get("creator_user_id")),
        )
        n += 1
    return n


def _import_grants(cur, shares):
    import psycopg
    n = 0
    for g in shares.get("grants") or []:
        if not isinstance(g, dict):
            continue
        gid = g.get("grant_id")
        active = g.get("revoked_at") is None
        cur.execute(
            "INSERT INTO grants "
            "(id, token, user_id, permissions, claimed_at, active) "
            "VALUES (%s,%s,%s,%s, to_timestamp(%s), %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            " token=EXCLUDED.token, user_id=EXCLUDED.user_id, "
            " permissions=EXCLUDED.permissions, claimed_at=EXCLUDED.claimed_at, "
            " active=EXCLUDED.active",
            (gid, g.get("share_token"), g.get("user_id"),
             psycopg.types.json.Jsonb(g.get("permissions") or []),
             g.get("claimed_at") or time.time(), active),
        )
        n += 1
    return n


def _import_projects(cur, shares):
    import psycopg
    n = 0
    pids = []
    for pid, proj in (shares.get("projects") or {}).items():
        if not isinstance(proj, dict):
            continue
        cur.execute(
            "INSERT INTO projects "
            "(project_id, name, note, owner_user_id, created_at) "
            "VALUES (%s,%s,%s,%s, to_timestamp(%s)) "
            "ON CONFLICT (project_id) DO UPDATE SET "
            " name=EXCLUDED.name, note=EXCLUDED.note, "
            " owner_user_id=EXCLUDED.owner_user_id, created_at=EXCLUDED.created_at",
            (pid, str(proj.get("name") or ""), str(proj.get("note") or ""),
             proj.get("owner_user_id"), proj.get("created_at") or time.time()),
        )
        pids.append((pid, proj.get("slides") or []))
        n += 1
    # project_slides：先清本次涉及的 project 再重建（幂等）
    if pids:
        cur.execute("DELETE FROM project_slides WHERE project_id = ANY(%s)",
                    ([p[0] for p in pids],))
        for pid, slides in pids:
            for i, s in enumerate(slides):
                if isinstance(s, str):
                    cur.execute(
                        "INSERT INTO project_slides (project_id, slide, position) "
                        "VALUES (%s,%s,%s)", (pid, s, i))
    return n


def _import_change_log_and_rois(cur, shares):
    """重建 change_log（每条 roi 一行，op 从快照推），再把 seq 回填进 roi.data。

    幂等：先删本次涉及切片的 change_log，再按 roi 顺序重插。seq 为 bigserial 全局
    单调；roi.data.change_seq 设为对应行 seq，使 list_changes/current_change_seq 自洽。
    json 的 per-slide 计数器数值不 1:1 保留（share_store_pg 已声明的允许实现差）。
    """
    import psycopg
    rois = [r for r in (shares.get("rois") or []) if isinstance(r, dict)]
    slides = sorted({r.get("slide") for r in rois if r.get("slide")})
    if slides:
        cur.execute("DELETE FROM change_log WHERE slide = ANY(%s)", (slides,))
    # 按 roi 顺序写 change_log，记 aid → seq
    aid_seq = {}
    for r in rois:
        aid = r.get("annotation_id")
        if not aid:
            continue
        op = "delete" if r.get("deleted") else "add"
        cur.execute(
            "INSERT INTO change_log (slide, token, annotation_id, op) "
            "VALUES (%s,%s,%s,%s) RETURNING seq",
            (r.get("slide"), r.get("token"), aid, op),
        )
        aid_seq[aid] = cur.fetchone()["seq"]
    # 按 roi 顺序写 rois（insert_seq 与数组序一致）
    for r in rois:
        aid = r.get("annotation_id")
        if not aid:
            continue
        data = dict(r)
        if aid in aid_seq:
            data["change_seq"] = aid_seq[aid]
        rid = _det_roi_id(aid)
        now = data.get("updated_at") or data.get("ts") or time.time()
        geom = {k: data[k] for k in _GEOM_KEYS if k in data}
        cur.execute(
            "INSERT INTO rois "
            "(id, token, slide, annotation_id, label, type, geom, size_mm, "
            " shared, note, deleted, owner_user_id, created_at, updated_at, data) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, to_timestamp(%s), "
            "to_timestamp(%s), %s) "
            "ON CONFLICT (annotation_id) DO UPDATE SET "
            " geom=EXCLUDED.geom, size_mm=EXCLUDED.size_mm, shared=EXCLUDED.shared, "
            " note=EXCLUDED.note, deleted=EXCLUDED.deleted, "
            " owner_user_id=EXCLUDED.owner_user_id, updated_at=EXCLUDED.updated_at, "
            " data=EXCLUDED.data",
            (rid, data.get("token"), data.get("slide"), aid,
             data.get("label", ""), data.get("type", "rect"),
             psycopg.types.json.Jsonb(geom),
             float(data.get("size_mm", 0.0) or 0.0),
             bool(data.get("shared", False)), data.get("note", ""),
             bool(data.get("deleted", False)), data.get("owner_user_id"),
             now, now, psycopg.types.json.Jsonb(data)),
        )
    return len(rois)


def _import_slide_assets(cur, slide_map, upload_dir, skipped):
    """为每个有 slide_id 的切片算 legacy_revision + content_sha256，重建 slide_assets。"""
    sids = [s for s in slide_map.values() if s]
    if sids:
        cur.execute("DELETE FROM slide_assets WHERE slide_id = ANY(%s)", (sids,))
    n = 0
    for name, sid in slide_map.items():
        if not sid or name in skipped:
            continue
        legacy_rev, sha = _slide_file_stats(upload_dir, name)
        import secrets
        asset_id = "ast_" + secrets.token_urlsafe(9)
        cur.execute(
            "INSERT INTO slide_assets (asset_id, slide_id, content_sha256, "
            "legacy_revision) VALUES (%s,%s,%s,%s)",
            (asset_id, sid, sha, legacy_rev),
        )
        n += 1
    return n


def _do_apply(conn, shares, users, upload_dir, skipped):
    """在已开启的事务内执行全部导入。返回各表写入计数 dict。"""
    with conn.cursor() as cur:
        n_users = _import_users(cur, users)
        slide_map = _import_slides(cur, shares, upload_dir, skipped)
        n_shares = _import_shares(cur, shares)
        n_grants = _import_grants(cur, shares)
        n_projects = _import_projects(cur, shares)
        n_rois = _import_change_log_and_rois(cur, shares)
        n_assets = _import_slide_assets(cur, slide_map, upload_dir, skipped)
    return {"users": n_users, "shares": n_shares, "grants": n_grants,
            "projects": n_projects, "rois": n_rois, "slide_assets": n_assets,
            "slides": len(slide_map)}


# --------------------------------------------------------------------------- #
# 命令：dry-run / apply / verify / rollback
# --------------------------------------------------------------------------- #
def _print_counts(counts, stream=None):
    # stream 运行期解析（不用默认参数绑定 sys.stdout，否则 capsys 等替换捕获不到）。
    if stream is None:
        stream = sys.stdout
    stream.write("实体计数：\n")
    for k in ("users", "shares", "grants", "rois", "slide_meta", "projects",
              "change_log"):
        stream.write("  %-12s %d\n" % (k, counts.get(k, 0)))


def cmd_dry_run(args):
    share_data_dir = Path(args.share_data_dir or _default_share_data_dir())
    upload_dir = args.upload_dir or _default_upload_dir()
    shares = _load_shares(share_data_dir)
    users = _load_users(share_data_dir)
    counts = _counts(shares, users)
    problems = _scan_problems(shares, users, upload_dir)
    _print_counts(counts)
    sys.stdout.write("\n数据源：%s / %s\n" % (_shares_file(share_data_dir),
                                            _users_file(share_data_dir)))
    sys.stdout.write("切片目录：%s\n" % upload_dir)
    if problems:
        sys.stdout.write("\n潜在问题（%d 条，不写 pg）：\n" % len(problems))
        for p in problems:
            sys.stdout.write("  - %s\n" % p)
    else:
        sys.stdout.write("\n潜在问题：无\n")
    sys.stdout.write("\n（dry-run 不触碰 PostgreSQL；同名归属冲突在 apply 期检测）\n")
    return 0


def _backup_json(share_data_dir, backup_dir):
    """把源 json 原样复制进备份目录。返回 (shares_bak, users_bak)。"""
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    sf, uf = _shares_file(share_data_dir), _users_file(share_data_dir)
    sf_bak = backup_dir / "shares.json"
    uf_bak = backup_dir / "users.json"
    if sf.is_file():
        shutil.copy2(sf, sf_bak)
    if uf.is_file():
        shutil.copy2(uf, uf_bak)
    return sf_bak, uf_bak


def cmd_apply(args):
    share_data_dir = Path(args.share_data_dir or _default_share_data_dir())
    upload_dir = args.upload_dir or _default_upload_dir()
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(args.backup_dir or
                      (share_data_dir / ("migration-backup-%s" % ts)))

    shares = _load_shares(share_data_dir)
    users = _load_users(share_data_dir)

    # 备份源 json（先复制再导入）
    sf_bak, uf_bak = _backup_json(share_data_dir, backup_dir)
    sys.stdout.write("已备份源 json → %s\n" % backup_dir)

    conn = _connect()
    problems_before = _scan_problems(shares, users, upload_dir)
    try:
        # 先建计划（检测同名归属冲突，需在事务内读现有 slides 行）
        with pg_store.transaction(conn):
            plan = _build_plan(shares, users, upload_dir, conn)
        skipped = set(plan.skipped_slides)
        # 单事务导入
        with pg_store.transaction(conn) as txn:
            written = _do_apply(conn, shares, users, upload_dir, skipped)
        # 身份映射表落盘
        mapping = {
            "created_at": time.time(),
            "share_data_dir": str(share_data_dir),
            "legacy_filename_to_slide_id": plan.slide_map,
            "login_id_to_user_id": plan.user_map,
            "tokens": plan.tokens,
            "annotation_ids": plan.annotation_ids,
            "skipped_slides": sorted(skipped),
        }
        (backup_dir / "mapping.json").write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        conn.close()
        sys.stderr.write("\n导入失败，事务已整体回滚：%s\n" % exc)
        sys.stderr.write("源 json 备份在：%s（json 未被改动，可重试）\n" % backup_dir)
        return 1
    conn.close()

    sys.stdout.write("\n导入完成（写入计数）：\n")
    for k, v in written.items():
        sys.stdout.write("  %-14s %d\n" % (k, v))
    if plan.pg_conflicts:
        sys.stdout.write("\n同名归属冲突（%d 条，已跳过该切片的 identity/asset）：\n"
                         % len(plan.pg_conflicts))
        for c in plan.pg_conflicts:
            sys.stdout.write("  - %s\n" % c)
    if problems_before:
        sys.stdout.write("\n潜在问题（%d 条）：\n" % len(problems_before))
        for p in problems_before:
            sys.stdout.write("  - %s\n" % p)
    sys.stdout.write("\n身份映射表：%s\n" % (backup_dir / "mapping.json"))
    sys.stdout.write("下一步：python3 scripts/migrate_json_to_pg.py verify\n")
    return 1 if plan.pg_conflicts else 0


def _geom_tuple(roi):
    return tuple((k, roi.get(k)) for k in _GEOM_KEYS if k in roi)


def _approx_eq(a, b, tol=1.0):
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return a == b


def cmd_verify(args):
    share_data_dir = Path(args.share_data_dir or _default_share_data_dir())
    shares = _load_shares(share_data_dir)
    users = _load_users(share_data_dir)
    diffs = []

    conn = _connect()
    try:
        with conn.cursor() as cur:
            # ---- users（旧格式 json 的 email 键读为 login_id 后比对）----
            cur.execute("SELECT user_id, login_id, role, disabled, "
                        "password_hash FROM users")
            pg_users = {r["login_id"]: r for r in cur.fetchall()}
            json_users = {}
            for uid, u in (users.get("users") or {}).items():
                if isinstance(u, dict):
                    json_users[_login_id_of(u)] = u
            _diff_count(diffs, "users", len(json_users), len(pg_users))
            for login_id, u in json_users.items():
                pg = pg_users.get(login_id)
                if pg is None:
                    diffs.append("users: json 有 %s 但 pg 无" % login_id)
                    continue
                if str(u.get("role") or "user") != str(pg["role"] or "user"):
                    diffs.append("users[%s] role: json=%s pg=%s"
                                 % (login_id, u.get("role"), pg["role"]))
                if bool(u.get("disabled", False)) != bool(pg["disabled"]):
                    diffs.append("users[%s] disabled: json=%s pg=%s"
                                 % (login_id, u.get("disabled"), pg["disabled"]))
                if str(u.get("password_hash") or "") != str(pg["password_hash"] or ""):
                    diffs.append("users[%s] password_hash 不一致" % login_id)

            # ---- shares ----
            cur.execute("SELECT token, slides, permissions, "
                        "extract(epoch from expires_at)::float8 AS expires_at, "
                        "revoked FROM shares")
            pg_shares = {r["token"]: r for r in cur.fetchall()}
            json_shares = shares.get("shares") or {}
            _diff_count(diffs, "shares", len(json_shares), len(pg_shares))
            for tok, sh in json_shares.items():
                pg = pg_shares.get(tok)
                if pg is None:
                    diffs.append("shares: json 有 %s 但 pg 无" % tok)
                    continue
                if set(sh.get("slides") or []) != set(pg["slides"] or []):
                    diffs.append("shares[%s] slides 集合不一致" % tok)
                if sorted(sh.get("permissions") or []) != sorted(pg["permissions"] or []):
                    diffs.append("shares[%s] permissions 不一致" % tok)
                if not _approx_eq(sh.get("expires_at"), pg["expires_at"]):
                    diffs.append("shares[%s] expires_at 不一致" % tok)

            # ---- grants ----
            cur.execute("SELECT id, token, user_id, permissions, active FROM grants")
            pg_grants = {r["id"]: r for r in cur.fetchall()}
            json_grants = shares.get("grants") or []
            json_grants_map = {g.get("grant_id"): g for g in json_grants
                               if isinstance(g, dict) and g.get("grant_id")}
            _diff_count(diffs, "grants", len(json_grants_map), len(pg_grants))
            for gid, g in json_grants_map.items():
                pg = pg_grants.get(gid)
                if pg is None:
                    diffs.append("grants: json 有 %s 但 pg 无" % gid)
                    continue
                if bool(g.get("revoked_at") is None) != bool(pg["active"]):
                    diffs.append("grants[%s] active 不一致" % gid)

            # ---- rois ----
            cur.execute("SELECT annotation_id, data FROM rois")
            pg_rois = {r["annotation_id"]: r["data"] for r in cur.fetchall()
                       if r["annotation_id"]}
            json_rois = {r.get("annotation_id"): r for r in (shares.get("rois") or [])
                         if isinstance(r, dict) and r.get("annotation_id")}
            _diff_count(diffs, "rois", len(json_rois), len(pg_rois))
            for aid, r in json_rois.items():
                pg = pg_rois.get(aid)
                if pg is None:
                    diffs.append("rois: json 有 %s 但 pg 无" % aid)
                    continue
                if _geom_tuple(r) != _geom_tuple(pg):
                    diffs.append("rois[%s] geom 不一致" % aid)
                if str(r.get("label") or "") != str(pg.get("label") or ""):
                    diffs.append("rois[%s] label 不一致" % aid)
                if bool(r.get("shared", False)) != bool(pg.get("shared", False)):
                    diffs.append("rois[%s] shared 不一致" % aid)
                if bool(r.get("deleted", False)) != bool(pg.get("deleted", False)):
                    diffs.append("rois[%s] deleted 不一致" % aid)

            # ---- slide_meta ----
            cur.execute("SELECT legacy_filename, alias, note, owner_user_id, "
                        "public FROM slides WHERE legacy_filename IS NOT NULL")
            pg_slides = {r["legacy_filename"]: r for r in cur.fetchall()}
            json_meta = shares.get("slide_meta") or {}
            _diff_count(diffs, "slide_meta", len(json_meta),
                        sum(1 for n in json_meta if n in pg_slides))
            for name, m in json_meta.items():
                pg = pg_slides.get(name)
                if pg is None:
                    diffs.append("slide_meta: json 有 %s 但 pg 无" % name)
                    continue
                if str(m.get("alias") or "") != str(pg["alias"] or ""):
                    diffs.append("slide_meta[%s] alias 不一致" % name)
                if str(m.get("note") or "") != str(pg["note"] or ""):
                    diffs.append("slide_meta[%s] note 不一致" % name)
                if bool(m.get("public", False)) != bool(pg["public"]):
                    diffs.append("slide_meta[%s] public 不一致" % name)
                # owner：json 无则不比（懒迁移可空）；json 有才比
                if m.get("owner_user_id") is not None and \
                        str(m.get("owner_user_id")) != str(pg["owner_user_id"] or ""):
                    diffs.append("slide_meta[%s] owner 不一致" % name)

            # ---- projects ----
            cur.execute("SELECT project_id FROM projects")
            pg_pids = {r["project_id"] for r in cur.fetchall()}
            json_projs = shares.get("projects") or {}
            _diff_count(diffs, "projects", len(json_projs), len(pg_pids))
            for pid in json_projs:
                if pid not in pg_pids:
                    diffs.append("projects: json 有 %s 但 pg 无" % pid)

            # ---- change_log（计数：应为 roi 条数）----
            cur.execute("SELECT count(*) AS n FROM change_log")
            pg_cl = int(cur.fetchone()["n"])
            _diff_count(diffs, "change_log", len(shares.get("rois") or []), pg_cl)
    finally:
        conn.close()

    if not diffs:
        sys.stdout.write("OK：json 与 pg 双读核对 0 差异\n")
        return 0
    sys.stderr.write("发现 %d 处差异（列前 20 条）：\n" % len(diffs))
    for d in diffs[:20]:
        sys.stderr.write("  - %s\n" % d)
    return 2


def _diff_count(diffs, name, json_n, pg_n):
    if json_n != pg_n:
        diffs.append("%s: 计数不一致 json=%d pg=%d" % (name, json_n, pg_n))


def cmd_rollback(args):
    share_data_dir = Path(args.share_data_dir or _default_share_data_dir())
    backup_dir = Path(args.backup_dir) if args.backup_dir else None
    if backup_dir is None:
        sys.stderr.write("rollback 需要 --backup-dir 指定备份目录\n")
        return 1
    sf_bak = backup_dir / "shares.json"
    uf_bak = backup_dir / "users.json"
    mapping = backup_dir / "mapping.json"
    missing = [str(p) for p in (sf_bak, uf_bak, mapping) if not p.exists()]
    if missing:
        sys.stderr.write("备份目录不完整，缺：%s\n" % ", ".join(missing))
        return 1
    if not args.yes:
        sys.stdout.write("将把备份 json 拷回 %s（覆盖当前）。继续？[y/N] "
                         % share_data_dir)
        sys.stdout.flush()
        ans = sys.stdin.readline().strip().lower()
        if ans not in ("y", "yes"):
            sys.stdout.write("已取消。\n")
            return 1
    if sf_bak.exists():
        shutil.copy2(sf_bak, _shares_file(share_data_dir))
    if uf_bak.exists():
        shutil.copy2(uf_bak, _users_file(share_data_dir))
    sys.stdout.write("已把备份 json 拷回 %s\n" % share_data_dir)
    sys.stdout.write("\n回滚指引：\n")
    sys.stdout.write("  1. STORAGE_BACKEND=json（或取消该 env）\n")
    sys.stdout.write("  2. 重启服务（gunicorn / docker）\n")
    sys.stdout.write("  3. PG 中的导入数据仍保留；如需清空可手动 TRUNCATE 业务表\n")
    return 0


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #
def _build_parser():
    p = argparse.ArgumentParser(
        prog="migrate_json_to_pg",
        description="JSON → PostgreSQL 迁移工具（Stage 3b-3）。停写窗口执行。",
    )
    share_arg = dict(action="store", default=None,
                     help="SHARE_DATA_DIR（默认读 env 或 ~/svs-viewer/share-data）")
    upload_arg = dict(action="store", default=None,
                      help="UPLOAD_DIR（默认读 env 或 ~/svs-viewer/uploads）")
    sub = p.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("dry-run", help="只读 json，输出计数+问题清单，不写 pg")
    pd.add_argument("--share-data-dir", **share_arg)
    pd.add_argument("--upload-dir", **upload_arg)
    pd.set_defaults(func=cmd_dry_run)

    pa = sub.add_parser("apply", help="执行导入（单事务，幂等）")
    pa.add_argument("--share-data-dir", **share_arg)
    pa.add_argument("--upload-dir", **upload_arg)
    pa.add_argument("--backup-dir", action="store", default=None,
                    help="回滚备份目录（默认 SHARE_DATA_DIR/migration-backup-<ts>/）")
    pa.set_defaults(func=cmd_apply)

    pv = sub.add_parser("verify", help="双读核对 json vs pg")
    pv.add_argument("--share-data-dir", **share_arg)
    pv.set_defaults(func=cmd_verify)

    pr = sub.add_parser("rollback", help="从备份目录还原 json")
    pr.add_argument("--share-data-dir", **share_arg)
    pr.add_argument("--backup-dir", action="store", required=False, default=None,
                    help="apply 时生成的备份目录")
    pr.add_argument("--yes", action="store_true", help="免交互确认")
    pr.set_defaults(func=cmd_rollback)
    return p


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as exc:
        sys.stderr.write("错误：%s\n" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
