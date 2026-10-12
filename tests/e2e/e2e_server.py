# -*- coding: utf-8 -*-
"""管理工作台 Playwright E2E 的被测应用进程（一次性修复包 F，§10.1）。

由 playwright.config.ts 的 webServer 拉起；职责：
  - 临时数据目录 + 内嵌 PostgreSQL（pgserver，同 conftest 的隔离策略）；
  - 仓库内 admin bundle + 仓库 source-policy pin（启动引导自动建 installation 行）；
  - 一次性 owner（BOOTSTRAP_OWNER_*）与普通用户——凭据只在进程内存与
    E2E_CREDS_FILE 指定文件中，绝不写 stdout/日志/artifact；
  - 本地 HTTP origin 起 Flask（「公网 HTTPS、内部 HTTP」的 scheme 分离由
    Python CSP 回归测试覆盖；部署后的公网冒烟负责真实 TLS 链）。

运行：python3 tests/e2e/e2e_server.py --port 8907
"""
import argparse
import atexit
import json
import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pg_reap  # noqa: E402  (path 就绪后再 import，不依赖脚本目录恰为 sys.path[0])


# --------------------------------------------------------------------------- #
# HistoPilot 桩插件（CI 无私仓 bundle 时保证 AI 面板 E2E 有意义而非跳过）
# --------------------------------------------------------------------------- #
# 拖动/钳制/位置持久化交互全在宿主侧（app.js 面板控制器 + ai-panel-chrome.js），
# 插件侧只负责面板显隐。桩只需：
#   - 目录存在 → histopilot_ui_enabled() 为真 → #ai-btn 与 #ai-panel 随壳渲染；
#   - ui/*.js 可经 /plugins/histopilot/ui/* 服务（index.html 按 flag 引用全套）；
#   - main.js 桥接 panel.toggle 与 #ai-panel-close：显隐面板并广播
#     panel.stateChanged（宿主据此互斥标注面板）。
# 真实 bundle（integrations/pathtogether 解压到 plugins/histopilot 或
# PLUGIN_BUNDLES_DIR）存在时不装桩——AI 业务链路仍由真实 bundle 覆盖。
_HISTOPILOT_STUB_MANIFEST = {
    "manifestSchemaVersion": "1.0.0",
    "id": "histopilot",
    "name": "HistoPilot (e2e stub)",
    "pluginVersion": "0.0.0-e2e",
    "pluginContractVersion": "1.0.0",
    "bridgeProtocolVersion": "1.0.0",
    "ui": {
        "entry": "/plugins/histopilot/ui/main.js",
        "slots": ["viewer.right-panel"],
    },
    "service": {"baseUrl": "/", "health": "/healthz"},
    "permissions": [],
}

# bridge-client.js 建命名空间（HostBridge 同窗口插件端最小实现）；其余文件为
# 占位，保持与真实 bundle 相同的加载面（缺失仅 404，但补齐免控制台噪音）。
_HISTOPILOT_STUB_UI_FILES = {
    "bridge-client.js": """\
/* e2e 桩：HostBridge 插件端最小实现（同窗口传输；只服务 panel.toggle）。 */
(function () {
  "use strict";
  if (window.HistoPilot && window.HistoPilot._stub) return;
  var reqHandlers = {};
  var evtHandlers = {};
  function reply(env, ok, body) {
    var host = window.HostBridgeHost;
    if (!host) return;
    var out = {
      kind: "response", protocolVersion: env.protocolVersion,
      pluginInstallationId: env.pluginInstallationId || "histopilot",
      requestId: env.requestId, ok: ok,
    };
    if (ok) { out.result = body == null ? null : body; }
    else { out.error = body; }
    host._receiveFromPlugin(out);
  }
  window.HistoPilot = {
    _stub: true,
    onRequest: function (method, fn) { reqHandlers[method] = fn; },
    onEvent: function (type, fn) { evtHandlers[type] = fn; },
    bridge: {
      emit: function (type, payload) {
        var host = window.HostBridgeHost;
        if (!host) return;
        host._receiveFromPlugin({
          kind: "event", protocolVersion: host.PROTO,
          pluginInstallationId: "histopilot",
          eventId: "stub_evt_" + Date.now() + "_" + Math.random().toString(36).slice(2),
          type: type, payload: payload == null ? {} : payload,
        });
      },
    },
    _onHostMessage: function (env) {
      if (!env) return;
      if (env.kind === "event") {
        var fn = evtHandlers[env.type];
        try { if (fn) fn(env.payload || {}); } catch (e) {}
        return;
      }
      if (env.kind !== "request") return;  // 桩不向 host 发请求，无 pending
      var handler = reqHandlers[env.method];
      Promise.resolve().then(function () {
        if (!handler) {
          throw { code: "unknown_method", message: "stub 未实现 " + env.method };
        }
        return handler(env.payload || {}, env);
      }).then(function (result) { reply(env, true, result); },
             function (err) {
               reply(env, false, (err && err.code) ? err
                 : { code: "stub_error", message: String((err && err.message) || err) });
             });
    },
  };
})();
""",
    "main.js": """\
/* e2e 桩：panel.toggle 与 #ai-panel-close 只做面板显隐（几何/持久化在宿主）。 */
(function () {
  "use strict";
  var HP = window.HistoPilot;
  if (!HP || !HP._stub) return;
  var isOpen = false;
  function setOpen(next) {
    var panel = document.getElementById("ai-panel");
    if (!panel) return;
    isOpen = !!next;
    panel.style.display = isOpen ? "flex" : "none";
    HP.bridge.emit("panel.stateChanged", { open: isOpen });
  }
  HP.onRequest("panel.toggle", function (payload) {
    var desired = payload && payload.open;
    if (desired === true) setOpen(true);
    else if (desired === false) setOpen(false);
    else setOpen(!isOpen);
    return { open: isOpen };
  });
  document.addEventListener("click", function (e) {
    if (e.target && e.target.closest && e.target.closest("#ai-panel-close")) {
      setOpen(false);
    }
  });
})();
""",
}
_STUB_NOOP_COMMENT = (
    "/* e2e 桩：占位文件（真实 bundle 子系统不在 CI；拖动 spec 不触达）。 */\n"
    "(function () { \"use strict\"; })();\n")


