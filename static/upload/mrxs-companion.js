/* =========================================================================
   mrxs-companion.js — MRXS「先选 .mrxs，再选同名数据文件夹」的共享协助逻辑。

   背景：网页拿不到用户所选 .mrxs 旁边的同级目录，只能让用户再选一次文件夹。
   本模块把两个页面（工作台上传面板 static/upload/inline-convert.js 与本地
   转换器页 /tools/slides）共用的**纯判定与装配**集中在这里：

     - stemOf / namesMatch      主入口词干与文件夹名的匹配（NFC 归一 +
                                小写折叠：macOS 可能给出 NFD 中文名）；
     - detectNeedsFolder        散文件输入里「只有一个 .mrxs、没有任何目录
                                结构」→ 需要同名数据文件夹；
     - planCompanionFolder      用户选了文件夹之后：同名的数据文件夹本身（a）
                                / 上一级父文件夹（b）/ 不同名但根目录有
                                Slidedat.ini（c，需显式确认）/ 没有
                                Slidedat.ini（d，拒绝）；
     - mergeRows                把已选的 .mrxs File 与文件夹成员装配成
                                engine.sniffMrxBundle / planBundle 认的
                                {name, relPath, file} 行（relPath 布局见下）；
     - collectDirectoryHandle   showDirectoryPicker 句柄的异步遍历（values()
                                递归，同一成员上限）；
     - pickDirectory            打开目录选择器（{id, mode:'read'}，仅在真有
                                句柄时传 startIn；TypeError 回退重试；
                                AbortError=用户取消；SecurityError/无 API 由
                                调用方回退到隐藏的 webkitdirectory input）。

   是否「完整包」的唯一权威仍是 engine.planBundle / sniffMrxBundle 与
   runner.prepareBundle——本模块只决定**装出哪些行**，绝不预判完整性。

   relPath 布局（与 planner 对齐，见 engine.js sniffMrxBundle）：入口行的
   relPath 就是 .mrxs 文件名本身（无目录前缀），成员行 relPath =「<stem>/
   …」。sniffMrxBundle 以入口所在目录为包根（rootLen = 入口段数 − 1），成员
   名 = 去掉 rootLen 段后的剩余路径，因此入口与文件夹**不需要**共同的顶层
   父目录；文件夹成员的第一段统一改写成入口词干 stem（大小写/NFD 差异就此
   归一），planner 找「{stem}/Slidedat.ini」必然命中。

   形态约束：classic script（window.HP_MRXS_COMPANION），无 DOM、无 i18n、
   零网络；纯函数 + 可注入依赖（win/getFile），vitest 用 new Function 直接
   驱动（tests/js/mrxs-companion.test.ts）。
   ========================================================================= */
