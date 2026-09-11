# -*- coding: utf-8 -*-
"""KFB → 经典多 IFD JPEG tiled BigTIFF 混合重封装 converter（Phase A spike）。

策略（docs/kfb-ingestion-converter-review.md §0.5/§6.3，不可降级）：
  1. 完整 256×256 JPEG tile 的压缩字节**原样**写入 TIFF 压缩段（禁止二次
     有损）；
  2. 尺寸不足的边缘 tile：解码 → 白底 256×256 左上贴图 → 尽量复用源
     量化表/采样重编码（拿不到量化表时 quality=95 兜底并记 warning
     ``edge_reencode_fallback_q95``）；
  3. 输出经典多 IFD 金字塔 BigTIFF（非 SubIFD OME），每层一个 IFD，
     TileWidth/TileLength=256，Photometric=YCbCr + YCbCrSubSampling 与源
     JPEG 采样一致（实测：与 JPEG 不一致的颜色解释会得到错误颜色）；
  4. MPP 写 TIFF resolution tags（ResolutionUnit=厘米），objective 进
     ImageDescription JSON 与 manifest；
  5. 先写 ``OUT.tif.part``，结构校验通过后 ``os.replace`` 原子转正；
  6. 全程按 tile 流式搬运，不把整幅金字塔解码进大 RGB 数组。

层级写入停止条件：包含首个 1×1 网格层（其后的冗余单 tile 层不写）。
资源护栏：wall timeout / 输出字节上限 / 目标盘剩余空间。
"""

from __future__ import annotations

import io
import json
import math
import os
import shutil
import struct
import time
from pathlib import Path

from .errors import KfbError
from .parser import TILE_H, TILE_W, parse_kfb, scan_jpeg

#: Pillow 采样因子 (h1,v1,h2,v2,h3,v3) → Pillow save(subsampling=int)
_PILLOW_SUBSAMPLING = {
    (1, 1, 1, 1, 1, 1): 0,
    (2, 1, 1, 1, 1, 1): 1,
    (2, 2, 1, 1, 1, 1): 2,
}
#: 同上 → TIFF YCbCrSubSampling 标签值 (h1, v1)
_TIFF_SUBSAMPLING = {
    (1, 1, 1, 1, 1, 1): (1, 1),
    (2, 1, 1, 1, 1, 1): (2, 1),
    (2, 2, 1, 1, 1, 1): (2, 2),
}

DEFAULT_TIMEOUT_SECONDS = 600.0
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024 ** 3
DEFAULT_MIN_FREE_BYTES = 256 * 1024 ** 2

WARN_EDGE_REENCODE_FALLBACK_Q95 = "edge_reencode_fallback_q95"


# --------------------------------------------------------------------------- #
# BigTIFF（little-endian, version 43）经典多 IFD writer
# --------------------------------------------------------------------------- #
_TIFF_BYTE, _TIFF_ASCII, _TIFF_SHORT, _TIFF_LONG = 1, 2, 3, 4
_TIFF_RATIONAL, _TIFF_LONG8 = 5, 16


