# -*- coding: utf-8 -*-
"""W2 format_requests 并发测试（F01–F05）。

- F01：两个**独立进程**并发提交（不同用户），20 条唯一 ID、20 行——
  验证多 web worker 下 PG 无丢行（JSONL 时代的 R2 缺陷）；
- F02：同用户限流竞态——per-user 咨询锁串行化，恰好 1 成功 1 RateLimited；
- F03：两个 drain worker 竞争——每作业至多领取一次；陈旧租约回写被拒；
- F04：崩溃语义——send_started_at 空 → 回收 queued；非空 → uncertain
  封存绝不重发；
- F05：sender 未配置时排水不干扰提交，新行照常可查、无状态回退。
"""
import json
import multiprocessing
import os
import re
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import psycopg  # noqa: E402
import pytest  # noqa: E402

import format_request_store as frs  # noqa: E402
import pg_store  # noqa: E402
import registration_mail_worker as rmw  # noqa: E402

_REQUEST_ID_RE = re.compile(r"请求 ID: (fr_[0-9a-f]+)")


@pytest.fixture(autouse=True)
def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("FORMAT_REQUEST_DIR",
                       str(tmp_path / "format_requests"))
    monkeypatch.setattr(frs, "_RETRY_BACKOFF_BASE_SECONDS", 0)
    # session PG 在 0049 落盘前已启动：幂等补应用迁移
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        pg_store.ensure_schema(conn)
    finally:
        conn.close()
    rmw.install_fake_sender().clear()
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "fake")
    yield


def _count_rows():
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM format_requests")
            return cur.fetchone()[0]
    finally:
        conn.close()


def _job_row(request_id):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    conn.row_factory = psycopg.rows.dict_row
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM format_request_mail_jobs "
                        "WHERE request_id=%s", (request_id,))
            return dict(cur.fetchone())
    finally:
        conn.close()


def _expire_lease(job_id):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE format_request_mail_jobs "
                "SET lease_expires_at = now() - interval '5 seconds' "
                "WHERE job_id=%s", (job_id,))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# F01：双进程并发提交
# --------------------------------------------------------------------------- #
def _f01_child(user_id, count, barrier, out_path, database_url):
    """子进程：等屏障后连发 count 条提交，结果写 out_path。

    spawn 上下文：子进程全新解释器（干净导入链，不继承父进程线程/
    连接状态），env 只显式带入 DATABASE_URL。"""
    os.environ["DATABASE_URL"] = database_url
    os.environ.setdefault("FORMAT_REQUEST_DIR",
                          os.path.join(_bootstrap.SHARE_DATA_DIR, "frf01"))
    import format_request_store as child_frs
    try:
        barrier.wait()
    except threading.BrokenBarrierError as e:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"ids": [], "errors": ["barrier: %s" % e]}, f)
        return
    ids, errors = [], []
    for i in range(count):
        try:
            rec = child_frs.submit_request(
                user_id=user_id, format_ext=".f%02d" % i)
            ids.append(rec["id"])
        except Exception as e:  # noqa: BLE001
            errors.append("%s: %s" % (type(e).__name__, e))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"ids": ids, "errors": errors}, f)


def test_f01_two_processes_submit_20_unique(tmp_path):
    url = os.environ["DATABASE_URL"]
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2, timeout=30)
    outs = [str(tmp_path / "p1.json"), str(tmp_path / "p2.json")]
    procs = [
        ctx.Process(target=_f01_child,
                    args=("f01_user_a", 10, barrier, outs[0], url)),
        ctx.Process(target=_f01_child,
                    args=("f01_user_b", 10, barrier, outs[1], url)),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, "子进程异常退出: %s" % p.exitcode
    results = []
    for path in outs:
        with open(path, "r", encoding="utf-8") as f:
            results.append(json.load(f))
    assert results[0]["errors"] == [], results[0]["errors"]
    assert results[1]["errors"] == [], results[1]["errors"]
    ids = results[0]["ids"] + results[1]["ids"]
    assert len(ids) == 20
    assert len(set(ids)) == 20  # 全部唯一（无覆盖/丢失）
    assert _count_rows() == 20
    # 每用户恰好 10 条（限流窗口内），邮件作业一一对应
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT owner_user_id, count(*) FROM format_requests "
                "GROUP BY owner_user_id ORDER BY owner_user_id")
            counts = dict(cur.fetchall())
            cur.execute("SELECT count(*) FROM format_request_mail_jobs")
            jobs = cur.fetchone()[0]
    finally:
        conn.close()
    assert counts == {"f01_user_a": 10, "f01_user_b": 10}
    assert jobs == 20


