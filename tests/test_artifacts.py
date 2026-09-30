"""配置制品测试：签发、验签、篡改、时间边界与多密钥场景。"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.artifacts import (
    REASON_EXPIRED,
    REASON_NOT_YET_VALID,
    REASON_TAMPERED,
    REASON_UNKNOWN_KEY,
    canonical_json,
    load_signing_keys,
    sign_config,
    verify_artifact,
)
from app.models import Artifact

SIGN_URL = "/artifacts/sign"
VERIFY_URL = "/artifacts/verify"

KEYS = {"key-a": "secret-alpha", "key-b": "secret-beta"}

# 覆盖当前时刻的宽时间窗，用于“有效”场景。
ISSUED = "2020-01-01T00:00:00Z"
EXPIRES = "2099-12-31T23:59:59Z"


@pytest.fixture
def signing_keys(monkeypatch):
    monkeypatch.setenv("SIGNING_KEYS", json.dumps(KEYS))
    return KEYS


def _sign(client, config, key_id="key-a", issued_at=ISSUED, expires_at=EXPIRES):
    payload = config if isinstance(config, dict) else config.model_dump(mode="json")
    resp = client.post(
        SIGN_URL,
        json={
            "config": payload,
            "key_id": key_id,
            "issued_at": issued_at,
            "expires_at": expires_at,
        },
    )
    assert resp.status_code == 200
    return resp.json()["artifact"]


def _verify(client, artifact):
    resp = client.post(VERIFY_URL, json={"artifact": artifact})
    assert resp.status_code == 200
    return resp.json()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ---- 规范化 JSON ----


def test_canonical_json_sorts_keys_recursively():
    data = {"z": {"y": 1, "b": 2}, "a": [{"d": 4, "c": 3}]}
    assert canonical_json(data) == b'{"a":[{"c":3,"d":4}],"z":{"b":2,"y":1}}'


def test_canonical_json_preserves_array_order():
    assert canonical_json({"a": [3, 1, 2]}) == b'{"a":[3,1,2]}'


def test_canonical_json_strips_whitespace_and_keeps_unicode():
    out = canonical_json({"a": 1, "b": [1, 2], "c": {"d": "中文"}})
    assert b", " not in out and b": " not in out and b"\n" not in out
    assert "中文".encode("utf-8") in out


def test_canonical_json_independent_of_insertion_order():
    first = canonical_json({"a": 1, "b": {"x": 1, "y": 2}})
    second = canonical_json({"b": {"y": 2, "x": 1}, "a": 1})
    assert first == second


# ---- 密钥加载 ----


def test_load_signing_keys_reads_env_json_mapping(monkeypatch):
    monkeypatch.setenv("SIGNING_KEYS", '{"k1": "s1", "k2": "s2"}')
    assert load_signing_keys() == {"k1": "s1", "k2": "s2"}


def test_load_signing_keys_missing_or_malformed_env(monkeypatch):
    monkeypatch.delenv("SIGNING_KEYS", raising=False)
    assert load_signing_keys() == {}
    monkeypatch.setenv("SIGNING_KEYS", "not-json")
    assert load_signing_keys() == {}
    monkeypatch.setenv("SIGNING_KEYS", '["not", "an", "object"]')
    assert load_signing_keys() == {}


# ---- 签发 ----


def test_sign_returns_artifact_with_digest_and_signature(
    client, signing_keys, simple_config
):
    artifact = _sign(client, simple_config)
    assert artifact["key_id"] == "key-a"
    assert artifact["config"] == simple_config.model_dump(mode="json")
    assert len(artifact["digest"]) == 64
    assert len(artifact["signature"]) == 64
    int(artifact["digest"], 16)
    int(artifact["signature"], 16)


def test_sign_is_deterministic(client, signing_keys, simple_config):
    assert _sign(client, simple_config) == _sign(client, simple_config)


def test_sign_digest_independent_of_json_key_order(
    client, signing_keys, simple_config
):
    cfg = simple_config.model_dump(mode="json")
    reordered = {key: cfg[key] for key in reversed(list(cfg))}
    assert _sign(client, cfg)["digest"] == _sign(client, reordered)["digest"]


def test_sign_digest_depends_on_array_order(
    client, signing_keys, two_switch_config
):
    cfg = two_switch_config.model_dump(mode="json")
    swapped = copy.deepcopy(cfg)
    swapped["switches"] = list(reversed(swapped["switches"]))
    assert _sign(client, cfg)["digest"] != _sign(client, swapped)["digest"]


def test_sign_rejects_invalid_config_with_422(client, signing_keys, simple_config):
    cfg = simple_config.model_dump(mode="json")
    cfg["switches"][0]["rules"][0]["allocations"][0]["weight"] = 1
    resp = client.post(
        SIGN_URL,
        json={
            "config": cfg,
            "key_id": "key-a",
            "issued_at": ISSUED,
            "expires_at": EXPIRES,
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "校验未通过" in detail["message"]
    assert any(e["code"] == "invalid_weight_sum" for e in detail["errors"])


def test_sign_rejects_unknown_key_with_404(client, signing_keys, simple_config):
    resp = client.post(
        SIGN_URL,
        json={
            "config": simple_config.model_dump(mode="json"),
            "key_id": "ghost",
            "issued_at": ISSUED,
            "expires_at": EXPIRES,
        },
    )
    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]


def test_sign_rejects_inverted_time_window(client, signing_keys, simple_config):
    resp = client.post(
        SIGN_URL,
        json={
            "config": simple_config.model_dump(mode="json"),
            "key_id": "key-a",
            "issued_at": EXPIRES,
            "expires_at": ISSUED,
        },
    )
    assert resp.status_code == 422


def test_sign_without_signing_keys_env_returns_404(
    client, monkeypatch, simple_config
):
    monkeypatch.delenv("SIGNING_KEYS", raising=False)
    resp = client.post(
        SIGN_URL,
        json={
            "config": simple_config.model_dump(mode="json"),
            "key_id": "key-a",
            "issued_at": ISSUED,
            "expires_at": EXPIRES,
        },
    )
    assert resp.status_code == 404


def test_sign_accepts_naive_datetime_as_utc(client, signing_keys, simple_config):
    artifact = _sign(
        client,
        simple_config,
        issued_at="2020-01-01T00:00:00",
        expires_at="2099-12-31T23:59:59",
    )
    assert _verify(client, artifact) == {"valid": True, "reason": None}


# ---- 验签 ----


def test_verify_round_trip_valid(client, signing_keys, simple_config):
    artifact = _sign(client, simple_config)
    assert _verify(client, artifact) == {"valid": True, "reason": None}


def test_verify_tampered_config(client, signing_keys, simple_config):
    artifact = _sign(client, simple_config)

    bumped = copy.deepcopy(artifact)
    bumped["config"]["version"] = 2
    assert _verify(client, bumped)["reason"] == REASON_TAMPERED

    hacked = copy.deepcopy(artifact)
    hacked["config"]["switches"][0]["variants"][0]["payload"]["ui"] = "hacked"
    assert _verify(client, hacked)["reason"] == REASON_TAMPERED


def test_verify_tampered_metadata(client, signing_keys, simple_config):
    artifact = _sign(client, simple_config)

    shifted = copy.deepcopy(artifact)
    shifted["issued_at"] = "2021-06-01T00:00:00Z"
    assert _verify(client, shifted)["reason"] == REASON_TAMPERED

    shifted = copy.deepcopy(artifact)
    shifted["expires_at"] = "2040-01-01T00:00:00Z"
    assert _verify(client, shifted)["reason"] == REASON_TAMPERED

    forged = copy.deepcopy(artifact)
    forged["digest"] = "0" * 64
    assert _verify(client, forged)["reason"] == REASON_TAMPERED

    forged = copy.deepcopy(artifact)
    forged["signature"] = "0" * 64
    assert _verify(client, forged)["reason"] == REASON_TAMPERED


def test_verify_tampered_signature_with_non_ascii(client, signing_keys, simple_config):
    artifact = _sign(client, simple_config)
    artifact["signature"] = "☃" * 64
    assert _verify(client, artifact)["reason"] == REASON_TAMPERED


def test_verify_unknown_key(client, signing_keys, simple_config):
    artifact = _sign(client, simple_config)
    artifact["key_id"] = "ghost"
    assert _verify(client, artifact)["reason"] == REASON_UNKNOWN_KEY


def test_verify_unknown_key_when_env_removed(
    client, signing_keys, monkeypatch, simple_config
):
    artifact = _sign(client, simple_config)
    monkeypatch.delenv("SIGNING_KEYS")
    assert _verify(client, artifact)["reason"] == REASON_UNKNOWN_KEY


def test_verify_not_yet_valid(client, signing_keys, simple_config):
    now = datetime.now(timezone.utc)
    artifact = _sign(
        client,
        simple_config,
        issued_at=_iso(now + timedelta(hours=1)),
        expires_at=_iso(now + timedelta(hours=2)),
    )
    assert _verify(client, artifact)["reason"] == REASON_NOT_YET_VALID


def test_verify_expired(client, signing_keys, simple_config):
    now = datetime.now(timezone.utc)
    artifact = _sign(
        client,
        simple_config,
        issued_at=_iso(now - timedelta(hours=2)),
        expires_at=_iso(now - timedelta(hours=1)),
    )
    assert _verify(client, artifact)["reason"] == REASON_EXPIRED


# ---- 时间边界（直接注入 now，闭区间 [issued_at, expires_at]）----


def _make_artifact(config, key_id, issued_at, expires_at, secret):
    config_dict = config.model_dump(mode="json")
    digest, signature = sign_config(
        config_dict, key_id, issued_at, expires_at, secret
    )
    return Artifact(
        config=config,
        key_id=key_id,
        issued_at=issued_at,
        expires_at=expires_at,
        digest=digest,
        signature=signature,
    )


def test_verify_time_window_boundaries(simple_config):
    issued = datetime(2026, 1, 1, tzinfo=timezone.utc)
    expires = datetime(2026, 12, 31, tzinfo=timezone.utc)
    artifact = _make_artifact(simple_config, "key-a", issued, expires, KEYS["key-a"])

    # 边界时刻仍有效（闭区间）。
    assert verify_artifact(artifact, KEYS, now=issued) is None
    assert verify_artifact(artifact, KEYS, now=expires) is None
    # 边界之外一秒即失败。
    assert (
        verify_artifact(artifact, KEYS, now=issued - timedelta(seconds=1))
        == REASON_NOT_YET_VALID
    )
    assert (
        verify_artifact(artifact, KEYS, now=expires + timedelta(seconds=1))
        == REASON_EXPIRED
    )


# ---- 多密钥 ----


def test_multi_key_signatures_differ_and_both_verify(
    client, signing_keys, simple_config
):
    artifact_a = _sign(client, simple_config, key_id="key-a")
    artifact_b = _sign(client, simple_config, key_id="key-b")

    # 摘要与密钥无关，签名与密钥绑定。
    assert artifact_a["digest"] == artifact_b["digest"]
    assert artifact_a["signature"] != artifact_b["signature"]
    assert _verify(client, artifact_a)["valid"] is True
    assert _verify(client, artifact_b)["valid"] is True


def test_multi_key_cross_swap_fails(client, signing_keys, simple_config):
    artifact_a = _sign(client, simple_config, key_id="key-a")
    artifact_b = _sign(client, simple_config, key_id="key-b")

    # 把 key-b 的签名贴到 key-a 的制品上。
    swapped = copy.deepcopy(artifact_a)
    swapped["signature"] = artifact_b["signature"]
    assert _verify(client, swapped)["reason"] == REASON_TAMPERED

    # key_id 也被签名覆盖：改成另一把真实存在的密钥同样失败。
    renamed = copy.deepcopy(artifact_a)
    renamed["key_id"] = "key-b"
    assert _verify(client, renamed)["reason"] == REASON_TAMPERED


def test_key_rotation_old_key_removed(client, signing_keys, monkeypatch, simple_config):
    artifact = _sign(client, simple_config, key_id="key-a")
    # 轮换后环境只保留新密钥：旧制品因 unknown_key 失效。
    monkeypatch.setenv("SIGNING_KEYS", json.dumps({"key-b": KEYS["key-b"]}))
    assert _verify(client, artifact)["reason"] == REASON_UNKNOWN_KEY