class _BigTiffPyramidWriter:
    """手写 BigTIFF：JPEG 字节原样填 TileOffsets 指向的压缩段。

    布局：16B 头 → 全部 tile payload（顺序写，记录 offset）→ 逐层 IFD +
    外部数组（BigTIFF IFD：u64 entry 数、每 entry 20B、u64 next 指针）。
    """

    def __init__(self, fh):
        self._fh = fh
        self._fh.write(b"II" + struct.pack("<HHHQ", 43, 8, 0, 0))
        self._ifds = []  # 每层 [(tag, type, count, payload_bytes)]

    def write_tile(self, data) -> tuple:
        offset = self._fh.tell()
        self._fh.write(data)
        return offset, len(data)

    def add_level(self, width, height, tile_offsets, tile_byte_counts, *,
                  sampling, mpp_x, mpp_y, description, reduced):
        """登记一层 IFD。tile 顺序必须已是 (row, x) 网格序。"""
        entries = [
            (254, _TIFF_LONG, 1, struct.pack("<I", 1 if reduced else 0)),
            (256, _TIFF_LONG, 1, struct.pack("<I", width)),
            (257, _TIFF_LONG, 1, struct.pack("<I", height)),
            (258, _TIFF_SHORT, 3, struct.pack("<HHH", 8, 8, 8)),
            (259, _TIFF_SHORT, 1, struct.pack("<H", 7)),  # JPEG
            (262, _TIFF_SHORT, 1, struct.pack("<H", 6)),  # YCbCr
            (270, _TIFF_ASCII, len(description), description),
            (277, _TIFF_SHORT, 1, struct.pack("<H", 3)),
            (282, _TIFF_RATIONAL, 1, _px_per_cm_rational(mpp_x)),
            (283, _TIFF_RATIONAL, 1, _px_per_cm_rational(mpp_y)),
            (284, _TIFF_SHORT, 1, struct.pack("<H", 1)),  # chunky
            (296, _TIFF_SHORT, 1, struct.pack("<H", 3)),  # 厘米
            (322, _TIFF_SHORT, 1, struct.pack("<H", TILE_W)),
            (323, _TIFF_SHORT, 1, struct.pack("<H", TILE_H)),
            (324, _TIFF_LONG8, len(tile_offsets),
             b"".join(struct.pack("<Q", o) for o in tile_offsets)),
            (325, _TIFF_LONG8, len(tile_byte_counts),
             b"".join(struct.pack("<Q", c) for c in tile_byte_counts)),
            (530, _TIFF_SHORT, 2, struct.pack("<HH", *sampling)),
        ]
        entries.sort(key=lambda e: e[0])
        self._ifds.append(entries)

    def finish(self):
        if not self._ifds:
            raise KfbError("conversion_validation_failed", "无 IFD 可写")
        fh = self._fh
        positions = []
        pos = fh.tell()
        for entries in self._ifds:
            size = 8 + 20 * len(entries) + 8  # u64 计数 + entries + next 指针
            ext = sum(len(p) + (len(p) % 2) for _t, _y, _c, p in entries
                      if len(p) > 8)
            positions.append((pos, size, ext))
            pos += size + ext
        for i, entries in enumerate(self._ifds):
            ifd_pos, size, _ext = positions[i]
            if fh.tell() != ifd_pos:  # 布局自检（防御性）
                raise KfbError("conversion_validation_failed", "IFD 布局错位")
            fh.write(struct.pack("<Q", len(entries)))
            ext_cursor = ifd_pos + size
            for (tag, typ, count, payload) in entries:
                fh.write(struct.pack("<HHQ", tag, typ, count))
                if len(payload) <= 8:
                    fh.write(payload + b"\x00" * (8 - len(payload)))
                else:
                    fh.write(struct.pack("<Q", ext_cursor))
                    ext_cursor += len(payload) + (len(payload) % 2)
            fh.write(struct.pack("<Q", positions[i + 1][0]
                                 if i + 1 < len(positions) else 0))
            for (_tag, _typ, _count, payload) in entries:
                if len(payload) > 8:
                    fh.write(payload)
                    if len(payload) % 2:
                        fh.write(b"\x00")
        end = fh.tell()
        fh.seek(8)
        fh.write(struct.pack("<Q", positions[0][0]))
        fh.seek(end)


def _px_per_cm_rational(mpp) -> bytes:
    """MPP(µm/px) → TIFF RATIONAL（像素/厘米，num/den 均限 u32）。

    openslide 由 厘米 + RATIONAL 恢复 mpp（µm/px = 10^4 / px_per_cm）。
    """
    if not math.isfinite(mpp) or mpp <= 0:
        raise KfbError("metadata_missing_required", "mpp=%r 非法" % (mpp,))
    for den in (10000, 100, 1):
        num = int(round(10000.0 / mpp * den))
        if 0 < num <= 0xFFFFFFFF:
            return struct.pack("<II", num, den)
    raise KfbError("metadata_missing_required", "mpp=%r 超出 RATIONAL 范围" % (mpp,))


# --------------------------------------------------------------------------- #
# 边缘 tile 重编码
# --------------------------------------------------------------------------- #
def _extract_qtables(im):
    """取源 JPEG 量化表（Pillow qtables）；不可用/形态非法 → None。

    Pillow 版本差异：``im.quantization`` 可能是 ``{table_id: [64 ints]}``
    dict 或 list。统一规整成按 table_id 升序的 list-of-lists（Pillow
    ``save(qtables=...)`` 接受该形态）。
    """
    try:
        q = im.quantization
    except Exception:  # noqa: BLE001
        return None
    if isinstance(q, dict):
        try:
            q = [q[k] for k in sorted(q)]
        except KeyError:  # noqa: PERF203
            return None
    if not isinstance(q, (list, tuple)):
        return None
    if not (2 <= len(q) <= 4):
        return None
    for table in q:
        if not isinstance(table, (list, tuple)) or len(table) != 64:
            return None
        if any((not isinstance(v, int)) or v < 1 or v > 255 for v in table):
            return None
    return [list(t) for t in q]


