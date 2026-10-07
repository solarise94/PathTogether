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
                        Aperio SVS（IFD0 压缩 = 7）、JPEG 编码明场
                        Leica SCN（描述是 SCN XML）、Hamamatsu NDPI
                        （Make 标识 Hamamatsu、整层单条带、压缩 = 7、
                        3 采样、photo 2/6）、通用瓦片 JPEG
                        TIFF/BigTIFF（无厂商描述、tiled、压缩 = 7、
                        3 采样、photo 2/6）、MRXS（.mrxs/.dat 成员）、
                        VMS（.vms 入口，完整包经文件夹选择交接）、
                        普通图片（未压缩 24/32 位 BMP——头解析判定位深
                        与压缩；基线 JPEG——FF D8 FF 魔数 + 头内 SOF0/1
                        三分量；渐进/灰度 JPEG 与 RLE/位域/调色板 BMP
                        不满足判定 → temporary）、Ventana BIF（BigTIFF +
                        IFD0 XMLPacket 带 iScan + JPEG 压缩；JPEG2000/
                        经典 TIFF/无 iScan 变体 → temporary）
                        → 本机转换后上传（工作台交接）
     temporary          暂时直传：尚无浏览器转换器的格式/变体（JPEG2000
                        编码 SVS（压缩 33003/33005）、JPEG2000/条带/
                        多通道变体的 NDPI、荧光/非 JPEG 编码
                        SCN、条带/LZW/deflate/非 8 位/多通道的通用
                        TIFF 变体、VMU、JPEG2000/经典 TIFF 的 BIF 变体、
                        SVSlide、
                        不满足可转换判定的 BMP/JPEG 变体、zip）
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
    "leica-scn-jpeg": 1,
    "generic-tiled-jpeg-tiff": 1,
    "hamamatsu-ndpi-jpeg": 1,
    "hamamatsu-vms-bundle": 1,
    "plain-image-bmp-jpeg": 1,
    "ventana-bif-jpeg": 1,
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

  // (dv, little) → {ifdOffset}；不合法返回 null。
  // 64 位偏移一律 DataView.getBigUint64(off, little)（按文件字节序）——曾把
  // 大端 u64 按“低 32 位在前”拼接（16×2^32 之类），并要求安全整数与上限：
  // 偏移必须落在文件内才是合法头。
  function readTiffHeader(dv, little) {
    if (dv.byteLength < 8) return null;
    var magic = dv.getUint16(2, little);
    if (magic !== 42 && magic !== 43) return null;   // 43 = BigTIFF
    var ifdOff;
    if (magic === 43) {
      // BigTIFF：offset size/reserved 校验 + 8 字节首 IFD 偏移（在 8..16）
      if (dv.byteLength < 16) return null;
      if (dv.getUint16(4, little) !== 8 || dv.getUint16(6, little) !== 0) {
        return null;
      }
      var big = dv.getBigUint64(8, little);
      if (big > BigInt(Number.MAX_SAFE_INTEGER)) return null;
      ifdOff = Number(big);
    } else {
      ifdOff = dv.getUint32(4, little);
    }
    if (ifdOff <= 0) return null;
    return { ifdOffset: ifdOff, bigtiff: magic === 43 };
  }

  // 在 IFD 区字节里找 tag；返回 {type, count, valueOffset} | null
  function findIfdEntry(ifdBytes, little, tag, bigtiff) {
    try {
      var dv = new DataView(ifdBytes.buffer, ifdBytes.byteOffset,
                            ifdBytes.byteLength);
      var entrySize = bigtiff ? 20 : 12;
      // 条目数按文件字节序：BigTIFF 是 u64，经典 TIFF 是 u16（曾对经典
      // 硬编码 getUint16(0, true)——大端 256 被读成 1，后面的条目全部漏扫）
      var count = bigtiff ? Number(dv.getBigUint64(0, little))
                          : dv.getUint16(0, little);
      if (count > 4096) return null;   // 防御：坏头不猜
      for (var i = 0; i < count; i++) {
        var at = (bigtiff ? 8 : 2) + i * entrySize;
        if (at + entrySize > ifdBytes.byteLength) return null;
        var t = dv.getUint16(at, little);   // tag 按文件字节序（曾硬编码小端）
        if (t !== tag) continue;
        var type = dv.getUint16(at + 2, little);
        var num = bigtiff ? Number(dv.getBigUint64(at + 4, little))
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
        // 64 位内联值按文件字节序拼高低 32 位（曾截成低 32 位）
        var lo = ifdDv.getUint32(entry.valueAt, little);
        var hi = ifdDv.getUint32(entry.valueAt + 4, little);
        return little ? lo + hi * 4294967296 : hi + lo * 4294967296;
      }
    } catch (e) { /* */ }
    return 0;
  }

  /// 64 位外联偏移（BigTIFF 值域字段 8 字节；经典 TIFF 4 字节）——按文件
  /// 字节序拼。本工具自己的产物可超 4 GiB，截成 32 位会读到错误位置。
  function valueOffset64(dv, at, little, bigtiff) {
    if (!bigtiff) return dv.getUint32(at, little);
    var lo = dv.getUint32(at, little);
    var hi = dv.getUint32(at + 4, little);
    return little ? lo + hi * 4294967296 : hi + lo * 4294967296;
  }

  function descTextAt(ifdBytes, entry, little, bigtiff) {
    try {
      var dv = new DataView(ifdBytes.buffer, ifdBytes.byteOffset,
                            ifdBytes.byteLength);
      var count = entry.count;
      var offset;
      if (count <= entry.inlineMax) {
        offset = entry.valueAt;
      } else {
        offset = valueOffset64(dv, entry.valueAt, little, bigtiff);
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
      var loc = descTextAt(ifdBytes, descEntry, little, hdr.bigtiff);
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
      return result;
    }
    if (ext === ".scn" && looksLikeLeicaScnXml(text)) {
      if (compression === 7 && scnBrightfield(text)) {
        // JPEG 编码明场 Leica SCN：浏览器转换器覆盖 → 本机转换后上传
        //（荧光/非 JPEG 编码的 SCN 落到默认 temporary = 暂时直传）
        result.cls = CLS.CONVERT;
        result.directClass = null;
      }
      return result;
    }
    if (ext === ".bif") {
      // Ventana BIF：IFD0 的 XMLPacket（700）携带 iScan 厂商块；BIF 是
      // BigTIFF，重叠瓦片拼接重编码已覆盖。JPEG2000（33003/33005）、
      // 经典 TIFF 容器或无 iScan 块 → 默认 temporary = 暂时直传。
      var xmpEntry = findIfdEntry(ifdBytes, little, 700, hdr.bigtiff);
      var xmpText = "";
      if (xmpEntry) {
        var xloc = descTextAt(ifdBytes, xmpEntry, little, hdr.bigtiff);
        if (xloc && xloc.count > 0) {
          var xcap = Math.min(xloc.count, 4096);
          var xraw = readRegion(headBytes, xloc.offset, xcap, more);
          if (xraw) {
            try { xmpText = new TextDecoder("utf-8", { fatal: false }).decode(xraw); }
            catch (e) { xmpText = ""; }
          }
        }
      }
      if (xmpText.indexOf("iScan") !== -1) {
        if (!hdr.bigtiff) return result;  // 经典 TIFF 的 ventana tif 变体
        if (compression === 7) {
          result.cls = CLS.CONVERT;
          result.directClass = null;
        }
        // JPEG2000/其他压缩 → temporary（声明例外由服务端词表校验）
      }
      return result;
    }
    if (ext === ".ndpi") {
      // F6：Hamamatsu NDPI 浏览器转换器覆盖。IFD0 的 Make（271）标识
      // Hamamatsu 且 IFD0 为整层单条带 JPEG 明场（压缩 7、3 采样、
      // photo 2/6、非分块）→ 本机转换后上传；JPEG2000（33003/33005）、
      // 分块存储或多通道变体不满足判定 → 默认 temporary = 暂时直传。
      if (compression === 7 && ndpiConvertible(headBytes, ifdBytes, idv, little, hdr.bigtiff)) {
        result.cls = CLS.CONVERT;
        result.directClass = null;
      }
      return result;
    }
    if (compression === 7 && genericTiledConvertible(ifdBytes, idv, little, hdr.bigtiff)) {
      // F5：通用瓦片 JPEG TIFF/BigTIFF（无厂商描述 + tiled + 3 采样 +
      // photo 2/6）：浏览器转换器覆盖 → 本机转换后上传。条带/LZW/
      // deflate/非 8 位/多通道变体不满足判定 → 默认 temporary = 暂时直传。
      result.cls = CLS.CONVERT;
      result.directClass = null;
      return result;
    }
    return result;
  }

  // F5：通用瓦片 JPEG TIFF 的可转换结构判定（与 Rust gtiff.rs / engine.js
  // 嗅探同一契约的 IFD0 快判）：tiled（322/323 在场）、SamplesPerPixel=3、
  // PhotometricInterpretation ∈ {2 RGB, 6 YCbCr}。只做分流提示——核心在
  // 复制前做同一批类型化终审。
  function genericTiledConvertible(ifdBytes, idv, little, bigtiff) {
    var tileW = findIfdEntry(ifdBytes, little, 322, bigtiff);
    var tileH = findIfdEntry(ifdBytes, little, 323, bigtiff);
    if (!tileW || !tileH) return false;
    var spp = findIfdEntry(ifdBytes, little, 277, bigtiff);
    if (spp && ifdUint(spp, idv, little) !== 3) return false;
    var photo = findIfdEntry(ifdBytes, little, 262, bigtiff);
    var photoVal = photo ? ifdUint(photo, idv, little) : 0;
    return photoVal === 2 || photoVal === 6;
  }

  // F6：Hamamatsu NDPI 的可转换结构判定（与 Rust ndpi.rs / engine.js 嗅探
  // 同一契约的 IFD0 快判）：Make（271）含 Hamamatsu、整层单条带（273/279
  // 在场且无 322/323）、SamplesPerPixel=3、PhotometricInterpretation ∈
  // {2, 6}。restart marker/层级/关联图分类由核心在复制后终审。
  function ndpiConvertible(headBytes, ifdBytes, idv, little, bigtiff) {
    var make = findIfdEntry(ifdBytes, little, 271, bigtiff);
    if (!make) return false;
    var makeLoc = descTextAt(ifdBytes, make, little, bigtiff);
    var makeText = "";
    if (makeLoc && makeLoc.count > 0) {
      // Make 值是绝对文件偏移（count > 4 恒为外联），从头缓冲读取
      var raw = readRegion(headBytes, makeLoc.offset,
        Math.min(makeLoc.count, 4096), null);
      if (raw) {
        try { makeText = new TextDecoder("utf-8", { fatal: false }).decode(raw); }
        catch (e) { makeText = ""; }
      }
    }
    if (makeText.indexOf("Hamamatsu") === -1) return false;
    if (findIfdEntry(ifdBytes, little, 322, bigtiff) ||
        findIfdEntry(ifdBytes, little, 323, bigtiff)) return false;
    if (!findIfdEntry(ifdBytes, little, 273, bigtiff) ||
        !findIfdEntry(ifdBytes, little, 279, bigtiff)) return false;
    var spp = findIfdEntry(ifdBytes, little, 277, bigtiff);
    if (spp && ifdUint(spp, idv, little) !== 3) return false;
    var photo = findIfdEntry(ifdBytes, little, 262, bigtiff);
    var photoVal = photo ? ifdUint(photo, idv, little) : 0;
    return photoVal === 2 || photoVal === 6;
  }

  // Leica SCN XML 描述（leica-microsystems.com/scn 命名空间）；
  // 明场判定与 Rust scn.rs / engine.js 嗅探同一规则（主图 illuminationSource）
  function looksLikeLeicaScnXml(text) {
    return !!text && text.indexOf("<scn") !== -1 &&
           text.indexOf("leica-microsystems.com/scn") !== -1;
  }

  function scnBrightfield(text) {
    if (!text) return false;
    var m = text.match(/<illuminationSource>\s*([A-Za-z]+)/);
    // 没有 illuminationSource 时不猜荧光：按明场放行（转换器核心再终审）
    return !m || m[1].toLowerCase() === "brightfield";
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

  // F8：普通图片的可转换头判定（与 Rust raster.rs / engine.js 嗅探同一
  // 契约的头快判，只做分流提示——变体终审在工具页有界探测与核心）。
  // BMP：DIB 头 12/40/52/56/108/124、未压缩、位深 24/32；JPEG：基线
  // SOF0/1 三分量（渐进/灰度/算术不满足 → temporary）。
  function classifyRasterHead(headBytes) {
    var bad = { cls: CLS.TEMPORARY, directClass: DIRECT_CLASS.LEGACY,
                compression: 0 };
    if (!headBytes || headBytes.length < 3) return bad;
    if (headBytes[0] === 0xFF && headBytes[1] === 0xD8 && headBytes[2] === 0xFF) {
      var i = 2, sof = null;
      while (i + 4 <= headBytes.length) {
        if (headBytes[i] !== 0xFF) return bad;
        while (i < headBytes.length && headBytes[i] === 0xFF) i++;
        if (i >= headBytes.length) break;
        var m = headBytes[i]; i++;
        if (m === 0xD9) break;
        if (m === 0x01 || (m >= 0xD0 && m <= 0xD7)) continue;
        if (i + 2 > headBytes.length) return bad;
        var ln = (headBytes[i] << 8) | headBytes[i + 1];
        if (ln < 2 || i + ln > headBytes.length) return bad;
        if (m === 0xC0 || m === 0xC1) sof = headBytes.subarray(i + 2, i + ln);
        if (m === 0xDA) break;
        i += ln;
      }
      if (sof && sof.length >= 6 && sof[5] === 3) {
        return { cls: CLS.CONVERT, directClass: null, compression: 0 };
      }
      return bad;
    }
    if (headBytes[0] === 0x42 && headBytes[1] === 0x4D && headBytes.length >= 26) {
      try {
        var dv = new DataView(headBytes.buffer, headBytes.byteOffset,
                              headBytes.byteLength);
        var dib = dv.getUint32(14, true);
        if ([12, 40, 52, 56, 108, 124].indexOf(dib) === -1) return bad;
        var bpp, compression = 0;
        if (dib === 12) {
          // OS/2 BITMAPCOREHEADER 只有 26 字节头
          bpp = dv.getUint16(24, true);
        } else {
          if (headBytes.length < 34) return bad;
          bpp = dv.getUint16(28, true);
          compression = dv.getUint32(30, true);
        }
        if (compression !== 0) return bad;
        if (bpp !== 24 && bpp !== 32) return bad;
        return { cls: CLS.CONVERT, directClass: null, compression: 0 };
      } catch (e) { return bad; }
    }
    return bad;
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
      case ".vms":
        // VMS 浏览器转换器已覆盖：入口 + 同目录 tile JPEG 的完整包经
        // 文件夹选择/整目录交接本机转换后上传（散入口在工具页得到列出
        // 缺成员的类型化信息；单文件直传不再声明）
        return { cls: CLS.CONVERT, ext: ext, bundle: true };
      case ".bmp":
      case ".jpg":
      case ".jpeg":
        // F8：普通图片浏览器转换器已覆盖（未压缩 24/32 位 BMP / 三分量
        // 基线 JPEG）——头解析分派；RLE/位域/调色板位深 BMP 与渐进/
        // 灰度 JPEG 不满足判定 → temporary（暂时直传，服务端终审）
        return { route: "raster", ext: ext };
      case ".svs":
        return { route: "tiff", ext: ext, svs: true };
      case ".scn":
        return { route: "tiff", ext: ext, scn: true };
      case ".ndpi":
        // F6：头解析分派（Make 标识 Hamamatsu + 整层单条带 JPEG →
        // convert；JP2K/多通道/分块变体 → temporary）
        return { route: "tiff", ext: ext, ndpi: true };
      case ".zip":
        // zip 是运输容器（MRXS 包/多文件），不是切片直传类别——不携带
        // direct_class 声明（服务端词表里 zip 只在受理词表，不在
        // direct_upload；声明 legacy-direct 会被 422 invalid_direct_class）
        return { cls: CLS.TEMPORARY, ext: ext };
      case ".bif":
        // Ventana BIF 浏览器转换器已覆盖（JPEG 编码 + RIGHT/UP 拼接）——
        // 头解析分派（IFD0 XMLPacket 带 iScan + BigTIFF + JPEG 压缩 →
        // convert；JPEG2000/经典 TIFF/其余变体 → temporary）
        return { route: "tiff", ext: ext, bif: true };
      case ".vmu":
      case ".svslide":
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
      var read = function (start, end) {
        return Promise.resolve(
          file.slice(start, end).arrayBuffer());
      };
      // 普通图片类：读头（BMP ≤ 138 字节 / JPEG 标记走到 SOF；合计
      // ≤ HEAD_BYTES），按头判定 convert / temporary
      if (routed.route === "raster") {
        return read(0, Math.min(HEAD_BYTES, file.size || HEAD_BYTES))
          .then(function (headBuf) {
            var r = classifyRasterHead(new Uint8Array(headBuf));
            r.ext = routed.ext;
            r.bundle = false;
            r.svsJp2k = false;
            r.directClass = r.directClass || null;
            return r;
          }, function () {
            // 嗅探读失败：按暂时直传降级（服务端终审）
            return { cls: CLS.TEMPORARY, directClass: DIRECT_CLASS.LEGACY,
                     ext: routed.ext, bundle: false, svsJp2k: false,
                     compression: 0 };
          });
      }
      // TIFF 类：读头（8 字节魔数定位 IFD；合计 ≤ HEAD_BYTES）
      return read(0, Math.min(HEAD_BYTES, file.size || HEAD_BYTES))
        .then(function (headBuf) {
          var head = new Uint8Array(headBuf);
          // 首轮：IFD/描述可能越出头部 → 解析失败再补读一段（IFD 常在
          // 头部，二轮只是兜底；合计仍 ≤ 2×HEAD_BYTES 的头区域）
          var first = classifyTiffHead(head, null, routed.ext);
          if (first.cls === CLS.TEMPORARY &&
              first.directClass === DIRECT_CLASS.LEGACY &&
              first.compression === 0) {
            // BIF：IFD0 常在标签图载荷之后（真实样本约 0.5 MB 处），超出
            // 头窗口——按头字段指向的首 IFD 偏移再补读一段（≤ 96 KiB，
            // 合计仍有界）
            if (routed.bif && head.length >= 16) {
              // 按文件自身的字节序读首 IFD 偏移（曾硬编码小端）
              var dv0 = new DataView(head.buffer, head.byteOffset, head.byteLength);
              var le0 = head[0] === 0x49 && head[1] === 0x49;
              var isBig = dv0.getUint16(2, le0) === 43;
              var ifdOff0 = valueOffset64(dv0, isBig ? 8 : 4, le0, isBig);
              if (ifdOff0 > 0 && ifdOff0 < (file.size || Infinity)) {
                // 窗口从 IFD 前 64 KiB 起：外联值（描述/XMLPacket）既可能
                // 排在 IFD 表之后（真实布局），也可能排在前面
                var wFrom = Math.max(0, ifdOff0 - 64 * 1024);
                var wLen = Math.min(160 * 1024, Math.max(0, (file.size || ifdOff0) - wFrom));
                return read(wFrom, wFrom + wLen).then(function (ifdBuf) {
                  var more2 = ifdBuf && ifdBuf.byteLength
                    ? { bytes: new Uint8Array(ifdBuf), baseOffset: wFrom }
                    : null;
                  return classifyTiffHead(head, more2, routed.ext);
                }, function () {
                  return first;
                });
              }
            }
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
    classifyRasterHead: classifyRasterHead,
    classifyFile: classifyFile,
    extOf: extOf,
  };
})();
