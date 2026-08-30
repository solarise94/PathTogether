# -*- coding: utf-8 -*-
"""PR0 契约夹具自洽性测试（admin-billing 方案 §13 PR0，PathTogether 部分）。

只校验 tests/fixtures/ 下的纯数据契约，不 import app、不依赖 PG / jsonschema
（仓库 requirements*.txt 未依赖 jsonschema，这里内置一个仅覆盖本 schema 所用
keyword 子集的最小 draft 2020-12 校验器；schema/夹具不变更时它与标准实现等价）：

  a) 所有样例 usage event 通过 tests/fixtures/usage_events/schema_v1.json；
     一组刻意构造的非法 body 必须被拒绝（防止校验器空转）；
  b) token 算术：非 null 时 total = hit + miss + output 且 reasoning <= output
     （镜像 ai_usage_events 的数据库 CHECK，见方案 §6.4）；
  c) DeepSeek 价格快照换算：全部 CNY×1e9 精确等于 nano 值（Decimal，禁止 float
     中转；rate 列单位 = nano-CNY / 百万 tokens，批次 A 0022 修正了 0018 的
     CNY×1000 误写），并与方案 §4 价格表逐项核对，另抽查三档 nano 值；
  d) time_band_cases 期望值与「北京时间工作日 09:00–12:00/14:00–18:00（左闭
     右开，周末 off_peak）」规则一致——用 zoneinfo 实现最小判定函数交叉验证。
     该函数仅用于夹具校验，不是生产 pricing 代码；
  e) canonical payload_hash：按 tests/fixtures/usage_events/README.md 的规则
     复算，与 README 中已验证示例的 sha256 一致（README 是 PR2 服务端实现的
     唯一依据，本测试防止两边漂移）。

运行：cd 项目根 && python3 -m pytest tests/test_usage_event_fixtures.py -q
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
USAGE_DIR = REPO_ROOT / "tests" / "fixtures" / "usage_events"
BILLING_DIR = REPO_ROOT / "tests" / "fixtures" / "billing"

SCHEMA_PATH = USAGE_DIR / "schema_v1.json"
PRICE_PATH = BILLING_DIR / "deepseek_price_snapshot_2026-08-28.json"
TIME_BAND_PATH = BILLING_DIR / "time_band_cases.json"
README_PATH = USAGE_DIR / "README.md"

TOKEN_KEYS = (
    "cache_hit_input_tokens",
    "cache_miss_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def event_files():
    return sorted(p for p in USAGE_DIR.glob("[0-9]*.json"))


# --------------------------------------------------------------------------- #
# 最小 JSON Schema 校验器（draft 2020-12 keyword 子集，仅本 schema 使用）
# --------------------------------------------------------------------------- #
def _type_ok(instance, name):
    if name == "object":
        return isinstance(instance, dict)
    if name == "array":
        return isinstance(instance, list)
    if name == "string":
        return isinstance(instance, str)
    if name == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if name == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if name == "boolean":
        return isinstance(instance, bool)
    if name == "null":
        return instance is None
    raise AssertionError("schema 使用了校验器未实现的 type: %r" % name)


def _equal(instance, expected):
    return instance == expected and isinstance(instance, bool) == isinstance(
        expected, bool
    )


def _is_rfc3339_datetime(value):
    try:
        s = value
        if s and s[-1] in "zZ":
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return False
    return dt.tzinfo is not None


def _resolve_ref(root, ref):
    assert ref.startswith("#/"), "仅支持本地指针 $ref: %r" % ref
    node = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def validate(instance, schema, root=None, path="$", errors=None):
    """返回错误列表（空列表 = 校验通过）。仅实现 schema_v1.json 用到的 keyword。"""
    if root is None:
        root = schema
    if errors is None:
        errors = []
    if schema is True:
        return errors
    if schema is False:
        errors.append("%s: schema 为 false，任何值都不合法" % path)
        return errors

    if "$ref" in schema:
        validate(instance, _resolve_ref(root, schema["$ref"]), root, path, errors)
        # 本 schema 的 $ref 均与其他 keyword 互斥使用，直接返回
        return errors

    if "type" in schema:
        names = schema["type"]
        names = [names] if isinstance(names, str) else names
        if not any(_type_ok(instance, n) for n in names):
            errors.append(
                "%s: 期望 type %s，实际 %s" % (path, names, type(instance).__name__)
            )
            return errors  # type 不匹配时后续 keyword 语义不再适用

    if "const" in schema and not _equal(instance, schema["const"]):
        errors.append("%s: 必须 == %r，实际 %r" % (path, schema["const"], instance))
    if "enum" in schema and not any(_equal(instance, v) for v in schema["enum"]):
        errors.append("%s: %r 不在 enum %r 内" % (path, instance, schema["enum"]))

    if isinstance(instance, str):
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            errors.append("%s: %r 不匹配 pattern %r" % (path, instance, schema["pattern"]))
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append("%s: 长度 %d < minLength %d" % (path, len(instance), schema["minLength"]))
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append("%s: 长度 %d > maxLength %d" % (path, len(instance), schema["maxLength"]))
        if schema.get("format") == "date-time" and not _is_rfc3339_datetime(instance):
            errors.append("%s: %r 不是带时区的 RFC3339 date-time" % (path, instance))

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append("%s: %r < minimum %r" % (path, instance, schema["minimum"]))
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append("%s: %r > maximum %r" % (path, instance, schema["maximum"]))

    if "anyOf" in schema:
        branch_errors = [
            validate(instance, sub, root, path, list()) for sub in schema["anyOf"]
        ]
        if not any(not e for e in branch_errors):
            errors.append("%s: 不满足 anyOf 任何分支" % path)
            for i, es in enumerate(branch_errors):
                errors.extend("    anyOf[%d] %s" % (i, e) for e in es[:3])

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append("%s: 缺必填字段 %r" % (path, key))
        props = schema.get("properties", {})
        addl = schema.get("additionalProperties", True)
        for key, value in instance.items():
            child = "%s.%s" % (path, key)
            if key in props:
                validate(value, props[key], root, child, errors)
            elif addl is False:
                errors.append("%s: 不允许的额外字段（additionalProperties:false）" % child)
            elif addl is not True:
                validate(value, addl, root, child, errors)
        if "propertyNames" in schema:
            for key in instance:
                name_errors = validate(key, schema["propertyNames"], root, path + "<key>", list())
                if name_errors:
                    errors.append("%s: 字段名 %r 不合法（propertyNames）" % (path, key))

    if isinstance(instance, list):
        if "items" in schema:
            for i, item in enumerate(instance):
                validate(item, schema["items"], root, "%s[%d]" % (path, i), errors)

    return errors


# --------------------------------------------------------------------------- #
# (a) schema 与样例事件
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def schema():
    return _load(SCHEMA_PATH)


@pytest.fixture(scope="module")
def events():
    return {p.name: _load(p) for p in event_files()}


def test_schema_metadata():
    schema_metadata = _load(SCHEMA_PATH)
    assert schema_metadata["$schema"] == (
        "https://json-schema.org/draft/2020-12/schema"
    )
    assert schema_metadata["properties"]["schema_version"]["const"] == 1
    assert schema_metadata["additionalProperties"] is False
    # 五个 token 字段必须显式出现（required），且都允许 null（中断 unpriced）
    for key in TOKEN_KEYS:
        assert key in schema_metadata["required"]
        assert schema_metadata["properties"][key]["$ref"] == "#/$defs/token_count"
    token_def = schema_metadata["$defs"]["token_count"]
    assert [b.get("type") for b in token_def["anyOf"]] == ["null", "integer"]
    assert token_def["anyOf"][1]["minimum"] == 0
    # raw_usage 必须用 propertyNames/additionalProperties 收紧
    raw = schema_metadata["properties"]["raw_usage"]
    assert "propertyNames" in raw and "additionalProperties" in raw


def test_at_least_six_sample_events_exist():
    assert len(event_files()) >= 6


def test_sample_events_conform_to_schema(schema, events):
    assert events
    for name, event in events.items():
        errors = validate(event, schema)
        assert not errors, "%s 未通过 schema 校验:\n%s" % (name, "\n".join(errors))


def test_required_scenarios_covered(events):
    by_name = events
    assert "01_owner_priced_flash_peak.json" in by_name
    assert by_name["02_user_priced_pro_offpeak_reasoning.json"]["reasoning_tokens"] > 0
    assert by_name["03_user_priced_vision_exp_peak.json"]["model"] == (
        "deepseek-v4-flash-vision-exp"
    )
    aborted = by_name["04_owner_interrupted_no_usage.json"]
    assert all(aborted[k] is None for k in TOKEN_KEYS)
    assert aborted["raw_usage"]["finish_reason"] == "aborted"
    assert by_name["05_demo_subject_offpeak.json"]["subject_type"] == "demo"
    assert "provider_request_id" not in by_name[
        "06_user_priced_flash_no_provider_request_id.json"
    ]
    # 07 与 01 同 event_id（幂等冲突重放样本）
    assert (
        by_name["07_replay_conflict_of_01.json"]["event_id"]
        == by_name["01_owner_priced_flash_peak.json"]["event_id"]
    )


def test_schema_rejects_invalid_bodies(schema):
    base = json.loads(json.dumps(_load(USAGE_DIR / "01_owner_priced_flash_peak.json")))
    assert not validate(base, schema)  # 基线必须通过，否则下面的拒绝断言无意义

    def bad(mutate):
        body = json.loads(json.dumps(base))
        mutate(body)
        return validate(body, schema)

    cases = {
        "event_id 前缀错误": lambda b: b.update(event_id="evt_" + "0" * 32),
        "call_id 长度错误": lambda b: b.update(call_id="call_" + "0" * 31),
        "event_id 大写 hex": lambda b: b.update(event_id="use_" + "A" * 32),
        "schema_version bump 未协商": lambda b: b.update(schema_version=2),
        "subject_type 非法": lambda b: b.update(subject_type="guest"),
        "occurred_at 缺时区": lambda b: b.update(occurred_at="2026-09-07T02:30:12"),
        "occurred_at 非法日期": lambda b: b.update(occurred_at="2026-13-07T02:30:12Z"),
        "token 字段缺省": lambda b: b.pop("total_tokens"),
        "token 字符串化": lambda b: b.update(total_tokens="4631"),
        "token 负数": lambda b: b.update(output_tokens=-1),
        "token 布尔": lambda b: b.update(output_tokens=True),
        "额外字段 _description": lambda b: b.update(_description="inline note"),
        "provider_request_id 整数": lambda b: b.update(provider_request_id=12345),
        "raw_usage 携带长文本（疑似 prompt 泄漏）": lambda b: b["raw_usage"].update(
            leaked_prompt="x" * 200
        ),
        "raw_usage 携带数组": lambda b: b.update(raw_usage=["stop", 4274]),
        "raw_usage 元数据缺 meta_version": lambda b: b["raw_usage"].update(
            provider_meta_v2={"service": "deepseek"}
        ),
        "raw_usage 字段名大写": lambda b: b["raw_usage"].update(FinishReason="stop"),
    }
    for label, mutate in cases.items():
        errors = bad(mutate)
        assert errors, "schema 应拒绝：%s" % label


# --------------------------------------------------------------------------- #
# (b) token 算术（镜像 ai_usage_events 数据库 CHECK）
# --------------------------------------------------------------------------- #
def test_token_arithmetic_invariants(events):
    for name, event in events.items():
        hit, miss, out, reasoning, total = (event[k] for k in TOKEN_KEYS)
        if total is not None:
            assert hit is not None, name
            assert miss is not None, name
            assert out is not None, name
            assert total == hit + miss + out, "%s: total != hit+miss+output" % name
        if reasoning is not None:
            assert out is not None, name
            assert reasoning <= out, "%s: reasoning > output" % name
        # 约定：样例里要么全 null（unpriced 中断），要么全部非 null
        nulls = [event[k] is None for k in TOKEN_KEYS]
        assert all(nulls) or not any(nulls), (
            "%s: token 字段必须全 null（中断）或全部有值" % name
        )


# --------------------------------------------------------------------------- #
# (c) DeepSeek 价格快照换算
# --------------------------------------------------------------------------- #
# docs/admin-billing-plugin-implementation-plan.md §4 价格表（CNY/百万 tokens）
DOC_TABLE = {
    "deepseek-v4-flash": {
        "off_peak": ("0.05", "1.5", "4.5"),
        "peak": ("0.10", "3.0", "9.0"),
    },
    "deepseek-v4-pro": {
        "off_peak": ("0.15", "4.5", "13.5"),
        "peak": ("0.30", "9.0", "27.0"),
    },
    "deepseek-v4-flash-vision-exp": {
        "off_peak": ("0.05", "1.5", "4.5"),
        "peak": ("0.10", "3.0", "9.0"),
    },
}
_CNY_KEYS = ("cache_hit_cny_per_million", "cache_miss_cny_per_million", "output_cny_per_million")
_NANO_KEYS = ("cache_hit_nano_per_million", "cache_miss_nano_per_million", "output_nano_per_million")


def _load_price_snapshot():
    # parse_float=Decimal：精确十进制，避免 0.05 之类先变 binary float
    return json.loads(
        PRICE_PATH.read_text(encoding="utf-8"), parse_float=Decimal, parse_int=int
    )


def test_price_snapshot_matches_plan_doc_table():
    snap = _load_price_snapshot()
    assert snap["provider"] == "deepseek"
    assert snap["currency"] == "CNY"
    assert snap["snapshot_date"] == "2026-08-28"
    models = snap["models"]
    assert set(models) == set(DOC_TABLE)
    for model, bands in DOC_TABLE.items():
        assert set(models[model]) == set(bands), model
        for band, cny_values in bands.items():
            for key, expected in zip(_CNY_KEYS, cny_values):
                assert models[model][band][key] == Decimal(expected), (
                    "%s/%s/%s 与方案 §4 价格表不一致" % (model, band, key)
                )


def test_price_snapshot_nano_conversion_exact():
    snap = _load_price_snapshot()
    # 换算公式（§P0-1 批次 A）：rate 列单位是 nano-CNY / 百万 tokens，
    # nano_per_million = CNY × 1e9（1 CNY = 1e9 nano）。独立十进制推导，
    # 不从迁移/夹具复制常量自证。
    one_cny_nano = Decimal(1_000_000_000)
    for model, bands in snap["models"].items():
        for band, rates in bands.items():
            for cny_key, nano_key in zip(_CNY_KEYS, _NANO_KEYS):
                cny, nano = rates[cny_key], rates[nano_key]
                assert isinstance(nano, int), "%s/%s/%s 必须是整数" % (model, band, nano_key)
                assert cny * one_cny_nano == Decimal(nano), (
                    "%s/%s/%s 换算错误：%s×1e9 != %s" % (model, band, nano_key, cny, nano)
                )
                # 量级护栏：禁止退回 0018 的 CNY×1000（每 token nano）误写
                assert nano >= 50_000_000, (
                    "%s/%s/%s 疑似 legacy 错误量级（CNY×1000）" % (model, band, nano_key)
                )


def test_price_snapshot_spot_checks():
    snap = _load_price_snapshot()
    models = snap["models"]
    # 三档抽查（§P0-1 表 + 最高价档 + 常规 miss 档；独立手算值）
    # 0.05 CNY → 0.05×1e9 = 50,000,000 nano/百万 tokens
    assert models["deepseek-v4-flash"]["off_peak"]["cache_hit_nano_per_million"] == 50_000_000
    # 27.0 CNY → 27,000,000,000
    assert models["deepseek-v4-pro"]["peak"]["output_nano_per_million"] == 27_000_000_000
    # 3.0 CNY → 3,000,000,000
    assert models["deepseek-v4-flash-vision-exp"]["peak"]["cache_miss_nano_per_million"] == 3_000_000_000


# --------------------------------------------------------------------------- #
# (d) time_band 边界用例交叉验证（最小判定函数，仅夹具校验用）
# --------------------------------------------------------------------------- #
_BEIJING = ZoneInfo("Asia/Shanghai")


def _judge_time_band(iso):
    """按 rule_text 实现的最小判定：北京时间工作日 [09:00,12:00) ∪ [14:00,18:00)。"""
    s = iso[:-1] + "+00:00" if iso[-1] in "zZ" else iso
    local = datetime.fromisoformat(s).astimezone(_BEIJING)
    if local.weekday() >= 5:  # 周六/周日全天 off_peak
        return "off_peak"
    secs = local.hour * 3600 + local.minute * 60 + local.second
    if 9 * 3600 <= secs < 12 * 3600 or 14 * 3600 <= secs < 18 * 3600:
        return "peak"
    return "off_peak"


def test_time_band_cases_agree_with_rule():
    data = _load(TIME_BAND_PATH)
    cases = data["cases"]
    assert len(cases) >= 20
    inputs = {c["input"] for c in cases}
    assert any(c["input"].endswith("Z") for c in cases), "缺 UTC 输入用例"
    assert any(c["input"].endswith("+08:00") for c in cases), "缺 +08:00 输入用例"
    assert any(
        c["input"].endswith(("+09:00", "-05:00")) for c in cases
    ), "缺其他偏移输入用例"
    assert len(inputs) == len(cases), "存在重复 input 用例"
    for case in cases:
        got = _judge_time_band(case["input"])
        assert got == case["expected_time_band"], (
            "%s 期望 %s，规则判定 %s（%s）"
            % (case["input"], case["expected_time_band"], got, case.get("note", ""))
        )
    # 关键边界必须逐条在场（方案 §14.1 必测矩阵）
    expected_boundary_bands = {
        "2026-09-07T08:59:59+08:00": "off_peak",
        "2026-09-07T09:00:00+08:00": "peak",
        "2026-09-07T11:59:59+08:00": "peak",
        "2026-09-07T12:00:00+08:00": "off_peak",
        "2026-09-07T13:59:59+08:00": "off_peak",
        "2026-09-07T14:00:00+08:00": "peak",
        "2026-09-07T17:59:59+08:00": "peak",
        "2026-09-07T18:00:00+08:00": "off_peak",
    }
    by_input = {c["input"]: c["expected_time_band"] for c in cases}
    for key, band in expected_boundary_bands.items():
        assert by_input.get(key) == band, "缺边界用例 %s → %s" % (key, band)


def test_time_band_rule_text_documents_the_rule():
    data = _load(TIME_BAND_PATH)
    rule = data["rule_text"]
    for token in ("Asia/Shanghai", "工作日", "09:00", "12:00", "14:00", "18:00", "off_peak", "peak"):
        assert token in rule, "rule_text 缺少规则要素 %r" % token
    snap = _load_price_snapshot()
    assert "09:00" in snap["schedule_rule_text"] and "18:00" in snap["schedule_rule_text"]
    assert snap["timezone"] == "Asia/Shanghai" == data["timezone"]


# --------------------------------------------------------------------------- #
# (e) canonical payload_hash（tests/fixtures/usage_events/README.md 为唯一依据）
# --------------------------------------------------------------------------- #
CANONICAL_FIELDS = (
    "cache_hit_input_tokens",
    "cache_miss_input_tokens",
    "call_id",
    "enqueued_at",
    "event_id",
    "model",
    "occurred_at",
    "output_tokens",
    "provider",
    "provider_request_id",
    "reasoning_tokens",
    "request_id",
    "schema_version",
    "session_id",
    "subject_id",
    "subject_type",
    "total_tokens",
    "user_id",
)


def _canonical_time(value):
    if value is None:
        return None
    s = value[:-1] + "+00:00" if value[-1] in "zZ" else value
    dt = datetime.fromisoformat(s).astimezone(timezone.utc)
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_json(event):
    obj = {}
    for key in CANONICAL_FIELDS:
        if key in ("occurred_at", "enqueued_at"):
            obj[key] = _canonical_time(event.get(key))
        else:
            obj[key] = event.get(key)  # 缺省可选键 → null
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def payload_hash(event):
    return hashlib.sha256(canonical_json(event).encode("utf-8")).hexdigest()


def test_canonical_field_set_is_fixed_18_fields():
    assert len(CANONICAL_FIELDS) == 18
    assert len(set(CANONICAL_FIELDS)) == 18
    # 排除项不得混入
    for banned in ("received_at", "raw_usage"):
        assert banned not in CANONICAL_FIELDS


def test_canonical_payload_hash_matches_readme(events):
    readme = README_PATH.read_text(encoding="utf-8")
    match = re.search(r"payload_hash = ([0-9a-f]{64})", readme)
    assert match, "README 缺少已验证的 payload_hash 示例"
    documented = match.group(1)
    computed = payload_hash(events["01_owner_priced_flash_peak.json"])
    assert computed == documented, "canonical 规则与 README 示例漂移： %s != %s" % (
        computed,
        documented,
    )
    # README 中记录的 07 冲突 hash 也必须一致
    hashes = re.findall(r"`([0-9a-f]{64})`", readme)
    conflict = payload_hash(events["07_replay_conflict_of_01.json"])
    assert conflict in hashes, "README 应记录 07 的冲突 hash"
    assert conflict != computed


def test_canonical_hash_exclusion_and_normalization(events):
    base = events["01_owner_priced_flash_peak.json"]
    baseline = payload_hash(base)

    # 1) raw_usage 不参与哈希：增删诊断键都不改变 hash
    mutated = json.loads(json.dumps(base))
    mutated["raw_usage"]["prompt_tokens"] = 999999
    mutated["raw_usage"]["provider_meta_v1"]["stream"] = False
    assert payload_hash(mutated) == baseline

    # 2) 缺省与显式 null 等价（可选键 provider_request_id）；与真实值必然不同
    mutated = json.loads(json.dumps(base))
    del mutated["provider_request_id"]
    explicit_null = json.loads(json.dumps(base))
    explicit_null["provider_request_id"] = None
    assert payload_hash(mutated) == payload_hash(explicit_null)
    assert payload_hash(mutated) != baseline

    # 3) 时间规范化：Z / +00:00 / 等价 +08:00 偏移输入 → 同一 hash
    mutated = json.loads(json.dumps(base))
    mutated["occurred_at"] = "2026-09-07T04:30:12.345000+02:00"
    mutated["enqueued_at"] = "2026-09-07t02:30:13.12z"  # 小写 t/z + 2 位小数
    assert payload_hash(mutated) == baseline

    # 4) 任何账单语义字段变化都必须改变 hash（07 冲突样本）
    assert payload_hash(events["07_replay_conflict_of_01.json"]) != baseline

    # 5) 整数保持整数：schema_version 不能被字符串化
    assert '"schema_version":1' in canonical_json(base)