def _reencode_edge_tile(payload, jpeg_w, jpeg_h, *, pil_subsampling):
    """解码边缘 tile → 白底 256×256 左上贴图 → 复用源量化表重编码。

    返回 ``(jpeg_bytes, reused_qtables: bool)``。源量化表拿不到时用
    Pillow quality=95 兜底（调用方记 warning）。
    """
    from PIL import Image

    try:
        im = Image.open(io.BytesIO(payload))
        im.load()
    except Exception as e:  # noqa: BLE001
        raise KfbError("jpeg_decode_failed", "边缘 tile 解码失败：%s" % e)
    if im.size != (jpeg_w, jpeg_h):
        raise KfbError(
            "jpeg_decode_failed",
            "tile 解码尺寸 %r 与索引 %d×%d 不符" % (im.size, jpeg_w, jpeg_h))
    canvas = Image.new("RGB", (TILE_W, TILE_H), (255, 255, 255))
    canvas.paste(im.convert("RGB"), (0, 0))
    qtables = _extract_qtables(im)
    buf = io.BytesIO()
    if qtables is not None:
        canvas.save(buf, format="JPEG", qtables=qtables,
                    subsampling=pil_subsampling)
    else:
        canvas.save(buf, format="JPEG", quality=95,
                    subsampling=pil_subsampling)
    return buf.getvalue(), qtables is not None


# --------------------------------------------------------------------------- #
# 层级选择与网格校验
# --------------------------------------------------------------------------- #
def _select_levels(doc):
    """选要写入的层：升序遍历，包含首个 1×1 网格层（含）为止。

    选中的层必须在索引里有 tile；中间缺整层（网格 >1×1 却无 tile）→
    conversion_validation_failed（fail clearly，不写残缺金字塔）。
    """
    selected = []
    stopped = False
    for lv in doc.levels:
        tiles = doc.tiles_by_level.get(lv.level, [])
        single = lv.tiles_across == 1 and lv.tiles_down == 1
        if stopped:
            break
        if not tiles:
            if single:
                break  # 文件本身没有该冗余层：正常停止
            raise KfbError(
                "conversion_validation_failed",
                "层 %d（%d×%d）在索引中无任何 tile" % (lv.level, lv.width, lv.height))
        selected.append(lv)
        if single:
            stopped = True
    if not selected:
        raise KfbError("conversion_validation_failed", "无可写入的层级")
    return selected


def _level_grid(doc, lv):
    """把该层 tile 排成 (row, x) 网格序并校验覆盖完整性。"""
    grid = {}
    for tile in doc.tiles_by_level.get(lv.level, []):
        grid[(tile.row, tile.col)] = tile
    expected = lv.tiles_across * lv.tiles_down
    if len(grid) != expected:
        missing = [(r, c) for r in range(lv.tiles_down)
                   for c in range(lv.tiles_across) if (r, c) not in grid]
        raise KfbError(
            "conversion_validation_failed",
            "层 %d 网格覆盖不全：缺 %d/%d（首缺 %r）"
            % (lv.level, len(missing), expected, missing[:3]))
    # 网格 cell 的 JPEG 不得超出该 cell；厂商 KFB 末行可能短于
    # header 高度裁剪值（多余区域按白边处理）。
    for (row, col), tile in grid.items():
        want_w = min(TILE_W, lv.width - col * TILE_W)
        want_h = min(TILE_H, lv.height - row * TILE_H)
        if tile.jpeg_w > want_w or tile.jpeg_h > want_h:
            raise KfbError(
                "conversion_validation_failed",
                "层 %d tile(%d,%d) 尺寸 %d×%d 超出网格 %d×%d"
                % (lv.level, row, col, tile.jpeg_w, tile.jpeg_h, want_w, want_h))
    return [grid[(row, col)]
            for row in range(lv.tiles_down)
            for col in range(lv.tiles_across)]


