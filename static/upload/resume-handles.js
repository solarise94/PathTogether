/* =========================================================================
   刷新后免重选续传的文件句柄（upload-resilience 2026-11-10）。
   渐进增强，仅 Chromium 系浏览器：

     window.showOpenFilePicker                 「选择文件」直接拿 FileSystemFileHandle
     DataTransferItem.getAsFileSystemHandle()  拖放时同步取句柄（app.js 已取）
     FileSystemFileHandle.query/requestPermission({mode:'read'})
     IndexedDB                                句柄可结构化克隆持久化（Chromium 113+）

   Firefox / Safari 没有这套 API：本模块 supported()=false，面板保持
  「重新选择同名文件」的既有提示（不降级、不报错）。句柄只在**单文件
  直传**路径持久化（转换产物/MRXS 包不适用：上传字节不是重选的原文件）。

  语义边界：
   - 句柄按 job_id 存 IndexedDB（key = job_id），meta 记录 name/size/
     lastModified；重选后仍逐项比对，文件被修改过就拒绝（绝不把新内容
     续到旧任务上）。
   - 权限状态由浏览器决定：queryPermission 为 prompt 时才 requestPermission
     （必须在用户手势中调用——恢复行的「继续上传」点击就是手势）。
   - 存储不可用（隐私模式/配额）→ 所有操作安静失败；句柄只影响便利性，
     绝不影响上传主链路。

   形态：classic script（app.js 之前加载），无依赖；可 vitest 直测
   （_setIndexedDBFactory 注入假 IDB）。
   ========================================================================= */