(function () {
  "use strict";

  var DEFAULT_MAX_MEMBERS = 8192;   // 镜像 engine.MRXS_MAX_MEMBERS（可覆盖）

  /// 「病例A.mrxs」→「病例A」（去 .mrxs 后缀，大小写不敏感；其余原样）。
  function stemOf(name) {
    var base = String(name || "").replace(/\\/g, "/").split("/").pop();
    return base.replace(/\.mrxs$/i, "");
  }

  /// 比较用归一：Unicode NFC（macOS 目录名可能是 NFD）+ 小写折叠（中文名
  /// 不受影响，拉丁/带音调名字大小写不敏感）。
  function normalizeName(s) {
    try {
      return String(s || "").normalize("NFC").toLowerCase();
    } catch (e) {
      return String(s || "").toLowerCase();
    }
  }

  function namesMatch(a, b) {
    return normalizeName(a) === normalizeName(b);
  }

  /// {name, relPath, file} 行形状归一（容 File 与已归一行两种输入）。
  function toRow(f) {
    if (!f || typeof f !== "object") return null;
    var file = (typeof f.file !== "undefined") ? f.file : f;
    if (!file) return null;
    var rel = String(f.relPath || f.webkitRelativePath || f.name || "");
    var name = String(f.name || rel.split("/").pop() || "");
    if (!rel || !name) return null;
    return { name: name, relPath: rel, file: file };
  }

  /**
   * detectNeedsFolder(rows) — 散文件输入是否进入「选择同名数据文件夹」状态。
   * 条件（都满足）：
   *   - 恰好一个 .mrxs 入口（多个/零个 → null，交给 planner/其他流程）；
   *   - 其余行全是散文件（relPath 无目录层）——有任何 ≥2 层的行说明文件夹
   *     已经在场（整包或目录 drop），完整性交给 planner 判定。
   * 返回 { stem, entryName, entryRow } | null。VMS（.vms）绝不进入本流程。
   */
  function detectNeedsFolder(rows) {
    var list = [];
    for (var i = 0; i < (rows || []).length; i++) {
      var r = toRow(rows[i]);
      if (r) list.push(r);
    }
    var entries = [];
    for (var j = 0; j < list.length; j++) {
      var leaf = list[j].relPath.split("/").pop();
      if (/\.mrxs$/i.test(leaf)) entries.push({ row: list[j], leaf: leaf });
    }
    if (entries.length !== 1) return null;
    var stem = stemOf(entries[0].leaf);
    if (!stem || stem === "." || stem === "..") return null;
    for (var k = 0; k < list.length; k++) {
      if (list[k] === entries[0].row) continue;
      var segs = list[k].relPath.split("/").filter(function (s) { return s.length; });
      if (segs.length >= 2) return null;
    }
    return { stem: stem, entryName: entries[0].leaf, entryRow: entries[0].row };
  }

  /// 在文件夹成员行里找**全部** Slidedat.ini（大小写不敏感）。父文件夹里
  /// 常见多张切片（病例A/、病例B/…各自带 Slidedat.ini），必须全扫再挑，
  /// 绝不能按遍历顺序拿第一个。返回 [{ row, segs, index }]
  ///（index = 'Slidedat.ini' 在 relPath 段中的位置）。
  function findSlidedatCandidates(rows) {
    var out = [];
    for (var i = 0; i < (rows || []).length; i++) {
      var r = toRow(rows[i]);
      if (!r) continue;
      var segs = r.relPath.split("/").filter(function (s) { return s.length; });
      if (segs.length >= 2 &&
          normalizeName(segs[segs.length - 1]) === "slidedat.ini") {
        out.push({ row: r, segs: segs, index: segs.length - 1 });
      }
    }
    return out;
  }

  /**
   * planCompanionFolder({ stem, folderName, rows }) — 选完文件夹后的判定。
   * rows 是所选文件夹的成员行（webkitRelativePath 或目录遍历产出，首段是
   * 所选文件夹名）。返回：
   *   { status: "use",     prefixSegs, prefixSegCount: 1 }
   *       (a) 所选文件夹本身就是同名数据文件夹（首段与 stem 同名且根上有
   *           Slidedat.ini）；
   *   { status: "parent",  prefixSegs, prefixSegCount: n }
   *       (b) 选的是上一级（或更上）文件夹，其下有 <stem>/ 子目录（根上是
   *           <stem>/Slidedat.ini）。父文件夹里若还有 <stem>.mrxs，那就是
   *           完整包——装配时入口以已选的 File 为准，效果一致；
   *   { status: "mismatch", folderName }
   *       (c) 根上有 Slidedat.ini 但与 stem 不同名 → 需用户显式确认；
   *   { status: "reject" }
   *       (d) 根上、以及任何同名子目录下都没有 Slidedat.ini。
   *
   * Slidedat.ini 的挑选（2026-10-22 复核）：扫描**全部**候选，优先取「直接
   * 上一级目录名与 stem 同名」且**最浅**的那一个——父文件夹里多张切片
   * （病例B/ 排在病例A/ 之前）绝不误判为 reject；否则根上有 Slidedat.ini
   * → mismatch；否则 reject。
   */
  function planCompanionFolder(opts) {
    var stem = String((opts && opts.stem) || "");
    var rows = (opts && opts.rows) || [];
    if (!stem) return { status: "reject" };
    var cands = findSlidedatCandidates(rows);
    // 1) 直接上一级目录与 stem 同名的候选：取最浅
    var matching = cands.filter(function (c) {
      var dirs = c.segs.slice(0, c.index);
      return dirs.length >= 1 && namesMatch(dirs[dirs.length - 1], stem);
    }).sort(function (a, b) { return a.index - b.index; });
    if (matching.length) {
      var dirs = matching[0].segs.slice(0, matching[0].index);
      return {
        status: dirs.length === 1 ? "use" : "parent",
        prefixSegs: dirs,
        prefixSegCount: dirs.length,
      };
    }
    // 2) 所选文件夹根上就有 Slidedat.ini（但与 stem 不同名）→ mismatch
    for (var i = 0; i < cands.length; i++) {
      if (cands[i].index === 1) {
        return {
          status: "mismatch",
          folderName: String(opts.folderName || cands[i].segs[0] || ""),
          prefixSegs: cands[i].segs.slice(0, 1),
          prefixSegCount: 1,
        };
      }
    }
    // (d)：没有 Slidedat.ini（或只在无关的深层目录里）
    return { status: "reject" };
  }

  /**
   * mergeRows({ stem, entryName, entryFile, rows, prefixSegs, maxMembers }) —
   * 装配进 planner 的行。入口行 relPath = entryName（文件名本身）；成员行
   * relPath =「<stem>/<文件夹内相对路径>」。非数据子树（父文件夹里的兄弟
   * 目录/散文件）与任何 .mrxs（入口已单独提供，避免「多个 .mrxs」拒绝）
   * 都被排除。返回 { ok: true, rows } | { ok: false, code }。
   * 超过 maxMembers（镜像 engine.MRXS_MAX_MEMBERS）→ { ok:false,
   * code:"too_many_members" }——在进入 planner/复制之前拒绝。
   */
  function mergeRows(opts) {
    var stem = String((opts && opts.stem) || "");
    var prefixSegs = (opts && opts.prefixSegs) || [];
    var rows = (opts && opts.rows) || [];
    var cap = (opts && typeof opts.maxMembers === "number")
      ? opts.maxMembers : DEFAULT_MAX_MEMBERS;
    var entryName = String((opts && opts.entryName) || "");
    if (!stem || !opts.entryFile || !entryName || !prefixSegs.length) {
      return { ok: false, code: "no_entry" };
    }
    var prefix = prefixSegs.join("/");
    var out = [{ name: entryName, relPath: entryName, file: opts.entryFile }];
    for (var i = 0; i < rows.length; i++) {
      var r = toRow(rows[i]);
      if (!r) continue;
      var segs = r.relPath.split("/").filter(function (s) { return s.length; });
      if (segs.length <= prefixSegs.length) continue;          // 不在数据子树内
      var head = segs.slice(0, prefixSegs.length).join("/");
      if (head !== prefix) continue;                           // 兄弟子树/散文件
      var leaf = segs[segs.length - 1];
      if (/\.mrxs$/i.test(leaf)) continue;                     // 入口已单独提供
      out.push({
        name: leaf,
        relPath: stem + "/" + segs.slice(prefixSegs.length).join("/"),
        file: r.file,
      });
      if (out.length > cap) {
        return { ok: false, code: "too_many_members", cap: cap };
      }
    }
    return { ok: true, rows: out };
  }

  /**
   * collectDirectoryHandle(dirHandle, { maxMembers }, deps) — 目录句柄递归
   * 遍历（values() 异步迭代；成员行 relPath 以所选文件夹名开头，与
   * webkitRelativePath 同形状）。超过上限立即抛 { code:"too_many_members" }。
   * deps.getFile 可注入（默认 handle.getFile()）。
   */
  async function collectDirectoryHandle(dirHandle, opts, deps) {
    var maxMembers = (opts && typeof opts.maxMembers === "number")
      ? opts.maxMembers : DEFAULT_MAX_MEMBERS;
    var getFile = (deps && typeof deps.getFile === "function")
      ? deps.getFile : function (h) { return h.getFile(); };
    var rows = [];
    await walkHandle(dirHandle, String(dirHandle.name || ""), rows,
      { maxMembers: maxMembers, getFile: getFile });
    return rows;
  }

  async function walkHandle(dir, prefix, rows, caps) {
    for await (const handle of dir.values()) {
      if (handle && handle.kind === "file") {
        var file = await caps.getFile(handle);
        rows.push({
          name: handle.name,
          relPath: prefix ? prefix + "/" + handle.name : handle.name,
          file: file,
        });
        if (rows.length > caps.maxMembers) {
          var e = new Error("目录成员数超过上限 " + caps.maxMembers);
          e.code = "too_many_members";
          throw e;
        }
      } else if (handle && handle.kind === "directory") {
        await walkHandle(handle, prefix ? prefix + "/" + handle.name : handle.name,
          rows, caps);
      }
    }
  }

  /**
   * collectCompanionFolder(dirHandle, stem, { maxMembers }, deps) — 协助流程
   * 的目录遍历（复核 2026-10-22）：父文件夹里可能放着**很多**切片，把整棵
   * 树都遍历（每个文件还各来一次 getFile()）既慢又会撞成员上限——而目标
   * 子树其实很小。因此：picked 目录名与 stem 不同名时，先扫它的**顶层**
   * 条目找与 stem 同名的直接子目录，只遍历那个子目录（relPath 仍以 picked
   * 目录名开头 → planCompanionFolder 判 "parent"，装配不变）；找不到（或
   * picked 目录本身就与 stem 同名）才按整棵树遍历。成员上限只作用于
   * **真正会被装配的子树**。
   */
  async function collectCompanionFolder(dirHandle, stem, opts, deps) {
    var maxMembers = (opts && typeof opts.maxMembers === "number")
      ? opts.maxMembers : DEFAULT_MAX_MEMBERS;
    var getFile = (deps && typeof deps.getFile === "function")
      ? deps.getFile : function (h) { return h.getFile(); };
    var caps = { maxMembers: maxMembers, getFile: getFile };
    var pickedName = String((dirHandle && dirHandle.name) || "");
    if (!namesMatch(pickedName, stem) && dirHandle && dirHandle.values) {
      for await (const entry of dirHandle.values()) {
        if (entry && entry.kind === "directory" &&
            namesMatch(String(entry.name || ""), stem)) {
          var rows = [];
          await walkHandle(entry,
            pickedName ? pickedName + "/" + entry.name : entry.name,
            rows, caps);
          return rows;
        }
      }
    }
    var all = [];
    await walkHandle(dirHandle, pickedName, all, caps);
    return all;
  }

  /// webkitdirectory 回退是否可用（input type=file 的 webkitdirectory
  /// 属性）。与 supportsDirectoryPicker 一起决定「还能不能选文件夹」：
  /// 两者都没有时才提示彻底不可用并隐藏按钮。deps.doc 可注入（测试）。
  function supportsFolderPicker(deps) {
    try {
      var doc = (deps && deps.doc) ||
        (typeof document !== "undefined" ? document : null);
      if (!doc || typeof doc.createElement !== "function") return false;
      var input = doc.createElement("input");
      input.type = "file";
      return "webkitdirectory" in input;
    } catch (e) {
      return false;
    }
  }

  /// 是否支持目录选择器（Firefox/Safari/iOS 没有 → 调用方回退 webkitdirectory
  /// input 并如实说明）。
  function supportsDirectoryPicker(win) {
    var w = win || (typeof window !== "undefined" ? window : undefined);
    return !!w && typeof w.showDirectoryPicker === "function";
  }

  /**
   * pickDirectory(opts, deps) — 打开目录选择器。opts: { id, startIn }。
   * startIn 只在真的拿到 FileSystemFileHandle 时才传（拖入 .mrxs 时在
   * drop 事件内同步 getAsFileSystemHandle()，或 showOpenFilePicker 的返回）；
   * 带 startIn 抛 TypeError（句柄不可用/已失效）→ 不带它重试一次。
   * 返回：
   *   { ok: true, handle, name }
   *   { ok: false, reason: "cancelled" }   用户 Esc/取消：调用方安静保持
   *   { ok: false, reason: "unsupported" } 无 API：回退隐藏 input
   *   { ok: false, reason: "security" }    SecurityError：回退隐藏 input
   *   { ok: false, reason: "error", error }
   */
  async function pickDirectory(opts, deps) {
    opts = opts || {};
    var w = (deps && deps.win) || (typeof window !== "undefined" ? window : undefined);
    if (!w || typeof w.showDirectoryPicker !== "function") {
      return { ok: false, reason: "unsupported" };
    }
    var base = { id: String(opts.id || "mrxs-companion"), mode: "read" };
    var opened = null;
    try {
      var options = base;
      if (opts.startIn) options = Object.assign({}, base, { startIn: opts.startIn });
      opened = await w.showDirectoryPicker(options);
    } catch (e) {
      if (e && e.name === "AbortError") return { ok: false, reason: "cancelled" };
      if (opts.startIn && e && e.name === "TypeError") {
        // startIn 不认这个句柄（跨驱动器/已失效等）：不带 startIn 再试一次
        try {
          opened = await w.showDirectoryPicker(base);
        } catch (e2) {
          if (e2 && e2.name === "AbortError") {
            return { ok: false, reason: "cancelled" };
          }
          return {
            ok: false,
            reason: e2 && e2.name === "SecurityError" ? "security" : "error",
            error: e2,
          };
        }
      } else {
        return {
          ok: false,
          reason: e && e.name === "SecurityError" ? "security" : "error",
          error: e,
        };
      }
    }
    if (!opened) return { ok: false, reason: "error" };
    return { ok: true, handle: opened, name: String(opened.name || "") };
  }

  window.HP_MRXS_COMPANION = {
    stemOf: stemOf,
    normalizeName: normalizeName,
    namesMatch: namesMatch,
    detectNeedsFolder: detectNeedsFolder,
    planCompanionFolder: planCompanionFolder,
    mergeRows: mergeRows,
    collectDirectoryHandle: collectDirectoryHandle,
    collectCompanionFolder: collectCompanionFolder,
    pickDirectory: pickDirectory,
    supportsDirectoryPicker: supportsDirectoryPicker,
    supportsFolderPicker: supportsFolderPicker,
    DEFAULT_MAX_MEMBERS: DEFAULT_MAX_MEMBERS,
  };
})();
