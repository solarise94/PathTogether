/* =========================================================================
   slide-sniff.js — 直传类别嗅探（共享；先转换后上传阶段 1）。
   工作台（static/app.js，classic script）与本地切片工具页（/tools/slides）
   共用的纯函数模块：只用 Blob.slice 读文件头（合计 ≤ 128 KB，绝不读整个
   文件），把文件判为：

     ome-tiff           TIFF 且 ImageDescription 含 OME-XML → 直接上传
                        （direct_class="ome-tiff"）
     converter-bigtiff  本机转换工具导出的经典 BigTIFF——描述是转换器写的
                        JSON 且带来源标记（source_format ∈
                        CONVERTER_SOURCE_FORMATS，与服务端
                        upload_direct_class / Rust convert_*.rs 同一词表）
                        → 直接上传（direct_class="converter-bigtiff"）
     convert            有浏览器转换器的格式：KFB、KFBF、JPEG 编码的
                        Aperio SVS（IFD0 压缩 = 7）、MRXS（.mrxs/.dat
                        成员）→ 本机转换后上传（工作台交接）
     temporary          暂时直传：尚无浏览器转换器的格式/变体（JPEG2000
                        编码 SVS（压缩 33003/33005）、NDPI、VMS、VMU、
                        SCN、BIF、SVSlide、BMP/JPEG、普通 TIFF、zip）
     unsupported        未登记扩展名

   形态约束：classic script（window.HP_SLIDE_SNIFF；与 cos-uploader.js
   同风格），无 DOM、无 i18n、零网络请求。判定只是**分流提示**：服务端在
   创建时校验声明词表、worker 在 open_slide 前按 upload_direct_class 复核
   （不符 → convert_in_browser）。
   ========================================================================= */