(function () {
  "use strict";

  var DB_NAME = "pt-upload-handles";
  var DB_VERSION = 1;
  var STORE = "handles";
  var dbPromise = null;
  var idbFactoryOverride = null;

  function idbFactory() {
    if (idbFactoryOverride) return idbFactoryOverride;
    try {
      return (typeof indexedDB !== "undefined" && indexedDB) ? indexedDB : null;
    } catch (e) {
      return null;
    }
  }

  function supported() {
    return !!idbFactory();
  }

  function pickerSupported() {
    return typeof window !== "undefined" &&
      typeof window.showOpenFilePicker === "function";
  }

  /// 打开数据库（惰性、缓存）。失败 → null（调用方安静降级）。
  function openDb() {
    if (dbPromise) return dbPromise;
    var factory = idbFactory();
    if (!factory) return Promise.resolve(null);
    dbPromise = new Promise(function (resolve) {
      var req;
      try {
        req = factory.open(DB_NAME, DB_VERSION);
      } catch (e) {
        resolve(null);
        return;
      }
      req.onupgradeneeded = function () {
        var db = req.result;
        if (!db.objectStoreNames.contains(STORE)) {
          db.createObjectStore(STORE, { keyPath: "job_id" });
        }
      };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { resolve(null); };
      req.onblocked = function () { resolve(null); };
    });
    return dbPromise;
  }

  function withStore(mode, fn) {
    return openDb().then(function (db) {
      if (!db) return null;
      return new Promise(function (resolve) {
        var tx;
        var req;
        try {
          tx = db.transaction(STORE, mode);
          req = fn(tx.objectStore(STORE));
        } catch (e) {
          resolve(null);
          return;
        }
        var result;
        if (req && typeof req === "object") {
          req.onsuccess = function () { result = req.result; };
          req.onerror = function () { result = null; };
        }
        tx.oncomplete = function () {
          resolve(result === undefined ? null : result);
        };
        tx.onerror = function () { resolve(null); };
        tx.onabort = function () { resolve(null); };
      });
    });
  }

  /// 保存句柄（覆盖同 job_id 的旧记录）。handle 可为 Promise（拖放事件里
  /// getAsFileSystemHandle() 是异步的）——等待后写入；失败安静返回 false。
  /// meta = {filename, size, last_modified}
  function save(jobId, handle, meta) {
    if (!jobId || !handle) return Promise.resolve(false);
    return Promise.resolve(handle).then(function (h) {
      if (!h) return false;
      var rec = {
        job_id: String(jobId),
        handle: h,
        filename: String((meta && meta.filename) || ""),
        size: Number((meta && meta.size) || 0),
        last_modified: (meta && typeof meta.last_modified === "number")
          ? meta.last_modified : null,
        saved_at: Date.now(),
      };
      return withStore("readwrite", function (store) {
        store.put(rec);
      }).then(function () { return true; });
    }, function () { return false; });
  }

  function load(jobId) {
    if (!jobId) return Promise.resolve(null);
    return withStore("readonly", function (store) {
      return store.get(String(jobId));
    });
  }

  function remove(jobId) {
    if (!jobId) return Promise.resolve(false);
    return withStore("readwrite", function (store) {
      store.delete(String(jobId));
    }).then(function () { return true; });
  }

  /// 句柄 → 可上传 File（权限 + 身份三重校验：name/size/lastModified）。
  /// 返回 {file} 或 {error: "permission_denied"|"file_changed"|"unavailable"}。
  /// requestPermission 必须在用户手势内调用（行按钮点击即是）。
  function fileFromHandle(rec) {
    var h = rec && rec.handle;
    if (!h || typeof h.getFile !== "function") {
      return Promise.resolve({ error: "unavailable" });
    }
    var permP;
    try {
      permP = (typeof h.queryPermission === "function")
        ? h.queryPermission({ mode: "read" }) : "granted";
    } catch (e) {
      permP = "denied";
    }
    return Promise.resolve(permP).then(function (perm) {
      if (perm === "granted") return "granted";
      if (typeof h.requestPermission !== "function") return "denied";
      return h.requestPermission({ mode: "read" });
    }).then(function (perm) {
      if (perm !== "granted") return { error: "permission_denied" };
      return Promise.resolve(h.getFile()).then(function (file) {
        if (!file) return { error: "unavailable" };
        if (typeof rec.filename === "string" && rec.filename &&
            file.name !== rec.filename) {
          return { error: "file_changed" };
        }
        if (typeof rec.size === "number" && rec.size > 0 &&
            file.size !== rec.size) {
          return { error: "file_changed" };
        }
        if (typeof rec.last_modified === "number" &&
            typeof file.lastModified === "number" &&
            file.lastModified !== rec.last_modified) {
          return { error: "file_changed" };
        }
        return { file: file };
      }, function () { return { error: "unavailable" }; });
    }, function () { return { error: "permission_denied" }; });
  }

  /// 「选择文件」（多选）：showOpenFilePicker → {items:[{handle,file}]}。
  /// 用户取消（AbortError）→ {cancelled:true}；不支持 → {supported:false}
  /// （调用方回落隐藏 input）。**必须在点击事件同步栈内调用**（用户手势）。
  function pickFiles(opts) {
    if (!pickerSupported()) {
      return Promise.resolve({ supported: false, cancelled: false, items: [] });
    }
    var o = opts || {};
    var pickerOpts = { multiple: true, excludeAcceptAllOption: false };
    if (Array.isArray(o.types) && o.types.length) pickerOpts.types = o.types;
    var p;
    try {
      p = window.showOpenFilePicker(pickerOpts);
    } catch (e) {
      return Promise.resolve({ supported: true, cancelled: false, items: [] });
    }
    return Promise.resolve(p).then(function (handles) {
      var out = [];
      var seq = Promise.resolve();
      (handles || []).forEach(function (h) {
        seq = seq.then(function () {
          return Promise.resolve(h.getFile()).then(function (file) {
            if (file) out.push({ handle: h, file: file });
          }, function () { /* 单个句柄失败：跳过该文件 */ });
        });
      });
      return seq.then(function () {
        return { supported: true, cancelled: false, items: out };
      });
    }, function (err) {
      return {
        supported: true,
        cancelled: !!(err && err.name === "AbortError"),
        items: [],
      };
    });
  }

  //: 测试注入口（与 __PT_TEST_HOOKS 同精神；不改变生产行为）
  function setIndexedDBFactory(factory) {
    idbFactoryOverride = factory || null;
    dbPromise = null;
  }

  window.HP_RESUME_HANDLES = {
    DB_NAME: DB_NAME,
    supported: supported,
    pickerSupported: pickerSupported,
    save: save,
    load: load,
    remove: remove,
    fileFromHandle: fileFromHandle,
    pickFiles: pickFiles,
    _setIndexedDBFactory: setIndexedDBFactory,
  };
})();
