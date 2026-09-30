"""校验接口测试：依赖图、规则引用、权重、环。"""

from __future__ import annotations

import copy

from app.models import (
    Allocation,
    Dependency,
    FlagConfig,
    Switch,
    Variant,
)

VALIDATE_URL = "/validate"


def _post(client, config: FlagConfig):
    return client.post(VALIDATE_URL, json=config.model_dump())


def _codes(resp) -> set[str]:
    return {e["code"] for e in resp.json()["errors"]}


def test_valid_config_passes(client, simple_config):
    resp = _post(client, simple_config)
    assert resp.status_code == 200
    assert resp.json()["valid"] is True
    assert resp.json()["errors"] == []


def test_duplicate_switch_rejected(client, simple_config):
    cfg = copy.deepcopy(simple_config)
    cfg.switches.append(copy.deepcopy(cfg.switches[0]))
    resp = _post(client, cfg)
    assert resp.json()["valid"] is False
    assert "duplicate_switch" in _codes(resp)


def test_unknown_default_variant_rejected(client, simple_config):
    cfg = copy.deepcopy(simple_config)
    cfg.switches[0].default_variant = "ghost"
    resp = _post(client, cfg)
    assert "unknown_default_variant" in _codes(resp)


def test_duplicate_variant_rejected(client):
    cfg = FlagConfig(
        switches=[
            Switch(
                id="s",
                default_variant="v1",
                variants=[Variant(id="v1"), Variant(id="v1")],
            )
        ]
    )
    resp = _post(client, cfg)
    assert "duplicate_variant" in _codes(resp)


def test_rule_unknown_variant_and_bad_weight_sum(client, simple_config):
    cfg = copy.deepcopy(simple_config)
    r = cfg.switches[0].rules[0]
    r.allocations[0] = Allocation(variant_id="ghost", weight=4000)
    r.allocations[1] = Allocation(variant_id="off", weight=4000)
    resp = _post(client, cfg)
    codes = _codes(resp)
    assert "unknown_rule_variant" in codes
    assert "invalid_weight_sum" in codes
    err = next(
        e for e in resp.json()["errors"] if e["code"] == "invalid_weight_sum"
    )
    assert "8000" in err["message"] and "10000" in err["message"]


def test_weight_sum_must_equal_10000(client, simple_config):
    cfg = copy.deepcopy(simple_config)
    cfg.switches[0].rules[0].allocations[0].weight = 6000
    # 6000 + 5000 = 11000
    resp = _post(client, cfg)
    assert "invalid_weight_sum" in _codes(resp)


def test_duplicate_allocation_rejected(client, simple_config):
    cfg = copy.deepcopy(simple_config)
    cfg.switches[0].rules[0].allocations = [
        Allocation(variant_id="on", weight=5000),
        Allocation(variant_id="on", weight=5000),
    ]
    resp = _post(client, cfg)
    assert "duplicate_allocation" in _codes(resp)


def test_unknown_dependency_rejected(client, simple_config):
    cfg = copy.deepcopy(simple_config)
    cfg.switches[0].dependencies.append(Dependency(switch="missing"))
    resp = _post(client, cfg)
    assert "unknown_dependency" in _codes(resp)
    assert resp.json()["valid"] is False


def test_unknown_dependency_variant_rejected(client, two_switch_config):
    cfg = copy.deepcopy(two_switch_config)
    cfg.switches[0].dependencies[0].variant = "nope"
    resp = _post(client, cfg)
    assert "unknown_dependency_variant" in _codes(resp)


def test_self_dependency_is_a_cycle(client):
    cfg = FlagConfig(
        switches=[
            Switch(
                id="selfish",
                default_variant="v",
                variants=[Variant(id="v")],
                dependencies=[Dependency(switch="selfish")],
            )
        ]
    )
    resp = _post(client, cfg)
    codes = _codes(resp)
    assert "dependency_cycle" in codes
    assert "selfish -> selfish" in resp.json()["errors"][0]["message"]


def test_two_node_cycle_detected(client):
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
    resp = _post(client, cfg)
    assert "dependency_cycle" in _codes(resp)
    assert resp.json()["valid"] is False


def test_three_node_cycle_detected(client):
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
                dependencies=[Dependency(switch="c")],
            ),
            Switch(
                id="c",
                default_variant="v",
                variants=[Variant(id="v")],
                dependencies=[Dependency(switch="a")],
            ),
        ]
    )
    resp = _post(client, cfg)
    assert "dependency_cycle" in _codes(resp)


def test_diamond_dependency_graph_is_valid(client):
    def node(id, deps):
        return Switch(
            id=id,
            default_variant="v",
            variants=[Variant(id="v")],
            dependencies=[Dependency(switch=d) for d in deps],
        )

    cfg = FlagConfig(switches=[node("a", ["b", "c"]), node("b", ["d"]), node("c", ["d"]), node("d", [])])
    resp = _post(client, cfg)
    assert resp.json()["valid"] is True


def test_pydantic_rejects_negative_weight(client):
    bad = {
        "version": 1,
        "switches": [
            {
                "id": "s",
                "default_variant": "v1",
                "variants": [{"id": "v1"}],
                "rules": [
                    {
                        "id": "r",
                        "conditions": [],
                        "allocations": [
                            {"variant_id": "v1", "weight": -1},
                        ],
                    }
                ],
            }
        ],
    }
    resp = client.post(VALIDATE_URL, json=bad)
    # 权重 -1 被 Pydantic 拦截（422），而非通过业务校验。
    assert resp.status_code == 422


def test_condition_requires_value(client):
    bad = {
        "version": 1,
        "switches": [
            {
                "id": "s",
                "default_variant": "v1",
                "variants": [{"id": "v1"}],
                "rules": [
                    {
                        "id": "r",
                        "conditions": [{"attribute": "tier", "operator": "eq"}],
                        "allocations": [{"variant_id": "v1", "weight": 10000}],
                    }
                ],
            }
        ],
    }
    resp = client.post(VALIDATE_URL, json=bad)
    assert resp.status_code == 422
