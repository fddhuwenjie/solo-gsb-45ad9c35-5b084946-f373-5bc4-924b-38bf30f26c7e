"""FastAPI 接口测试：/health、/validate、/evaluate。"""

from __future__ import annotations

import copy

EVAL_URL = "/evaluate"
VALIDATE_URL = "/validate"


def _evaluate(client, config, switch_id, user_id, context=None):
    return client.post(
        EVAL_URL,
        json={
            "config": config.model_dump(mode="json"),
            "switch_id": switch_id,
            "user_id": user_id,
            "context": context or {},
        },
    )


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_evaluate_happy_path(client, simple_config):
    resp = _evaluate(client, simple_config, "new_checkout", "user-42", {"tier": "gold"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["switch_id"] == "new_checkout"
    assert body["variant_id"] in ("on", "off")
    assert body["served_default"] is False
    assert body["reason"] is None
    steps = [t["step"] for t in body["trace"]]
    assert steps[0] == "start"
    assert "rule_matched" in steps
    assert "bucket" in steps
    assert steps[-1] == "variant_selected"
    bucket = next(t for t in body["trace"] if t["step"] == "bucket")
    assert set(bucket["detail"]) == {"salt", "user_id", "bucket", "buckets", "ranges"}
    assert bucket["detail"]["user_id"] == "user-42"


def test_evaluate_default_when_no_rule_match(client, simple_config):
    resp = _evaluate(client, simple_config, "new_checkout", "u", {"tier": "free"})
    body = resp.json()
    assert body["variant_id"] == "off"
    assert body["served_default"] is True
    assert body["reason"] == "no_rule_matched"
    assert body["payload"] == {"ui": "old"}
    default_entry = next(t for t in body["trace"] if t["step"] == "default")
    assert default_entry["detail"]["reason"] == "no_rule_matched"


def test_evaluate_unknown_switch_returns_404(client, simple_config):
    resp = _evaluate(client, simple_config, "ghost", "u")
    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]


def test_evaluate_rejects_invalid_config(client, simple_config):
    cfg = copy.deepcopy(simple_config)
    cfg.switches[0].rules[0].allocations[0].weight = 1
    resp = _evaluate(client, cfg, "new_checkout", "u")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "校验未通过" in detail["message"]
    assert any(e["code"] == "invalid_weight_sum" for e in detail["errors"])


def test_evaluate_rejects_config_with_cycle(client):
    from app.models import Dependency, FlagConfig, Switch, Variant

    cfg = FlagConfig(
        switches=[
            Switch(
                id="a",
                default_variant="v",
                variants=[Variant(id="v")],
                dependencies=[Dependency(switch="b")],
            ),
            Switch(
                id="b",
                default_variant="v",
                variants=[Variant(id="v")],
                dependencies=[Dependency(switch="a")],
            ),
        ]
    )
    resp = _evaluate(client, cfg, "a", "u")
    assert resp.status_code == 422
    codes = {e["code"] for e in resp.json()["detail"]["errors"]}
    assert "dependency_cycle" in codes


def test_evaluate_rejects_unknown_dependency(client, simple_config):
    from app.models import Dependency

    cfg = copy.deepcopy(simple_config)
    cfg.switches[0].dependencies.append(Dependency(switch="missing"))
    resp = _evaluate(client, cfg, "new_checkout", "u")
    assert resp.status_code == 422
    codes = {e["code"] for e in resp.json()["detail"]["errors"]}
    assert "unknown_dependency" in codes


def test_evaluate_dependency_flow(client, two_switch_config):
    ok = _evaluate(client, two_switch_config, "feature_a", "u", {"tier": "gold"})
    assert ok.json()["variant_id"] == "a_on"
    blocked = _evaluate(client, two_switch_config, "feature_a", "u", {"tier": "free"})
    assert blocked.json()["variant_id"] == "a_off"
    assert blocked.json()["reason"] == "dependency_not_satisfied"


def test_evaluate_requires_user_id(client, simple_config):
    resp = client.post(
        EVAL_URL,
        json={
            "config": simple_config.model_dump(mode="json"),
            "switch_id": "new_checkout",
            "context": {},
        },
    )
    assert resp.status_code == 422


def test_consistent_assignment_for_same_user(client, simple_config):
    first = _evaluate(client, simple_config, "new_checkout", "stable-user", {"tier": "gold"})
    second = _evaluate(client, simple_config, "new_checkout", "stable-user", {"tier": "gold"})
    assert first.json()["variant_id"] == second.json()["variant_id"]


def test_different_salts_can_diverge(client, simple_config):
    # 同一用户在不同盐值下桶可能不同；统计上至少有一对盐值产生分歧。
    variants = set()
    for salt in [f"salt-{i}" for i in range(40)]:
        cfg = copy.deepcopy(simple_config)
        cfg.switches[0].salt = salt
        variants.add(
            _evaluate(client, cfg, "new_checkout", "user-xyz", {"tier": "gold"})
            .json()["variant_id"]
        )
    assert variants == {"on", "off"}


RESOLVE_URL = "/release-windows/resolve"


def test_release_windows_resolve_happy_path(client):
    resp = client.post(
        RESOLVE_URL,
        json={
            "start": "2026-02-07T00:00:00Z",
            "end": "2026-02-08T00:00:00Z",
            "min_duration_seconds": 3600,
            "calendars": [
                {
                    "calendar_id": "us",
                    "timezone": "America/New_York",
                    "weekly_windows": [
                        {"rule_id": "sat", "weekday": 5, "start_local": "12:00",
                         "end_local": "18:00"}
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["start"] == "2026-02-07T00:00:00Z"
    assert [(w["start"], w["end"]) for w in body["windows"]] == [
        ("2026-02-07T17:00:00Z", "2026-02-07T23:00:00Z")
    ]
    assert body["windows"][0]["duration_seconds"] == 6 * 3600


def test_release_windows_resolve_rejects_non_utc(client):
    resp = client.post(
        RESOLVE_URL,
        json={
            "start": "2026-02-07T08:00:00+08:00",
            "end": "2026-02-08T00:00:00Z",
            "min_duration_seconds": 60,
            "calendars": [{"calendar_id": "u", "timezone": "UTC"}],
        },
    )
    assert resp.status_code == 422
