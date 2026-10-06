'use strict';
// 浏览器门禁共享的「原生参考结果缓存」（tests-only；C2 run_parity 与
// C3/C4 lib.nativeConvert 都走这里）。
//
// 动机：每轮全量门禁里，同样的 (CLI, 输入, 参数) 原生转换被重复跑——
// parity 对真实样本做原生参考，C3 各场景又对同一夹具再做一次原生参考。
// VMS/NDPI 这类大样本的原生转换以十分钟计，而输入与 CLI 都没变。
//
// 契约：
//   key = sha256( JSON{ cliSha256, inputFingerprint, args } )
//     - cliSha256：CLI 二进制内容的 sha256（CLI 任何重构建自动失效）；
//     - inputFingerprint：输入文件（realpath+size+mtimeNs）或目录（递归
//       全体成员的 rel+size+mtimeNs，排序后摘要；任一成员变化即失效）；
//     - args：完整参数向量（输出路径替换为占位符，避免落盘位置影响键）。
//   命中 → `.gate-tmp/native-cache/<key>/output.bin` 以 hardlink（跨设备
//   回退 copy）交给调用方，sha256 直接用存储时记下的值（不再重哈希）。
//   未命中 → 正常转换 → 哈希 → 原子写入缓存（tmp 目录 + rename）。
//
// 失效 = 键变化：CLI/输入/参数任一变化都会得到新键，旧条目自然不再命中
// （不主动删除；.gate-tmp 本就是门禁工作区）。缓存目录缺省
// `<repo>/.gate-tmp/native-cache`，可用 PT_NATIVE_CACHE 覆盖（测试用）。

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { execFileSync } = require('child_process');

const REPO = path.resolve(__dirname, '..', '..');

function cacheRoot() {
  return process.env.PT_NATIVE_CACHE
    || path.join(REPO, '.gate-tmp', 'native-cache');
}

// ---------------------------------------------------------------- digest --

function sha256FileChunks(p) {
  const h = crypto.createHash('sha256');
  const fd = fs.openSync(p, 'r');
  try {
    const buf = Buffer.alloc(4 * 2 ** 20);
    for (;;) {
      const n = fs.readSync(fd, buf, 0, buf.length, null);
      if (n === 0) break;
      h.update(buf.subarray(0, n));
    }
  } finally {
    fs.closeSync(fd);
  }
  return h.digest('hex');
}

// ----------------------------------------------------------------- key --

/// 目录指纹：全体成员（递归、含符号链接目标之外的真实文件与目录本身不
/// 计）按 rel 排序，`rel\0size\0mtimeNs` 逐行进 sha256。任何成员的增删、
/// 改名、内容/大小/mtime 变化都会改变指纹。
function dirFingerprint(dir) {
  const h = crypto.createHash('sha256');
  const walk = (abs, rel) => {
    const entries = fs.readdirSync(abs, { withFileTypes: true })
      .sort((a, b) => (a.name < b.name ? -1 : 1));
    for (const e of entries) {
      const childAbs = path.join(abs, e.name);
      const childRel = rel ? `${rel}/${e.name}` : e.name;
      if (e.isDirectory()) {
        walk(childAbs, childRel);
      } else if (e.isFile()) {
        // bigint stat：mtimeNs 只有 bigint 形态才暴露（ns 精度——同秒内
        // 的改动也能区分）
        const st = fs.statSync(childAbs, { bigint: true });
        h.update(`${childRel}\0${st.size}\0${st.mtimeNs}\n`);
      }
      // 套接字/FIFO 等奇异成员不参与（门禁输入不会出现）
    }
  };
  walk(dir, '');
  return 'd:' + h.digest('hex');
}

function inputFingerprint(input) {
  const abs = path.resolve(input);
  const st = fs.statSync(abs, { bigint: true });
  if (st.isDirectory()) return dirFingerprint(abs);
  return `f:${abs}\0${st.size}\0${st.mtimeNs}`;
}

const _cliShaMemo = new Map();

function cliSha256(cli) {
  const abs = path.resolve(cli);
  const st = fs.statSync(abs, { bigint: true });
  const memoKey = `${abs}\0${st.size}\0${st.mtimeNs}`;
  const hit = _cliShaMemo.get(memoKey);
  if (hit) return hit;
  const sha = sha256FileChunks(abs);
  _cliShaMemo.set(memoKey, sha);
  return sha;
}