# --------------------------------------------------------------------------- #
# F02：同用户限流竞态（咨询锁下确定性）
# --------------------------------------------------------------------------- #
def test_f02_same_user_limit_race():
    limit = 3
    for i in range(limit - 1):
        frs.submit_request(user_id="f02_racer", format_ext=".pre%d" % i,
                           daily_limit=limit)
    barrier = threading.Barrier(2, timeout=15)
    results = []

    def _submit():
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            return
        try:
            rec = frs.submit_request(user_id="f02_racer",
                                     format_ext=".race", daily_limit=limit)
            results.append(("ok", rec["id"]))
        except frs.RateLimited:
            results.append(("limited", None))
        except Exception as e:  # noqa: BLE001
            results.append(("error", "%s: %s" % (type(e).__name__, e)))

    threads = [threading.Thread(target=_submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert len(results) == 2, results
    outcomes = sorted(r[0] for r in results)
    assert outcomes == ["limited", "ok"], results  # 恰好 1 成功 1 限流
    assert frs.count_since("f02_racer", 0) == limit  # 终量 = L，不越限


# --------------------------------------------------------------------------- #
# F03：双 worker 竞争领取 + 陈旧租约拒绝
# --------------------------------------------------------------------------- #
class _RecordingSender:
    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def send(self, to, subject, body):
        with self._lock:
            self.calls.append((to, subject, body))

    def mailed_request_ids(self):
        with self._lock:
            out = []
            for _to, _subject, body in self.calls:
                m = _REQUEST_ID_RE.search(body)
                if m:
                    out.append(m.group(1))
            return out


def test_f03_two_workers_single_claim():
    ids = [frs.submit_request(user_id="f03_u%d" % i,
                              format_ext=".x%d" % i)["id"]
           for i in range(6)]
    sender = _RecordingSender()
    barrier = threading.Barrier(2, timeout=15)

    def _drain():
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            return
        try:
            frs.drain_once(limit=10, sender=sender)
        except Exception as e:  # noqa: BLE001
            sender.calls.append(("DRAIN-ERROR", str(e), ""))

    threads = [threading.Thread(target=_drain) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    mailed = sender.mailed_request_ids()
    # 每作业恰好发送一次（无重复、无遗漏）
    assert len(mailed) == 6, mailed
    assert sorted(mailed) == sorted(ids)
    for rid in ids:
        job = _job_row(rid)
        assert job["mail_status"] == "sent"
        assert job["attempts"] == 1
        assert job["lease_token"] is None

    # 陈旧/错误租约回写被拒；正确租约成功
    rid = frs.submit_request(user_id="f03_lease", format_ext=".lz")["id"]
    job = frs.claim_mail_job("f03_w1", lease_seconds=60)
    assert job is not None and job["request_id"] == rid
    assert "mail_status" not in job  # 返回快照只有发送所需的请求列 + 租约
    assert _job_row(rid)["mail_status"] == "sending"
    assert _job_row(rid)["attempts"] == 1
    assert frs.complete_mail_job(job["job_id"], "wrong-token", "sent") is False
    assert frs.mark_send_started(job["job_id"], "wrong-token") is False
    assert _job_row(rid)["mail_status"] == "sending"  # 未被错误回写改动
    assert frs.mark_send_started(job["job_id"], job["lease_token"]) is True
    assert frs.complete_mail_job(job["job_id"], job["lease_token"],
                                 "sent") is True
    assert _job_row(rid)["mail_status"] == "sent"


# --------------------------------------------------------------------------- #
# F04：崩溃语义（send_started_at 判定）
# --------------------------------------------------------------------------- #
def test_f04_crash_before_and_after_send():
    rid_before = frs.submit_request(user_id="f04_a",
                                    format_ext=".before")["id"]
    rid_after = frs.submit_request(user_id="f04_b",
                                   format_ext=".after")["id"]
    # 两个 worker 同时领取（互不冲突）
    job_before = frs.claim_mail_job("f04_w1", lease_seconds=60)
    job_after = frs.claim_mail_job("f04_w2", lease_seconds=60)
    assert {job_before["request_id"], job_after["request_id"]} == \
        {rid_before, rid_after}
    if job_before["request_id"] != rid_before:  # 领取顺序保险
        job_before, job_after = job_after, job_before

    # 崩溃于发送之前：未置 send_started_at → 回收 queued 重试
    _expire_lease(job_before["job_id"])
    reaped = frs.reap_expired_sending()
    assert reaped == {"requeued": 1, "uncertain": 0}
    assert _job_row(rid_before)["mail_status"] == "queued"

    # 崩溃于发送已开始之后：send_started_at 非空 → uncertain 封存
    assert frs.mark_send_started(job_after["job_id"],
                                 job_after["lease_token"]) is True
    _expire_lease(job_after["job_id"])
    reaped = frs.reap_expired_sending()
    assert reaped == {"requeued": 0, "uncertain": 1}
    assert _job_row(rid_after)["mail_status"] == "uncertain"

    # 排水：只补发回收的 queued；uncertain 绝不重发（多轮也不变）
    sender = rmw.install_fake_sender()
    assert frs.drain_once(sender=sender) == 1
    assert frs.drain_once(sender=sender) == 0
    assert frs.drain_once(sender=sender) == 0
    assert _job_row(rid_before)["mail_status"] == "sent"
    assert _job_row(rid_after)["mail_status"] == "uncertain"
    mailed = _RecordingSender()
    mailed.calls = list(sender.sent)
    assert mailed.mailed_request_ids() == [rid_before]


# --------------------------------------------------------------------------- #
# F05：排水（无 sender）与提交并发
# --------------------------------------------------------------------------- #
def test_f05_submit_during_drain_no_sender(monkeypatch):
    monkeypatch.setattr(rmw, "get_sender", lambda environ=None: None)
    for i in range(3):
        frs.submit_request(user_id="f05_u", format_ext=".pre%d" % i)
    drained = threading.Event()
    errors = []

    def _drain():
        try:
            frs.drain_once()  # 通道未配置 → 0 条、全部保留 queued
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        drained.set()

    t = threading.Thread(target=_drain)
    t.start()
    # 排水进行中提交新请求（无 sender → drain 立即返回，时序宽松即可）
    new_ids = []
    for i in range(2):
        new_ids.append(frs.submit_request(user_id="f05_u",
                                          format_ext=".during%d" % i)["id"])
    assert drained.wait(timeout=5)
    t.join(timeout=5)
    assert errors == []
    # 新行持久、queued 可查、无状态回退
    page = frs.list_requests(owner_user_id="f05_u", limit=100)
    assert len(page["items"]) == 5
    assert all(item["mail_status"] == "queued" for item in page["items"])
    admin = frs.admin_list_requests(limit=100)
    assert len(admin["items"]) == 5
    # 通道恢复后一次性全部排水
    assert frs.drain_once(sender=rmw.install_fake_sender()) == 5
