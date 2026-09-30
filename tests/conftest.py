"""测试公共夹具。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import (
    Allocation,
    Condition,
    FlagConfig,
    Operator,
    Rule,
    Switch,
    Variant,
)

W = 10_000


def variant(id: str, payload=None) -> Variant:
    return Variant(id=id, payload=payload)


def alloc(variant_id: str, weight: int) -> Allocation:
    return Allocation(variant_id=variant_id, weight=weight)


def cond(attribute: str, operator: Operator, value=None) -> Condition:
    return Condition(attribute=attribute, operator=operator, value=value)


def rule(id: str, conditions: list[Condition], allocations: list[Allocation]) -> Rule:
    return Rule(id=id, conditions=conditions, allocations=allocations)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def simple_config() -> FlagConfig:
    """单开关：tier=gold 命中后 on/off 各 50%，否则默认 off。"""
    return FlagConfig(
        switches=[
            Switch(
                id="new_checkout",
                default_variant="off",
                variants=[
                    variant("on", {"ui": "new"}),
                    variant("off", {"ui": "old"}),
                ],
                rules=[
                    rule(
                        "gold_users",
                        [cond("tier", Operator.EQ, "gold")],
                        [alloc("on", 5000), alloc("off", 5000)],
                    )
                ],
            )
        ]
    )


@pytest.fixture
def two_switch_config() -> FlagConfig:
    """feature_a 依赖 feature_b 解析到 on；feature_b 对 gold 用户 100% on。"""
    return FlagConfig(
        switches=[
            Switch(
                id="feature_a",
                default_variant="a_off",
                variants=[variant("a_on"), variant("a_off")],
                dependencies=[{"switch": "feature_b", "variant": "b_on"}],
                rules=[
                    rule(
                        "always",
                        [],
                        [alloc("a_on", 10_000)],
                    )
                ],
            ),
            Switch(
                id="feature_b",
                default_variant="b_off",
                variants=[variant("b_on"), variant("b_off")],
                rules=[
                    rule(
                        "gold",
                        [cond("tier", Operator.EQ, "gold")],
                        [alloc("b_on", 10_000)],
                    )
                ],
            ),
        ]
    )
