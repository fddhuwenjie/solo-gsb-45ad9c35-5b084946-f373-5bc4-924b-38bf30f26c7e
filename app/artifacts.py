"""配置制品：规范化 JSON、SHA-256 摘要与 HMAC-SHA256 签发/验签。

- 规范化 JSON：对象键按字典序排列、保留数组顺序、去掉一切空白；
- digest：配置规范化 JSON 的 SHA-256 十六进制摘要；
- signature：以 SIGNING_KEYS 中的密钥对签名材料做 HMAC-SHA256，
  签名材料覆盖 key_id、issued_at、expires_at、digest 与 config；
- 密钥只从环境变量 SIGNING_KEYS（JSON 映射 key_id -> secret）读取。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any

from app.models import Artifact

# 验签失败原因（机器可读）。
REASON_UNKNOWN_KEY = "unknown_key"
REASON_TAMPERED = "tampered"
REASON_NOT_YET_VALID = "not_yet_valid"
REASON_EXPIRED = "expired"


def canonical_json(data: Any) -> bytes:
    """规范化 JSON 字节串：键按字典序、数组顺序不变、无空白。"""
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def load_signing_keys() -> dict[str, str]:
    """仅从环境变量 SIGNING_KEYS 读取密钥表（JSON 映射 key_id -> secret）。

    环境变量缺失、不是合法 JSON 或不是对象时，按“无任何密钥”处理。
    """
    raw = os.environ.get("SIGNING_KEYS")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key_id): str(secret) for key_id, secret in data.items()}


def config_digest(config_dict: dict[str, Any]) -> str:
    """配置规范化 JSON 的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(canonical_json(config_dict)).hexdigest()


def _signed_material(
    key_id: str,
    issued_at: datetime,
    expires_at: datetime,
    digest: str,
    config_dict: dict[str, Any],
) -> bytes:
    """签名材料：key_id、时间窗、摘要与配置的规范化 JSON。

    时间戳已由模型层归一到 UTC，isoformat 结果唯一，签发与验签可复现。
    """
    return canonical_json(
        {
            "config": config_dict,
            "digest": digest,
            "expires_at": expires_at.isoformat(),
            "issued_at": issued_at.isoformat(),
            "key_id": key_id,
        }
    )


def sign_config(
    config_dict: dict[str, Any],
    key_id: str,
    issued_at: datetime,
    expires_at: datetime,
    secret: str,
) -> tuple[str, str]:
    """返回 (digest, signature)：配置的 SHA-256 摘要与 HMAC-SHA256 签名。"""
    digest = config_digest(config_dict)
    material = _signed_material(key_id, issued_at, expires_at, digest, config_dict)
    signature = hmac.new(
        secret.encode("utf-8"), material, hashlib.sha256
    ).hexdigest()
    return digest, signature


def verify_artifact(
    artifact: Artifact,
    keys: dict[str, str],
    now: datetime | None = None,
) -> str | None:
    """验签：返回 None 表示有效，否则返回失败原因。

    判定顺序：unknown_key -> tampered（摘要/签名任一不符）->
    not_yet_valid -> expired。时间窗为闭区间 [issued_at, expires_at]。
    """
    secret = keys.get(artifact.key_id)
    if secret is None:
        return REASON_UNKNOWN_KEY

    config_dict = artifact.config.model_dump(mode="json")
    digest = config_digest(config_dict)
    if not _safe_compare(digest, artifact.digest):
        return REASON_TAMPERED

    material = _signed_material(
        artifact.key_id,
        artifact.issued_at,
        artifact.expires_at,
        digest,
        config_dict,
    )
    expected = hmac.new(
        secret.encode("utf-8"), material, hashlib.sha256
    ).hexdigest()
    if not _safe_compare(expected, artifact.signature):
        return REASON_TAMPERED

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if moment < artifact.issued_at:
        return REASON_NOT_YET_VALID
    if moment > artifact.expires_at:
        return REASON_EXPIRED
    return None


def _safe_compare(expected: str, provided: str) -> bool:
    """compare_digest 对非 ASCII str 会抛 TypeError，统一按不匹配处理。"""
    try:
        return hmac.compare_digest(expected, provided)
    except TypeError:
        return False