def _install_histopilot_stub(plugin_dir):
    """把桩 bundle 写入 plugin_dir（临时目录，随 e2e 进程清理）。"""
    ui_dir = plugin_dir / "ui"
    ui_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "manifest.json").write_text(
        json.dumps(_HISTOPILOT_STUB_MANIFEST, ensure_ascii=False, indent=2),
        encoding="utf-8")
    for name, body in _HISTOPILOT_STUB_UI_FILES.items():
        (ui_dir / name).write_text(body, encoding="utf-8")
    for name in ("api.js", "sse.js", "renderer.js",
                 "config-panel.js", "sessions.js"):
        (ui_dir / name).write_text(_STUB_NOOP_COMMENT, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8907)
    # 分享服务（share_server.py，独立 Flask）第二端口；缺省 = 主端口 + 1
    parser.add_argument("--share-port", type=int, default=None)
    args = parser.parse_args()
    share_port = args.share_port or (args.port + 1)

    tmp = tempfile.mkdtemp(prefix="pt-e2e-app-")
    os.environ["SHARE_DATA_DIR"] = os.path.join(tmp, "share-data")
    os.environ["UPLOAD_DIR"] = os.path.join(tmp, "uploads")
    os.makedirs(os.environ["SHARE_DATA_DIR"], exist_ok=True)
    os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)
    # E2E 上传目录在 /tmp（tmpfs 常小于 upload_guard 默认 20 GiB 磁盘保留
    # 水位）——测试环境把水位降到 16 MiB，只影响本进程内的守卫阈值
    #（upload_guard 在 import 期读 env；真实默认值语义不受影响）。
    os.environ.setdefault("UPLOAD_RESERVED_FREE_BYTES", str(16 * 1024 * 1024))
    os.environ.setdefault("AI_SIDECAR_URL", "http://127.0.0.1:8055")

    # 内嵌 PostgreSQL（隔离实例，不碰任何本机库）
    import pgserver
    import psycopg
    import pg_store
    pgdata = os.path.join(tmp, "pgdata")
    srv = pgserver.get_server(pgdata)
    os.environ["DATABASE_URL"] = srv.get_uri()
    os.environ["STORAGE_BACKEND"] = "postgres"

    # postmaster 是守护进程（独立进程组），webServer 的 SIGKILL 打不到它；
    # 写 marker 给父进程（playwright.config.ts 的 exit 兜底）收割用，不含凭据。
    marker = pg_reap.marker_path_for(args.port)
    pg_reap.write_marker(marker, pgdata=pgdata, tmp=tmp)
    cleanup = pg_reap.make_cleanup(pgdata, tmp, marker, lambda: srv)
    atexit.register(cleanup)
    # SIGTERM -> 正常退出路径，保证 finally/atexit 里的清理执行
    pg_reap.install_signal_handlers()

    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        pg_store.ensure_schema(conn)
    finally:
        conn.close()

    # 一次性凭据：只进进程内存 + 指定文件（默认系统临时目录内、带端口名）
    owner_pw = secrets.token_urlsafe(24)
    user_pw = secrets.token_urlsafe(24)
    pw_file = Path(tmp) / "bootstrap-owner-pw"
    pw_file.write_text(owner_pw, encoding="utf-8")
    pw_file.chmod(0o600)
    os.environ["BOOTSTRAP_OWNER_LOGIN_ID"] = "e2e-owner@pt.test"
    os.environ["BOOTSTRAP_OWNER_PASSWORD_FILE"] = str(pw_file)
    os.environ["REQUIRE_ADMIN_AUTH"] = "1"
    # 生产判定（TESTING/debug 均关）下 CSP 必须有规范公网 origin；本地 E2E
    # 用 http origin（本地 HTTP 合法，公网强制 https 由 Python CSP 测试覆盖）
    os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:%d" % args.port
    # 分享 URL 指向本进程第二端口的 share_server（SHARE_BASE_URL 在 app.py
    # 模块作用域读取，必须先于 import app 设置）。
    os.environ["SHARE_BASE_URL"] = "http://127.0.0.1:%d" % share_port

    # Fixed offline capabilities, independent of the caller's developer credentials.
    os.environ.pop("BAIDU_SHARE_SECRET_KEY", None)
    os.environ["FORMAT_REQUEST_INLINE_DRAIN"] = "0"
    # openslide stub + 数据目录幂等初始化（与 pytest 会话同一套引导）
    import _bootstrap  # noqa: F401
    import app as app_mod  # 启动期自动：owner 首建 + admin 插件 installation 引导

    # HistoPilot 桩（CI 无私仓 bundle）：让 AI 面板 E2E 有意义而非整体 404；
    # 真实 bundle 已装时 _plugin_dir 命中，绝不覆盖（见 _HISTOPILOT_STUB_ 注释）。
    # e2e 进程内恒开兼容 UI（开关语义由 Python 单测覆盖）。
    os.environ.pop("HISTOPILOT_UI_ENABLED", None)
    tmp_root = Path(tmp).resolve()
    bundles_root = Path(app_mod.PLUGIN_BUNDLES_DIR).resolve()
    if tmp_root != bundles_root and tmp_root not in bundles_root.parents:
        print("e2e: PLUGIN_BUNDLES_DIR=%s 不在临时目录 %s 内，不装 HistoPilot 桩"
              % (bundles_root, tmp_root), file=sys.stderr)
    elif app_mod._plugin_dir("histopilot") is None:
        _install_histopilot_stub(bundles_root / "histopilot")

    # R3 Wave1-Money 单轨：注册用户授权面恒为一次性总额度（无 target 种
    # 子行——user_spend_target 键已随 0032 删除）；10d/10e/10f（抽屉
    # CAS/邀请模板）验收总额度形态，故只种子：全局默认总额度 50 CNY
    # （version=1 首写），预建普通用户经建号组合原语自动获得默认额度行。
    # R6（2026-09-19）：管理台「新建用户」表单退役——10d 不再经 UI 建号，
    # 限额户改为种子直建（3.5 CNY 初始总额度，与原 10d 面值一致）。
    import spend_store
    spend_store.set_total_default(50_000_000_000, 1, updated_by="e2e-seed")

    import user_store_pg
    # 返回 (public_user_dict, allowance)——取 dict（含 user_id）供切片种子
    e2e_user, _ = user_store_pg.create_user_with_total_allowance(
        "e2e-user@pt.test", user_pw, display_name="E2E 普通用户")
    user_store_pg.create_user_with_total_allowance(
        "e2e-limited@pt.test", secrets.token_urlsafe(24),
        display_name="E2E 限额用户", total_limit_nano_cny=3_500_000_000)

    from seed_assets import seed_viewer_assets
    raster_slides = seed_viewer_assets(e2e_user["user_id"])

    creds_path = os.environ.get("E2E_CREDS_FILE") or os.path.join(
        tempfile.gettempdir(), "pt-e2e-creds-%d.json" % args.port)
    Path(creds_path).write_text(json.dumps({
        "baseUrl": "http://127.0.0.1:%d" % args.port,
        "shareBaseUrl": "http://127.0.0.1:%d" % share_port,
        "rasterSlides": raster_slides,
        "ownerLogin": "e2e-owner@pt.test",
        "ownerPassword": owner_pw,
        "userLogin": "e2e-user@pt.test",
        "userPassword": user_pw,
    }), encoding="utf-8")

    # 分享服务（raster-image-compat E2E 起接入）：真实 share_server.app 在
    # 第二端口以 daemon 线程运行，与主站共享 UPLOAD_DIR / SHARE_DATA_DIR /
    # DATABASE_URL（既有 PG 与临时目录清理纪律不变——daemon 线程随进程
    # 退出结束，finally/atexit 清理路径不受影响）。
    import share_server
    threading.Thread(
        target=lambda: share_server.app.run(
            host="127.0.0.1", port=share_port, threaded=True),
        name="e2e-share-server", daemon=True).start()

    try:
        app_mod.app.run(host="127.0.0.1", port=args.port, threaded=True)
    finally:
        cleanup()
        atexit.unregister(cleanup)


if __name__ == "__main__":
    main()
