"""Shared PoC configuration (candidate A and B).

All credential values are read **only** from environment variables. Nothing in
this module ever prints a SecretId / SecretKey / STS token / signed URL.

Contract references:
- docs/cos-direct-upload-audit-plan.md §10 ("PoC 环境边界与执行记录") — the
  `poc/` prefix isolates object paths; bucket/region/PoC credentials are
  external inputs recorded as `blocked_external_input` until the operator
  provides them.
- docs/upload-routing-open-source-review.md §0 D3 — byte sizes are decimal
  integers everywhere (300 MB == 300000000).
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from typing import List, Optional

ENV_SECRET_ID = "COS_POC_SECRET_ID"
ENV_SECRET_KEY = "COS_POC_SECRET_KEY"
ENV_BUCKET = "COS_POC_BUCKET"
ENV_REGION = "COS_POC_REGION"
ENV_PREFIX = "COS_POC_PREFIX"
ENV_PROD_ORIGIN = "COS_POC_PROD_ORIGIN"

DEFAULT_PREFIX = "poc/"

# Operator-side admin token for serve_poc.py dev endpoints (optional guard on
# top of the 127.0.0.1-only bind). Not a Tencent Cloud credential.
ENV_ADMIN_TOKEN = "COS_POC_ADMIN_TOKEN"

# 十进制字节常量（合同 §1 / §6.1；禁止 1024 进制换算）。
POOL_CAPACITY_BYTES = 10_000_000_000
POOL_SAFETY_BYTES = 500_000_000
POOL_ADMISSION_BYTES = POOL_CAPACITY_BYTES - POOL_SAFETY_BYTES  # 9_500_000_000

# PoC 必测尺寸（十进制）：300 MB / 500 MB；条件追加 2 GB；边界 ~9.5 GB。
SMOKE_SIZE_BYTES = 1_048_576  # 1 MiB 冒烟粒度：仅本地验证脚本可用性，不是合同测试尺寸

_BUCKET_RE = re.compile(r"^[a-z0-9-]{1,60}-[0-9]{6,12}$")  # name-appid
_REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+(-[0-9]+)?$")  # ap-guangzhou, eu-frankfurt, ...
_KEY_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,900}$")


class ConfigError(RuntimeError):
    """Raised when required external inputs are missing or malformed."""


@dataclass
class PocConfig:
    secret_id: Optional[str] = None
    secret_key: Optional[str] = None
    bucket: Optional[str] = None
    region: Optional[str] = None
    prefix: str = DEFAULT_PREFIX
    prod_origin: Optional[str] = None
    admin_token: Optional[str] = None
    missing: List[str] = field(default_factory=list)

    @property
    def has_credentials(self) -> bool:
        return bool(self.secret_id) and bool(self.secret_key)

    @property
    def cos_host(self) -> str:
        """Virtual-hosted style host: <bucket>.cos.<region>.myqcloud.com."""
        return f"{self.bucket}.cos.{self.region}.myqcloud.com"

    def describe(self) -> dict:
        """Redacted, log-safe summary. Never includes secret values."""
        return {
            "bucket": self.bucket,
            "region": self.region,
            "prefix": self.prefix,
            "prod_origin": self.prod_origin,
            "secret_id_present": bool(self.secret_id),
            "secret_key_present": bool(self.secret_key),
            "missing_env": list(self.missing),
        }

    def require_credentials(self) -> None:
        if not self.has_credentials or not self.bucket or not self.region:
            raise ConfigError(
                "blocked_external_input: missing " + ", ".join(self.missing)
                + " (PoC credentials / bucket / region are operator-supplied; "
                  "see docs/evidence/cos-20260924.md)"
            )

    def full_key(self, key: str) -> str:
        """Return key with the configured prefix, validating shape."""
        if key.startswith(self.prefix):
            full = key
        else:
            full = self.prefix + key
        if not _KEY_SAFE_RE.match(full):
            raise ConfigError(f"unsafe key shape (keep PoC keys ASCII, no spaces): {full!r} masked")
        return full


def normalize_prefix(raw: Optional[str]) -> str:
    if raw is None or raw == "":
        return DEFAULT_PREFIX
    prefix = raw.strip()
    if prefix.startswith("/"):
        raise ConfigError(f"{ENV_PREFIX} must not start with '/': got a leading-slash prefix")
    if not prefix.endswith("/"):
        prefix += "/"
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*/$", prefix):
        raise ConfigError(f"{ENV_PREFIX} must be a simple directory-like prefix, got invalid value")
    return prefix


def load_config(require: bool = True, env: Optional[dict] = None) -> PocConfig:
    """Build a PocConfig from the environment.

    With require=True, raises ConfigError listing the missing external inputs
    instead of attempting any network call with half-present credentials.
    """
    get = (env if env is not None else os.environ).get
    cfg = PocConfig(
        secret_id=get(ENV_SECRET_ID) or None,
        secret_key=get(ENV_SECRET_KEY) or None,
        bucket=get(ENV_BUCKET) or None,
        region=get(ENV_REGION) or None,
        prefix=normalize_prefix(get(ENV_PREFIX)),
        prod_origin=get(ENV_PROD_ORIGIN) or None,
        admin_token=get(ENV_ADMIN_TOKEN) or None,
    )
    if cfg.bucket and not _BUCKET_RE.match(cfg.bucket):
        raise ConfigError(
            f"{ENV_BUCKET} must look like '<name>-<appid>' (e.g. mybucket-1250000000)"
        )
    if cfg.region and not _REGION_RE.match(cfg.region):
        raise ConfigError(f"{ENV_REGION} must look like 'ap-guangzhou' / 'eu-frankfurt'")
    if cfg.prod_origin and not re.match(r"^https?://[A-Za-z0-9.:.-]+$", cfg.prod_origin):
        raise ConfigError(f"{ENV_PROD_ORIGIN} must be scheme://host[:port] exactly")
    cfg.missing = [
        name
        for name in (ENV_SECRET_ID, ENV_SECRET_KEY, ENV_BUCKET, ENV_REGION)
        if not get(name)
    ]
    if require:
        cfg.require_credentials()
    return cfg


def random_key(prefix: str, label: str = "obj") -> str:
    """Generate a random, information-free PoC object key under `prefix`.

    No patient data, no original filenames (合同 §3.1 key 命名纪律的 PoC 版).
    """
    stamp = secrets.token_hex(4)
    nonce = secrets.token_hex(12)
    return f"{prefix}{label}-{stamp}-{nonce}"
