# -*- coding: utf-8 -*-
"""e2e 内嵌 PostgreSQL 的回收助手（postmaster 泄漏修复）。

被测应用进程（e2e_server.py，SIGTERM 路径）与回归测试
（tests/test_e2e_pg_reap.py）共用；playwright 父进程兜底
（playwright.config.ts 的 process exit handler）在
tests/e2e/stop-embedded-postgres.ts 里实现同一套同步语义。

marker 文件是子进程写给父进程（playwright exit 兜底）的「账本」：
只含 pgdata/tmp/python pid，绝不写凭据。
"""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional


def marker_path_for(port: int) -> str:
    """marker 路径：默认系统临时目录按端口命名，env E2E_PG_MARKER 可覆盖。"""
    return os.environ.get("E2E_PG_MARKER") or os.path.join(
        tempfile.gettempdir(), "pt-e2e-pg-%d.json" % port)


def write_marker(path: str, *, pgdata: str, tmp: str) -> None:
    Path(path).write_text(json.dumps({
        "pgdata": pgdata,
        "tmp": tmp,
        "pid": os.getpid(),
    }), encoding="utf-8")


def read_postmaster_pid(pgdata: str) -> Optional[int]:
    try:
        first = (Path(pgdata) / "postmaster.pid").read_text(
            encoding="utf-8").splitlines()[0].strip()
        return int(first)
    except (OSError, ValueError, IndexError):
        return None


def process_command(pid: int) -> str:
    """ps 查 pid 的完整命令行；查不到（已死/僵尸）返回空串。"""
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def pid_is_our_postgres(pid: int, pgdata: str) -> bool:
    """防 PID 复用：只认命令行里带 postgres / pgdata 路径的进程。"""
    if pid <= 1:
        return False
    cmd = process_command(pid)
    return bool(cmd) and ("postgres" in cmd or pgdata in cmd)


def _wait_gone(pid: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(0.05)
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True


def stop_postmaster(pgdata: str, timeout_s: float = 5.0) -> None:
    """按 pgdata/postmaster.pid 收割 postmaster。

    流程：校验命令行 -> SIGTERM -> 等待 -> 复验命令行 -> SIGKILL 本体 ->
    SIGKILL 整个进程组（postmaster 由 pg_ctl 守护化、自成进程组，组长死了
    残留 backend 还在，必须连组一起收；非组长时 -pid 报 ESRCH，忽略）。
    复验防等待窗口内原进程退出、PID 被无关进程复用时误杀。

    幂等且吞异常；pid 缺失/<=1/命令行对不上（PID 复用）时不动。
    """
    try:
        pid = read_postmaster_pid(pgdata)
        if pid is None or not pid_is_our_postgres(pid, pgdata):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return  # 发信号前恰已退出（含 ProcessLookupError）
        if _wait_gone(pid, timeout_s):
            return
        # 仍活着 → SIGKILL。发之前再核验命令行（防等待窗口 PID 复用）。
        if not pid_is_our_postgres(pid, pgdata):
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return
        # 连进程组一起收掉残留 backend（非组长 → ESRCH，忽略）。
        try:
            os.kill(-pid, signal.SIGKILL)
        except OSError:
            pass
    except Exception:
        return


def make_cleanup(pgdata: str, tmp: str, marker_path: str,
                 get_server: Optional[Callable[[], object]]) -> Callable[[], None]:
    """构造幂清理闭包：srv.cleanup -> stop_postmaster 兜底 -> rmtree -> 删 marker。

    atexit 注册与 finally 各调一次也只执行一遍。
    """
    done: list = []

    def _cleanup() -> None:
        if done:
            return
        done.append(True)
        try:
            srv = get_server() if get_server is not None else None
            if srv is not None:
                srv.cleanup()
        except Exception:
            pass
        stop_postmaster(pgdata)
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            Path(marker_path).unlink(missing_ok=True)
        except OSError:
            pass

    return _cleanup


def install_signal_handlers() -> None:
    """SIGTERM/SIGINT -> SystemExit(0)，让清理走正常退出路径。"""
    def _term(_signum, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)


__all__ = [
    "marker_path_for", "write_marker", "read_postmaster_pid",
    "process_command", "pid_is_our_postgres", "stop_postmaster",
    "make_cleanup", "install_signal_handlers",
]