function cacheKey(cli, input, args, output) {
  const argv = ['convert', input, output, ...args].map(String)
    .map((a) => (a === String(output) ? '<out>' : a));
  const material = JSON.stringify({
    cli: cliSha256(cli), input: inputFingerprint(input), args: argv,
  });
  return crypto.createHash('sha256').update(material).digest('hex');
}

// ------------------------------------------------------------- 落地/取用 --

function materialize(cachedArtifact, output) {
  fs.mkdirSync(path.dirname(output), { recursive: true });
  const link = () => fs.linkSync(cachedArtifact, output);
  try {
    link();
    return;
  } catch (e) {
    if (e.code === 'EEXIST') {
      // 输出路径已存在（同一参考产物路径被多个场景复用——如 C3 场景
      // a/f/k 共用 bf-580x300-native.tif）：删除后重链；仍失败则回退
      // copyFileSync（覆盖语义）
      try {
        fs.unlinkSync(output);
        link();
        return;
      } catch { /* fall through */ }
    } else if (e.code !== 'EXDEV' && e.code !== 'EPERM' && e.code !== 'EMLINK') {
      throw e;
    }
  }
  fs.copyFileSync(cachedArtifact, output); // 跨设备/链接受限回退
}

/// 走缓存的原生转换。`opts`：
///   cli     CLI 路径（缺省 <repo>/slide-transform-core/target/release/slide-transform）
///   input   输入（文件或目录；目录按全体成员指纹）
///   output  调用方期望的产物路径（命中时 hardlink/copy 到这里）
///   args    convert 的附加参数（如 ['--profile','bf-ome','--encoding','compact']）
///   convert 可注入的自定义转换函数（测试用）；缺省 execFileSync(cli, [...])
/// 返回 { sha256, bytes, cached, key }。同步实现：调用方（C3 lib/
/// run_parity）都在异步流程里，但保持 sync 以兼容既有 sync API。
function nativeConvertCached(opts) {
  const cli = opts.cli
    || path.join(REPO, 'slide-transform-core/target/release/slide-transform');
  const input = opts.input;
  const output = opts.output;
  const args = opts.args || [];
  if (!input || !output) throw new Error('nativeConvertCached: input/output required');
  const key = cacheKey(cli, input, args, output);
  const root = cacheRoot();
  const entryDir = path.join(root, key);
  const artifact = path.join(entryDir, 'output.bin');
  const metaPath = path.join(entryDir, 'meta.json');

  const readMeta = () => {
    try {
      const meta = JSON.parse(fs.readFileSync(metaPath, 'utf8'));
      if (typeof meta.sha256 === 'string' && fs.existsSync(artifact)
        && fs.statSync(artifact).size === meta.bytes) {
        return meta;
      }
    } catch { /* 损坏条目视作未命中 */ }
    return null;
  };

  let meta = readMeta();
  if (meta) {
    materialize(artifact, output);
    return { sha256: meta.sha256, bytes: meta.bytes, cached: true, key };
  }

  // 未命中：正常转换（写到最终位置），成功后原子入缓存
  const convert = opts.convert || ((out) => {
    execFileSync(cli, ['convert', input, out, '--overwrite', ...args],
      { stdio: ['ignore', 'ignore', 'inherit'] });
  });
  fs.mkdirSync(path.dirname(output), { recursive: true });
  convert(output, { input, args });
  const sha = sha256FileChunks(output);
  const bytes = fs.statSync(output).size;

  const tmpDir = path.join(root, `.tmp-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`);
  fs.mkdirSync(tmpDir, { recursive: true });
  try {
    const tmpArtifact = path.join(tmpDir, 'output.bin');
    // 存储侧必须 COPY（不能 hardlink）：hardlink 会让缓存工件与调用方
    // 输出文件同 inode——调用方日后原地改写/截断其输出即污染缓存。
    // 取用侧仍 hardlink（调用方 unlink 不影响缓存工件）。
    fs.copyFileSync(output, tmpArtifact);
    fs.writeFileSync(path.join(tmpDir, 'meta.json'), JSON.stringify({
      sha256: sha, bytes, storedAt: new Date().toISOString(),
      cliSha256: cliSha256(cli), input: inputFingerprint(input), args: args.map(String),
    }, null, 2));
    try {
      fs.renameSync(tmpDir, entryDir); // 原子发布；并发撞键时保留先到者
    } catch (e) {
      if (e.code !== 'ENOTEMPTY' && e.code !== 'EEXIST') throw e;
    }
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
  return { sha256: sha, bytes, cached: false, key };
}

module.exports = {
  nativeConvertCached, cacheKey, inputFingerprint, cliSha256, cacheRoot,
  sha256FileChunks,
};
