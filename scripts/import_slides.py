# -*- coding: utf-8 -*-
"""管理员离线导入切片（Upload V2 方案 U4）。

把经 rsync/SFTP 落到暂存目录的单文件 WSI 校验后原子提升进 UPLOAD_DIR，
并写入 slide_meta 归属。不走 HTTP，不经过 CSRF / 分片协议。

ZIP / MRXS 伴侣包不在本通道（请走 ``POST /api/upload``）。

容器内用法::

    python /app/scripts/import_slides.py --src /data/import-staging
    python /app/scripts/import_slides.py --src /data/import-staging \\
        --owner-login-id user@example.com --move

主机侧（uploads 已 bind-mount）也可直接跑，需 ``UPLOAD_DIR`` / ``DATABASE_URL``
与容器一致。``--dry-run`` 只报告不落盘。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 仓库根进 path，便于 ``python scripts/import_slides.py``
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _err(msg):
    sys.stderr.write("import_slides: 错误：%s\n" % msg)


def _info(msg):
    sys.stdout.write("import_slides: %s\n" % msg)


def iter_candidates(src: Path):
    """列出暂存区里的普通文件（跳过隐藏、目录）。"""
    if src.is_file():
        yield src
        return
    if not src.is_dir():
        raise FileNotFoundError("暂存路径不存在：%s" % src)
    for child in sorted(src.iterdir()):
        if child.name.startswith("."):
            continue
        if child.is_file():
            yield child


def resolve_owner(owner_user_id=None, owner_login_id=None):
    """解析归属。都空 → 部署 owner（空 user_id + role=owner，与免认证归一一致）。"""
    import user_store

    if owner_user_id and owner_login_id:
        raise ValueError("不要同时传 --owner-user-id 与 --owner-login-id")
    if owner_user_id:
        user = user_store.get_user(owner_user_id)
        if not user:
            raise ValueError("用户不存在：%s" % owner_user_id)
        if user.get("disabled"):
            raise ValueError("用户已禁用：%s" % owner_user_id)
        return user["user_id"], user.get("role") or user_store.ROLE_USER
    if owner_login_id:
        user = user_store.get_user_by_login_id(owner_login_id)
        if not user:
            raise ValueError("登录账号不存在：%s" % owner_login_id)
        if user.get("disabled"):
            raise ValueError("用户已禁用：%s" % owner_login_id)
        return user["user_id"], user.get("role") or user_store.ROLE_USER
    return "", "owner"


def import_one(src: Path, upload_dir: Path, owner_user_id, requester_role,
               *, dry_run=False, move=False):
    """校验并提升单个文件。成功返回 dest Path；跳过/失败 raise ValueError。"""
    import app as app_mod
    import share_store
    import upload_guard

    name = src.name
    safe = app_mod._sanitize_name(name)
    if not safe:
        raise ValueError("非法文件名：%r" % name)
    ext = Path(safe).suffix.lower().lstrip(".")
    if ext in getattr(app_mod, "ARCHIVE_EXTS", {"zip"}) or ext == "mrxs":
        raise ValueError("ZIP/MRXS 请走 POST /api/upload，本通道只收单文件 WSI：%s" % name)
    if ext not in app_mod.SUPPORTED_EXTS:
        raise ValueError("不支持的扩展名 .%s：%s" % (ext, name))

    dest = upload_dir / safe
    if dest.exists():
        raise ValueError("名称不可用（目标已存在）：%s" % safe)

    if dry_run:
        return dest

    upload_guard.check_disk_watermark(upload_dir)

    # 先在原地校验（不复制）；通过后再 link。跨设备则 copy+validate 后再 replace。
    if not app_mod._validate_slide_file(src):
        raise ValueError("无效的切片文件：%s" % name)

    try:
        os.link(src, dest)
    except FileExistsError:
        raise ValueError("名称不可用（目标已存在）：%s" % safe)
    except OSError:
        # 跨设备：copy 到临时再 replace
        tmp = upload_dir / (".importing-" + safe)
        try:
            import shutil
            shutil.copy2(src, tmp)
            if not app_mod._validate_slide_file(tmp):
                tmp.unlink(missing_ok=True)
                raise ValueError("无效的切片文件：%s" % name)
            os.replace(tmp, dest)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    try:
        share_store.set_slide_meta(
            safe,
            owner_user_id=owner_user_id or None,
            requester_role=requester_role,
        )
    except Exception:
        dest.unlink(missing_ok=True)
        raise

    if move and src.resolve() != dest.resolve():
        try:
            src.unlink()
        except OSError as e:
            _info("已导入 %s，但删除源文件失败：%s" % (safe, e))
    return dest


def run(src, upload_dir=None, owner_user_id=None, owner_login_id=None,
        dry_run=False, move=False):
    src = Path(src)
    upload_dir = Path(upload_dir or os.environ.get("UPLOAD_DIR") or "/data/uploads")
    owner_uid, role = resolve_owner(owner_user_id, owner_login_id)
    results = {"ok": [], "failed": []}
    for path in iter_candidates(src):
        try:
            dest = import_one(path, upload_dir, owner_uid, role,
                              dry_run=dry_run, move=move)
            results["ok"].append(str(dest.name))
            _info("%s%s → %s" % ("dry-run " if dry_run else "", path.name, dest.name))
        except ValueError as e:
            results["failed"].append({"file": path.name, "error": str(e)})
            _err(str(e))
    return results


def main(argv=None):
    p = argparse.ArgumentParser(description="管理员 rsync/SFTP 切片安全导入")
    p.add_argument("--src", required=True, help="暂存文件或目录")
    p.add_argument("--upload-dir", default=None, help="默认 UPLOAD_DIR env")
    p.add_argument("--owner-user-id", default=None)
    p.add_argument("--owner-login-id", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--move", action="store_true",
                   help="成功后删除暂存源文件（同文件系统下 link+unlink）")
    args = p.parse_args(argv)
    try:
        results = run(
            args.src,
            upload_dir=args.upload_dir,
            owner_user_id=args.owner_user_id,
            owner_login_id=args.owner_login_id,
            dry_run=args.dry_run,
            move=args.move,
        )
    except (FileNotFoundError, ValueError) as e:
        _err(str(e))
        return 1
    if results["failed"]:
        return 1
    if not results["ok"]:
        _err("暂存区没有可导入的文件")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