# --------------------------------------------------------------------------- #
# 转换主入口
# --------------------------------------------------------------------------- #
def convert_kfb(src_path, dst_path, *, timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
                min_free_bytes=DEFAULT_MIN_FREE_BYTES,
                overwrite=False, on_progress=None):
    """把 kfb_bf_v1 转换为经典多 IFD JPEG tiled BigTIFF。

    返回 manifest dict（同时落盘 ``<dst>.manifest.json`` 与
    ``<dst>.associated/<name>.jpg``）。任何失败抛 :class:`KfbError`
    （稳定码），并清理半成品 ``.part``。
    """
    from .manifest import build_manifest, write_manifest

    src = Path(src_path)
    dst = Path(dst_path)
    if dst.exists() and not overwrite:
        raise KfbError("conversion_validation_failed",
                       "输出已存在：%s" % dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    usage = shutil.disk_usage(str(dst.parent))
    if usage.free < min_free_bytes:
        raise KfbError("conversion_disk_low",
                       "目标盘剩余 %d < %d" % (usage.free, min_free_bytes))

    doc = parse_kfb(src)
    part_path = dst.with_name(dst.name + ".part")
    assoc_dir = dst.with_name(dst.name + ".associated")
    assoc_dir_created = False
    started = time.monotonic()

    def _check_time():
        if time.monotonic() - started > float(timeout_seconds):
            raise KfbError("conversion_timeout",
                           "转换超过 %.1fs" % float(timeout_seconds))

    try:
        header = doc.header
        if not header.brightfield:
            raise KfbError("unsupported_kfb_variant",
                           "非明场 KFB（flags bit0=0）；Phase A 仅转换明场")
        if not (math.isfinite(header.mpp_x) and header.mpp_x > 0
                and math.isfinite(header.mpp_y) and header.mpp_y > 0):
            raise KfbError("metadata_missing_required", "MPP 缺失/非法")

        levels = _select_levels(doc)
        description = (json.dumps({
            "source_format": ("kfb_kfbio_jpeg" if header.version != 1
                              else "kfb_bf_v1"),
            "scanner_id": header.scanner_id,
            "mpp_x": header.mpp_x,
            "mpp_y": header.mpp_y,
            "objective": header.objective,
        }, ensure_ascii=True, sort_keys=True) + "\x00").encode("ascii")

        level_results = []
        warnings = []
        with open(part_path, "wb") as fh:
            writer = _BigTiffPyramidWriter(fh)
            for lv in levels:
                _check_time()
                tiles = _level_grid(doc, lv)
                # 该层 IFD 的采样：取第一个完整 tile 的 SOF；全部完整 tile
                # 必须一致（YCbCrSubSampling 是 IFD 级标签）
                sampling = None
                for tile in tiles:
                    if not tile.is_full_tile:
                        continue
                    probe = scan_jpeg(doc.tile_payload(tile))
                    if probe.sampling is None:
                        raise KfbError(
                            "conversion_validation_failed",
                            "层 %d tile(%d,%d) 不是三分量 JPEG"
                            % (lv.level, tile.row, tile.col))
                    if sampling is None:
                        sampling = probe.sampling
                    elif probe.sampling != sampling:
                        raise KfbError(
                            "conversion_validation_failed",
                            "层 %d tile 采样不一致：%r vs %r"
                            % (lv.level, probe.sampling, sampling))
                if sampling is None:
                    # 整层没有完整 tile（如 1×1 小层）：用重编码 tile 的采样
                    for tile in tiles:
                        probe = scan_jpeg(doc.tile_payload(tile))
                        if probe.sampling is not None:
                            sampling = probe.sampling
                            break
                if sampling not in _TIFF_SUBSAMPLING:
                    raise KfbError(
                        "conversion_validation_failed",
                        "层 %d JPEG 采样 %r 不在支持集（4:4:4/4:2:2/4:2:0）"
                        % (lv.level, sampling))
                pil_sub = _PILLOW_SUBSAMPLING[sampling]
                tiff_sub = _TIFF_SUBSAMPLING[sampling]

                offsets = []
                counts = []
                raw_copied = reencoded = 0
                for tile in tiles:
                    _check_time()
                    payload = doc.tile_payload(tile)
                    if tile.is_full_tile:
                        data = payload  # 原样复制，禁止二次有损
                        raw_copied += 1
                    else:
                        data, reused = _reencode_edge_tile(
                            payload, tile.jpeg_w, tile.jpeg_h,
                            pil_subsampling=pil_sub)
                        reencoded += 1
                        if not reused:
                            warnings.append(WARN_EDGE_REENCODE_FALLBACK_Q95)
                    off, cnt = writer.write_tile(data)
                    offsets.append(off)
                    counts.append(cnt)
                if fh.tell() > max_output_bytes:
                    raise KfbError(
                        "conversion_output_too_large",
                        "输出已写 %d > %d" % (fh.tell(), max_output_bytes))
                writer.add_level(
                    lv.width, lv.height, offsets, counts,
                    sampling=tiff_sub, mpp_x=header.mpp_x,
                    mpp_y=header.mpp_y, description=description,
                    reduced=bool(level_results))
                level_results.append({
                    "level": lv.level, "width": lv.width, "height": lv.height,
                    "tiles_across": lv.tiles_across,
                    "tiles_down": lv.tiles_down,
                    "tiles_total": len(tiles),
                    "tiles_raw_copied": raw_copied,
                    "tiles_reencoded": reencoded,
                })
                if on_progress:
                    on_progress(lv.level, len(tiles), fh.tell())
            writer.finish()
            fh.flush()
            os.fsync(fh.fileno())
        _validate_output(part_path, levels)

        # associated：OUT.associated/<name>.jpg（manifest 引用）
        if doc.associated:
            assoc_dir.mkdir(parents=True, exist_ok=True)
            assoc_dir_created = True
        for item in doc.associated:
            (assoc_dir / ("%s.jpg" % item.name)).write_bytes(
                doc.associated_payload(item))
            _check_time()

        manifest = build_manifest(
            src=src, dst=dst, part_path=part_path, header=header,
            level_results=level_results, warnings=sorted(set(warnings)),
            associated=doc.associated, assoc_dir=assoc_dir)
        # no-clobber：先 link 目标，成功后再写 sidecar。manifest 不能先于
        # dest 落盘，否则 link 失败时 sidecar 会被误认为本任务已拥有目标。
        try:
            os.link(str(part_path), str(dst))
        except FileExistsError:
            raise KfbError("conversion_validation_failed",
                           "输出已存在：%s" % dst)
        try:
            part_path.unlink()
        except OSError:
            pass
        write_manifest(manifest, dst.with_name(dst.name + ".manifest.json"))
        return manifest
    except Exception:
        # 失败清理：半成品 .part 不留（源文件永远保留）
        for p in (part_path,
                  dst.with_name(dst.name + ".manifest.json.part")):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        if assoc_dir_created:
            shutil.rmtree(assoc_dir, ignore_errors=True)
        raise
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
# 输出结构校验（promote 前）
# --------------------------------------------------------------------------- #
def _validate_output(part_path, levels):
    """用 tifffile 重开 .part 做结构校验（不整幅解码）。

    校验：IFD 数与层一致、每层尺寸/瓦片标签/压缩一致、每层首末 tile
    的 JPEG 可解码为 256×256（防 SOI/EOI 完好但流损坏）。
    """
    try:
        import tifffile
    except ImportError as e:  # pragma: no cover
        raise KfbError("conversion_validation_failed",
                       "校验依赖 tifffile 缺失：%s" % e)
    try:
        with open(part_path, "rb") as raw_fh, \
                tifffile.TiffFile(str(part_path)) as tf:
            if not tf.is_bigtiff:
                raise KfbError("conversion_validation_failed", "输出非 BigTIFF")
            pages = tf.pages
            if len(pages) != len(levels):
                raise KfbError(
                    "conversion_validation_failed",
                    "IFD 数 %d != 层数 %d" % (len(pages), len(levels)))
            from PIL import Image
            for page, lv in zip(pages, levels):
                if (page.imagewidth, page.imagelength) != (lv.width, lv.height):
                    raise KfbError(
                        "conversion_validation_failed",
                        "层 %d 尺寸 (%d,%d) != 期望 (%d,%d)"
                        % (lv.level, page.imagewidth, page.imagelength,
                           lv.width, lv.height))
                if int(page.tilewidth) != TILE_W or \
                        int(page.tilelength) != TILE_H:
                    raise KfbError("conversion_validation_failed",
                                   "层 %d tile 标签非 256" % lv.level)
                if int(page.compression) != 7:
                    raise KfbError("conversion_validation_failed",
                                   "层 %d 压缩非 JPEG" % lv.level)
                n_expected = lv.tiles_across * lv.tiles_down
                offsets = page.tags["TileOffsets"].value
                counts = page.tags["TileByteCounts"].value
                if len(offsets) != n_expected or len(counts) != n_expected:
                    raise KfbError("conversion_validation_failed",
                                   "层 %d 瓦片计数不符" % lv.level)
                for idx in (0, len(offsets) - 1):
                    raw_fh.seek(offsets[idx])
                    seg = raw_fh.read(counts[idx])
                    try:
                        im = Image.open(io.BytesIO(seg))
                        im.load()
                    except Exception as e:  # noqa: BLE001
                        raise KfbError(
                            "jpeg_decode_failed",
                            "输出层 %d tile[%d] 不可解码：%s"
                            % (lv.level, idx, e))
                    if im.size != (TILE_W, TILE_H):
                        raise KfbError(
                            "conversion_validation_failed",
                            "输出层 %d tile[%d] 解码尺寸 %r"
                            % (lv.level, idx, im.size))
    except KfbError:
        raise
    except Exception as e:  # noqa: BLE001
        raise KfbError("conversion_validation_failed",
                       "输出校验异常：%s" % e)