(function () {
  "use strict";

  // 转换器来源标记（服务端 upload_direct_class.CONVERTER_SOURCE_FORMATS
  // 同词表；来源：kfb/converter.py、slide-transform-core convert_*.rs）
  var CONVERTER_SOURCE_FORMATS = {
    "kfb_bf_v1": 1,
    "kfb_kfbio_jpeg": 1,
    "aperio-svs-jpeg": 1,
    "mirax-bundle": 1,
  };

  // 结果类别
  var CLS = {
    OME: "ome-tiff",
    CONVERTER: "converter-bigtiff",
    CONVERT: "convert",
    TEMPORARY: "temporary",
    UNSUPPORTED: "unsupported",
  };

  // direct_class 声明值（/api/ingestions direct_class 字段）
  var DIRECT_CLASS = {
    OME: "ome-tiff",
    CONVERTER: "converter-bigtiff",
    LEGACY: "legacy-direct",
    SVS_JP2K: "unconverted-variant:svs-jp2k",
  };

  var HEAD_BYTES = 128 * 1024;   // 阶段 1 合同：合计最多约 128 KB

  function extOf(name) {
    var base = String(name || "").replace(/\\/g, "/").split("/").pop();
    var i = base.lastIndexOf(".");
    return i < 0 ? "" : base.slice(i).toLowerCase();
  }

  function lower(value) {
    return String(value || "").toLowerCase();
  }

  // ---------- 最小 TIFF 头解析（魔数 + IFD0 + 描述标签） ----------

  // (dv, little) → {ifdOffset}；不合法返回 null
  function readTiffHeader(dv, little) {
    if (dv.byteLength < 8) return null;
    var magic = dv.getUint16(2, little);
    if (magic !== 42 && magic !== 43) return null;   // 43 = BigTIFF
    var ifdOff = dv.getUint32(4, little);
    if (ifdOff <= 0) return null;
    return { ifdOffset: ifdOff, bigtiff: magic === 43 };
  }

  // 在 IFD 区字节里找 tag；返回 {type, count, valueOffset} | null
  function findIfdEntry(ifdBytes, little, tag, bigtiff) {
    try {
      var dv = new DataView(ifdBytes.buffer, ifdBytes.byteOffset,
                            ifdBytes.byteLength);
      var entrySize = bigtiff ? 20 : 12;
      var count = bigtiff ? dv.getUint64(0, true) : dv.getUint16(0, true);
      if (count > 4096) return null;   // 防御：坏头不猜
      for (var i = 0; i < count; i++) {
        var at = (bigtiff ? 8 : 2) + i * entrySize;
        if (at + entrySize > ifdBytes.byteLength) return null;
        var t = bigtiff ? dv.getUint16(at, true) : dv.getUint16(at, true);
        if (t !== tag) continue;
        var type = dv.getUint16(at + 2, little);
        var num = bigtiff ? dv.getUint64(at + 4, true)
                          : dv.getUint32(at + 4, little);
        // 值内联在值域字段（≤4/8 字节）或值域字段是偏移
        return { type: type, count: num,
                 valueAt: at + (bigtiff ? 12 : 8),
                 inlineMax: bigtiff ? 8 : 4 };
      }
    } catch (e) { /* 坏头 */ }
    return null;
  }

  function ifdUint(entry, ifdDv, little) {
    // SHORT/LONG 内联值（count=1 的常规情形）
    try {
      if (entry.type === 3) return ifdDv.getUint16(entry.valueAt, little);
      if (entry.type === 4) return ifdDv.getUint32(entry.valueAt, little);
      if (entry.type === 16 || entry.type === 17) {  // LONG8 族（BigTIFF）
        return ifdDv.getUint32(entry.valueAt, little);
      }
    } catch (e) { /* */ }
    return 0;
  }

  function descTextAt(ifdBytes, entry, little) {
    try {
      var dv = new DataView(ifdBytes.buffer, ifdBytes.byteOffset,
                            ifdBytes.byteLength);
      var count = entry.count;
      var offset;
      if (count <= entry.inlineMax) {
        offset = entry.valueAt;
      } else {
        offset = dv.getUint32(entry.valueAt, little);
      }
      return { offset: offset, count: count };
    } catch (e) {
      return null;
    }
  }

  function looksLikeOmeXml(text) {
    if (!text) return false;
    var head = text.slice(0, 4096);
    return /^\s*<\?xml/i.test(head) && /OME/i.test(head.slice(0, 2048));
  }

  function converterMarked(text) {
    if (!text) return false;
    var body = text.split("\x00", 1)[0].trim();
    if (body.charAt(0) !== "{") return false;
    var obj;
    try { obj = JSON.parse(body); } catch (e) { return false; }
    if (!obj || typeof obj !== "object") return false;
    return Object.prototype.hasOwnProperty.call(
      CONVERTER_SOURCE_FORMATS, String(obj.source_format || ""));
  }

  /**
   * classifyTiffHead(headBytes, moreBytes, ext) — 纯函数（可注入字节，
   * vitest 直用）。headBytes：文件前 8 字节起的整段头（≤HEAD_BYTES）；
   * moreBytes：描述/IFD 超出 headBytes 时的**续读段**（可 null，带
   * baseOffset 标注其在文件中的起点）。ext：小写扩展名（.svs 等）。
   * 返回 { cls, directClass, compression }：
   *   cls ∈ ome-tiff | converter-bigtiff | temporary（含 svs-jp2k / 普通
   *   TIFF）——convert/unsupported 由扩展名快路径在 classifyExt 决定。
   */
  function classifyTiffHead(headBytes, more, ext) {
    var result = { cls: CLS.TEMPORARY, directClass: DIRECT_CLASS.LEGACY,
                   compression: 0 };
    if (!headBytes || headBytes.byteLength < 8) return result;
    var h = new DataView(headBytes.buffer || headBytes,
                         headBytes.byteOffset || 0,
                         headBytes.byteLength);
    var b0 = h.getUint8(0), b1 = h.getUint8(1);
    var little;
    if (b0 === 0x49 && b1 === 0x49) little = true;
    else if (b0 === 0x4D && b1 === 0x4D) little = false;
    else return result;
    var hdr = readTiffHeader(h, little);
    if (!hdr) return result;

    // IFD 区可能位于头部之外：优先 headBytes，越界用续读段
    var ifdBytes = sliceRegion(headBytes, hdr.ifdOffset, 4 + 4096 * 24 + 64,
                               more);
    if (!ifdBytes) return result;
    var idv = new DataView(ifdBytes.buffer, ifdBytes.byteOffset,
                           ifdBytes.byteLength);
    var compEntry = findIfdEntry(ifdBytes, little, 259, hdr.bigtiff);
    var compression = compEntry ? ifdUint(compEntry, idv, little) : 0;
    result.compression = compression;

    var descEntry = findIfdEntry(ifdBytes, little, 270, hdr.bigtiff);
    var text = "";
    if (descEntry) {
      var loc = descTextAt(ifdBytes, descEntry, little);
      if (loc && loc.count > 0) {
        var cap = Math.min(loc.count, HEAD_BYTES);
        var raw = readRegion(headBytes, loc.offset, cap, more);
        if (raw) {
          try {
            text = new TextDecoder("utf-8", { fatal: false }).decode(raw);
          } catch (e) { text = ""; }
        }
      }
    }
    if (looksLikeOmeXml(text)) {
      result.cls = CLS.OME;
      result.directClass = DIRECT_CLASS.OME;
      return result;
    }
    if (converterMarked(text)) {
      result.cls = CLS.CONVERTER;
      result.directClass = DIRECT_CLASS.CONVERTER;
      return result;
    }
    if (ext === ".svs" && (compression === 33003 || compression === 33005)) {
      // JPEG2000 编码 SVS：暂无浏览器转换器 → 暂时直传（声明例外）
      result.directClass = DIRECT_CLASS.SVS_JP2K;
      return result;
    }
    if (ext === ".svs" && compression === 7) {
      // JPEG 编码 Aperio SVS：浏览器转换器覆盖 → 本机转换后上传
      result.cls = CLS.CONVERT;
      result.directClass = null;
    }
    return result;
  }

  // ---- 区域读取辅助（head 优先，越界落到续读段 more={bytes,baseOffset}) --
  function sliceRegion(headBytes, offset, cap, more) {
    var headLen = headBytes ? headBytes.byteLength : 0;
    if (offset < headLen) {
      var end = Math.min(headLen, offset + cap);
      if (end - offset >= 2) return headBytes.subarray(offset, end);
    }
    if (more && more.bytes && more.baseOffset !== undefined &&
        offset >= more.baseOffset &&
        offset + 2 <= more.baseOffset + more.bytes.byteLength) {
      var mAt = offset - more.baseOffset;
      var mEnd = Math.min(more.bytes.byteLength, mAt + cap);
      return more.bytes.subarray(mAt, mEnd);
    }
    return null;
  }

  function readRegion(headBytes, offset, cap, more) {
    var headLen = headBytes ? headBytes.byteLength : 0;
    var parts = [];
    if (offset < headLen) {
      var fromHead = Math.min(headLen, offset + cap);
      parts.push(headBytes.subarray(offset, fromHead));
    }
    var needEnd = offset + cap;
    if (needEnd > headLen && more && more.bytes &&
        more.baseOffset !== undefined) {
      var mFrom = Math.max(0, headLen - more.baseOffset);
      var mTo = Math.min(more.bytes.byteLength, needEnd - more.baseOffset);
      if (mTo > mFrom) parts.push(more.bytes.subarray(mFrom, mTo));
    }
    if (!parts.length) return null;
    if (parts.length === 1) return parts[0];
    var total = 0;
    parts.forEach(function (p) { total += p.byteLength; });
    var out = new Uint8Array(total);
    var at = 0;
    parts.forEach(function (p) { out.set(p, at); at += p.byteLength; });
    return out;
  }

  /**
   * classifyExt(name) — 纯扩展名快路径（不读字节）。
   * 返回 { route: 'tiff' | cls, ... }：tiff → 需要头解析；否则直接给类别。
   */
  function classifyExt(name) {
    var ext = extOf(name);
    switch (ext) {
      case ".tif":
      case ".tiff":
      case ".ome.tif":     // 命名只是提示：头里真有 OME-XML 才声明 ome-tiff
      case ".ome.tiff":
        return { route: "tiff", ext: ext };
      case ".kfb":
      case ".kfbf":
        return { cls: CLS.CONVERT, ext: ext };
      case ".mrxs":
      case ".dat":
        return { cls: CLS.CONVERT, ext: ext, bundle: true };
      case ".svs":
        return { route: "tiff", ext: ext, svs: true };
      case ".zip":
        return { cls: CLS.TEMPORARY, directClass: DIRECT_CLASS.LEGACY,
                 ext: ext };
      case ".ndpi":
      case ".vms":
      case ".vmu":
      case ".scn":
      case ".bif":
      case ".svslide":
      case ".bmp":
      case ".jpg":
      case ".jpeg":
        return { cls: CLS.TEMPORARY, directClass: DIRECT_CLASS.LEGACY,
                 ext: ext };
      default:
        return { cls: CLS.UNSUPPORTED, ext: ext };
    }
  }

  /**
   * classifyFile(file) — 工作台/工具页共用的唯一入口。
   * 返回 Promise<{
   *   cls: 'ome-tiff'|'converter-bigtiff'|'convert'|'temporary'|'unsupported',
   *   directClass: string|null,        // 直传类带声明值；其余 null
   *   ext: string,                     // 小写扩展名
   *   bundle: bool,                    // MRXS 成员（交接整文件夹）
   *   svsJp2k: bool,                   // JPEG2000 编码 SVS（声明例外）
   *   compression: number,             // TIFF IFD0 压缩码（诊断用）
   * }>
   * 读失败按扩展名快路径降级（绝不因嗅探失败阻塞上传——服务端终审）。
   */
  function classifyFile(file) {
    return Promise.resolve().then(function () {
      var routed = classifyExt(file && file.name);
      if (routed.cls) {
        return { cls: routed.cls, directClass: routed.directClass || null,
                 ext: routed.ext, bundle: !!routed.bundle, svsJp2k: false,
                 compression: 0 };
      }
      // TIFF 类：读头（8 字节魔数定位 IFD；合计 ≤ HEAD_BYTES）
      var read = function (start, end) {
        return Promise.resolve(
          file.slice(start, end).arrayBuffer());
      };
      return read(0, Math.min(HEAD_BYTES, file.size || HEAD_BYTES))
        .then(function (headBuf) {
          var head = new Uint8Array(headBuf);
          // 首轮：IFD/描述可能越出头部 → 解析失败再补读一段（IFD 常在
          // 头部，二轮只是兜底；合计仍 ≤ 2×HEAD_BYTES 的头区域）
          var first = classifyTiffHead(head, null, routed.ext);
          if (first.cls === CLS.TEMPORARY &&
              first.directClass === DIRECT_CLASS.LEGACY &&
              first.compression === 0) {
            return read(HEAD_BYTES, HEAD_BYTES * 2).then(function (moreBuf) {
              var more = moreBuf && moreBuf.byteLength
                ? { bytes: new Uint8Array(moreBuf), baseOffset: HEAD_BYTES }
                : null;
              return classifyTiffHead(head, more, routed.ext);
            }, function () {
              return first;
            });
          }
          return first;
        })
        .then(function (r) {
          r.ext = routed.ext;
          r.bundle = false;
          r.svsJp2k = r.directClass === DIRECT_CLASS.SVS_JP2K;
          if (r.cls !== CLS.OME && r.cls !== CLS.CONVERTER) {
            r.directClass = r.svsJp2k ? DIRECT_CLASS.SVS_JP2K
                                      : DIRECT_CLASS.LEGACY;
          }
          return r;
        }, function () {
          // 嗅探读失败：按普通 TIFF 暂时直传降级（服务端终审）
          return { cls: CLS.TEMPORARY, directClass: DIRECT_CLASS.LEGACY,
                   ext: routed.ext, bundle: false, svsJp2k: false,
                   compression: 0 };
        });
    });
  }

  window.HP_SLIDE_SNIFF = {
    CLS: CLS,
    DIRECT_CLASS: DIRECT_CLASS,
    CONVERTER_SOURCE_FORMATS: CONVERTER_SOURCE_FORMATS,
    classifyExt: classifyExt,
    classifyTiffHead: classifyTiffHead,
    classifyFile: classifyFile,
    extOf: extOf,
  };
})();
