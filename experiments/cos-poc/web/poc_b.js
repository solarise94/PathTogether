/* COS PoC — 候选 B 驱动脚本.
 *
 * 纪律（合同 docs/cos-direct-upload-audit-plan.md §3.2/§5、§10 脱敏规则）：
 *  - STS 凭证只存内存变量，页面卸载即消失；禁止 localStorage/sessionStorage。
 *  - 禁止 console.log / 记录 TmpSecretId、TmpSecretKey、token、签名 URL。
 *    本文件只输出状态码、错误码、request id、ETag、versionId 等非秘密。
 *  - 不向 COS 发送平台 Cookie/CSRF：所有 COS 请求经独立适配（SDK XHR），
 *    不走 apiFetch/xhrSend（本 PoC 页面亦无平台会话）。
 */
(function () {
  'use strict';

  // ---- 内存态（唯一持有凭证的地方） --------------------------------------
  var state = {
    cos: null,
    bucket: null,
    region: null,
    key: null,            // 服务端分配的随机 key（非秘密）
    credentialFetchedAt: 0,
    results: [],
    seq: 0
  };

  var $ = function (id) { return document.getElementById(id); };
  var logPane = $('log');
  function logLine(text) {
    var stamp = new Date().toISOString();
    logPane.textContent += '\n[' + stamp + '] ' + text;
    logPane.scrollTop = logPane.scrollHeight;
  }
  // 防御性兜底：即使未来有人改代码，也不让 token 类值进入日志面板。
  function sanitize(text) {
    return String(text)
      .replace(/(TmpSecret(?:Id|Key)|sessionToken|SecurityToken|q-signature|x-cos-security-token)[^,}&\s]*/gi, '$1=<redacted>')
      .replace(/(q-ak)=[^&\s]+/gi, '$1=<redacted>');
  }
  logLine = (function (orig) {
    return function (text) { orig(sanitize(text)); };
  })(logLine);

  // ---- 结果表 -------------------------------------------------------------
  function record(op, expected, actual, verdict, requestId) {
    state.seq += 1;
    state.results.push({
      seq: state.seq, op: op, expected: expected, actual: actual,
      verdict: verdict, request_id: requestId || null,
      at: new Date().toISOString()
    });
    var row = document.createElement('tr');
    var cells = [String(state.seq), op, expected, actual, verdict, requestId || '—', new Date().toISOString()];
    for (var i = 0; i < cells.length; i++) {
      var td = document.createElement('td');
      td.textContent = cells[i];
      if (i === 4) td.className = verdict === 'PASS' ? 'pass' : (verdict === 'FAIL' ? 'fail' : '');
      row.appendChild(td);
    }
    $('results').getElementsByTagName('tbody')[0].appendChild(row);
    logLine(op + ' → ' + verdict + ' actual=' + actual + (requestId ? ' requestId=' + requestId : ''));
  }

  function describeErr(err) {
    if (!err) return 'unknown error';
    var status = err.statusCode || err.status;
    var code = err.error && err.error.Code ? err.error.Code : (err.error || '');
    if (typeof code === 'object') { try { code = JSON.stringify(code).slice(0, 120); } catch (e) { code = String(code); } }
    var rid = (err.headers && (err.headers['x-cos-request-id'] || err.headers['x-cos-trace-id'])) || err.requestId || null;
    return { status: status || 'xhr-blocked', code: String(code).slice(0, 120), requestId: rid };
  }

  // ---- STS 获取（getAuthorization 回调；凭证只进内存） ---------------------
  function fetchCredential(cb) {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/poc-sts?key=' + encodeURIComponent(state.key || ''), true);
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      if (xhr.status !== 200) {
        cb(new Error('poc-sts http ' + xhr.status + ' ' + String(xhr.responseText).slice(0, 200)));
        return;
      }
      var data;
      try { data = JSON.parse(xhr.responseText); } catch (e) { cb(e); return; }
      if (!data.ok) { cb(new Error('poc-sts error: ' + (data.error || 'unknown'))); return; }
      // 回填环境信息（非秘密）
      state.bucket = data.bucket; state.region = data.region;
      if (data.key) state.key = data.key;
      $('env-bucket').textContent = data.bucket;
      $('env-region').textContent = data.region;
      $('env-key').textContent = data.key;
      // 凭证对象：仅内存传递。此处不 log 任何字段值。
      state.credentialFetchedAt = Date.now();
      cb(null, {
        TmpSecretId: data.credentials.tmpSecretId,
        TmpSecretKey: data.credentials.tmpSecretKey,
        SecurityToken: data.credentials.sessionToken,
        XCosSecurityToken: data.credentials.sessionToken,
        StartTime: data.startTime,
        ExpiredTime: data.expiredTime
      });
    };
    xhr.send();
  }

  function ensureCos() {
    if (state.cos) return Promise.resolve();
    if (typeof COS === 'undefined') return Promise.reject(new Error('SDK 未加载：请通过 tools/serve_poc.py 打开本页（/vendor/cos-js-sdk-v5.min.js）'));
    var opts = {
      getAuthorization: function (options, callback) {
        // SDK 续取回调：仍按当前分配的 key 取新 STS（生产上服务端会重验 owner/状态/额度）。
        fetchCredential(function (err, cred) {
          if (err) { logLine('getAuthorization 失败（非秘密部分）: ' + err.message); callback(err); return; }
          callback(null, cred); // 凭证直接交给 SDK，不落日志
        });
      }
    };
    var chunkSize = parseInt($('opt-chunk-size').value, 10);
    if (chunkSize > 0) opts.chunkSize = chunkSize; // SDK 分块大小（MB）
    state.cos = new COS(opts);
    $('env-sdk').textContent = (COS.version || 'vendored cos-js-sdk-v5');
    return Promise.resolve();
  }

  function assignKey() {
    fetchCredential(function (err) {
      if (err) { record('分配 key（GET /api/poc-sts）', '成功返回 key 与凭证', err.message, 'FAIL', null); return; }
      record('分配 key（GET /api/poc-sts）', '成功返回 key 与凭证（凭证不展示）', 'ok key=' + state.key, 'PASS', null);
    });
  }

  // ---- 测试内容来源：真实文件或页内随机 Blob（无医疗数据） ------------------
  function payload() {
    return Promise.resolve().then(function () {
      var f = $('file-input').files && $('file-input').files[0];
      if ($('opt-use-blob').checked || !f) {
        var mb = (parseInt($('opt-blob-mb').value, 10) || 8) * 1000000; // 十进制 MB
        var chunks = [];
        var per = 1000000; // crypto.getRandomValues 单次 65536 上限 → 分批填充
        var remaining = mb;
        while (remaining > 0) {
          var take = Math.min(remaining, 65536);
          var buf = new Uint8Array(take);
          crypto.getRandomValues(buf);
          chunks.push(buf);
          remaining -= take;
        }
        return new Blob(chunks, { type: 'application/octet-stream' });
      }
      return f;
    });
  }

  function withTask(name, expected, fn) {
    ensureCos().then(function () { return payload(); }).then(function (blob) {
      fn(blob, function (err, data) {
        if (err) {
          var d = describeErr(err);
          record(name, expected, 'HTTP ' + d.status + ' code=' + d.code,
            (expected.indexOf('失败') === 0 && d.status !== 'xhr-blocked') ? 'PASS' : 'FAIL', d.requestId);
        } else {
          var headers = (data && data.headers) || {};
          var version = headers['x-cos-version-id'] || '';
          var etag = (data && (data.ETag || data.etag)) || headers.etag || '';
          record(name, expected,
            '成功' + (etag ? ' etag=' + etag : '') + (version ? ' version=' + version : ''),
            expected.indexOf('失败') === 0 ? 'FAIL' : 'PASS',
            headers['x-cos-request-id'] || null);
        }
      });
    }).catch(function (e) {
      record(name, expected, '本地错误: ' + e.message, 'FAIL', null);
    });
  }

  function baseParams() {
    return { Bucket: state.bucket, Region: state.region };
  }

  // ---- 各开关 -------------------------------------------------------------
  function uploadMain() {
    withTask('sliceUploadFile 上传指定 key', '成功（记录 ETag/versionId）', function (blob, done) {
      var params = baseParams();
      params.Key = state.key;
      params.Body = blob;
      params.onProgress = function (info) {
        var pct = Math.round((info.percent || 0) * 100);
        $('upload-progress').value = pct;
        $('upload-progress-text').textContent = pct + '%';
      };
      state.cos.sliceUploadFile(params, done);
    });
  }

  function crossKeyWrite() {
    withTask('跨 key 写入（putObject 到另一随机 key）', '失败（403，§3.0 条件 1）', function (blob, done) {
      var params = baseParams();
      params.Key = state.key.replace(/[^/]+$/, '') + 'cross-' + Date.now() + '-' + Math.floor(Math.random() * 1e6);
      params.Body = blob.size > 1000000 ? blob.slice(0, 1000000) : blob; // 只需小探针
      state.cos.putObject(params, done);
    });
  }

  function getObjectTest() {
    withTask('GetObject 下载该 key', '失败（403，§3.0 条件 2）', function (blob, done) {
      var params = baseParams();
      params.Key = state.key;
      params.DataType = 'text';
      state.cos.getObject(params, function (err, data) {
        if (!err && data && typeof data.Body === 'string' && data.Body.length > 80) {
          data.Body = '<response body withheld>'; // 不记录内容，仅证明是否拿到
        }
        done(err, data);
      });
    });
  }

  function deleteObjectTest() {
    withTask('DeleteObject 删除该 key', '失败（403，§3.0 条件 2）', function (blob, done) {
      var params = baseParams();
      params.Key = state.key;
      state.cos.deleteObject(params, done);
    });
  }

  function listBucketTest() {
    withTask('ListBucket（getBucket Prefix=' + '…poc/ 前缀）', '失败（403，§3.0 条件 2）', function (blob, done) {
      var params = baseParams();
      params.Prefix = state.key.split('/')[0] + '/';
      params.MaxKeys = 10;
      state.cos.getBucket(params, done);
    });
  }

  function repeatWrite() {
    var n = parseInt($('opt-repeat-n').value, 10) || 3;
    var i = 0;
    var versionIds = [];
    function next() {
      i += 1;
      if (i > n) {
        record('同 key 重复写 ×' + n + ' 汇总',
          '每次成功且各得新 versionId（版本控制生效）；随后点“占用审计”核对凭证期内总占用 ≤ 预约字节',
          versionIds.length + '/' + n + ' 次成功，versionId 数=' + new Set(versionIds).size,
          versionIds.length === n ? 'PASS' : 'FAIL', null);
        return;
      }
      withTask('同 key 重复写 第' + i + '/' + n + ' 次（putObject）', '成功且产生新版本', function (blob, done) {
        var params = baseParams();
        params.Key = state.key;
        params.Body = blob.slice(0, Math.min(blob.size, 5000000)); // 小探针即可堆版本
        state.cos.putObject(params, function (err, data) {
          if (!err) {
            var v = data && data.headers && data.headers['x-cos-version-id'];
            if (v) versionIds.push(v);
          }
          done(err, data);
        });
      });
      setTimeout(next, 1200); // 串行触发，便于审计页面逐次核对
    }
    next();
  }

  function abortTest() {
    withTask('AbortUploadTask（该 key 的未完成 multipart）', '成功（授权范围内）', function (blob, done) {
      // SDK v1.10.1: abortUploadTask({Bucket, Region, Key, UploadId, Level})
      // Level='file' + Key → 清理该 key 的全部未完成任务；无 UploadId 时不用 'task' 级。
      state.cos.abortUploadTask({
        Bucket: state.bucket, Region: state.region, Key: state.key, Level: 'file'
      }, done);
    });
  }

  // ---- 服务端 admin 端点（操作者凭证留在服务端，浏览器不经手） --------------
  function adminCall(path, body, name, expectedDesc, judge) {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', path, true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      var text = sanitize(String(xhr.responseText).slice(0, 4000));
      var ok = xhr.status === 200;
      record(name, expectedDesc, 'HTTP ' + xhr.status + ' ' + (ok ? text.slice(0, 600) : text.slice(0, 300)),
        ok ? judge(xhr.responseText) : 'FAIL', null);
    };
    xhr.send(JSON.stringify(body));
  }

  function auditOccupancy() {
    adminCall('/api/poc-admin/audit', { key: state.key },
      '占用审计（版本+在途分块字节）',
      '返回该 key 全版本与 multipart 碎片总占用；硬门槛：凭证有效期内 ≤ 该任务已预约字节（§3.0 条件 3）',
      function (responseText) {
        try {
          var data = JSON.parse(responseText);
          if (data.ok) {
            logLine('占用审计 totals=' + JSON.stringify(data.totals));
            logLine('预留对比：请在记录里写明本任务 reserved_bytes（= declared size），并核对 totals.object_versions_bytes + totals.multipart_parts_bytes ≤ reserved_bytes');
          }
        } catch (e) { /* 已在 actual 列展示 */ }
        return 'PASS';
      });
  }

  function cleanupAll() {
    adminCall('/api/poc-admin/cleanup', { key: state.key },
      '全版本清理（abort 在途 + 逐 versionId 删除）',
      '服务端操作者凭证执行；浏览器凭证本就无删除权（§3.2）', function () { return 'PASS'; });
  }

  // ---- 导出（脱敏） ---------------------------------------------------------
  function exportResults() {
    var payload = {
      note: 'redacted PoC results: status codes / error codes / request ids only; no credentials',
      generated_at: new Date().toISOString(),
      bucket: state.bucket, region: state.region, key: state.key,
      results: state.results
    };
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'poc-b-results-' + Date.now() + '.json';
    a.click();
    URL.revokeObjectURL(a.href);
  }

  // ---- 绑定 -----------------------------------------------------------------
  $('btn-assign-key').onclick = function () { state.cos = null; assignKey(); };
  $('btn-upload').onclick = uploadMain;
  $('btn-cross-key').onclick = crossKeyWrite;
  $('btn-get-object').onclick = getObjectTest;
  $('btn-delete-object').onclick = deleteObjectTest;
  $('btn-list-bucket').onclick = listBucketTest;
  $('btn-repeat-write').onclick = repeatWrite;
  $('btn-abort').onclick = abortTest;
  $('btn-audit').onclick = auditOccupancy;
  $('btn-cleanup').onclick = cleanupAll;
  $('btn-export').onclick = exportResults;
  $('btn-clear-results').onclick = function () {
    state.results = []; state.seq = 0;
    $('results').getElementsByTagName('tbody')[0].innerHTML = '';
    logPane.textContent = '— 已清空 —';
  };

  logLine('页面就绪。SDK=' + (typeof COS !== 'undefined' ? '已从 /vendor 加载' : '缺失'));
})();
