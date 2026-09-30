"""评估引擎单元测试：哈希分桶、累计权重区间、规则、运算符。"""

from __future__ import annotations

import hashlib

import pytest

from app.evaluator import Evaluator, hash_bucket
from app.models import (
    BUCKETS,
    Allocation,
    Condition,
    FlagConfig,
    Operator,
    Rule,
    Switch,
    Variant,
)
from tests.conftest import alloc, cond, rule, variant


def _make_switch(**kwargs) -> FlagConfig:
    defaults = dict(
        id="s",
        default_variant="off",
        variants=[variant("on"), variant("off")],
        rules=[],
    )
    defaults.update(kwargs)
    return FlagConfig(switches=[Switch(**defaults)])


def test_hash_bucket_known_digest():
    # 契约：sha256(f"{salt}:{switch_id}:{user_id}") 前 8 位 hex 模 10000。
    digest = hashlib.sha256("salt123:new_checkout:user-7".encode()).hexdigest()
    expected = int(digest[:8], 16) % 10000
    assert hash_bucket("salt123", "new_checkout", "user-7") == expected


def test_hash_bucket_range_and_determinism():
    for user in [f"u{i}" for i in range(200)]:
        b = hash_bucket("s", "s", user)
        assert 0 <= b < BUCKETS
        assert hash_bucket("s", "s", user) == b


def test_cumulative_weight_boundaries(simple_config, monkeypatch):
    # 5000/5000：0 与 4999 落 on，5000 与 9999 落 off。
    ev = Evaluator(simple_config)
    for bucket, expected in [(0, "on"), (4999, "on"), (5000, "off"), (9999, "off")]:
        monkeypatch.setattr("app.evaluator.hash_bucket", lambda *a, **k: bucket)
        result = ev.evaluate("new_checkout", "u1", {"tier": "gold"})
        assert result.variant_id == expected, bucket
        assert result.served_default is False


def test_uneven_split_boundaries(monkeypatch):
    cfg = _make_switch(
        rules=[
            rule("r", [], [alloc("on", 2000), alloc("off", 8000)])
        ]
    )
    ev = Evaluator(cfg)
    for bucket, expected in [(0, "on"), (1999, "on"), (2000, "off"), (9999, "off")]:
        monkeypatch.setattr("app.evaluator.hash_bucket", lambda *a, **k: bucket)
        assert ev.evaluate("s", "u", {}).variant_id == expected


def test_zero_weight_variant_never_selected(monkeypatch):
    cfg = _make_switch(
        variants=[variant("canary"), variant("stable")],
        default_variant="stable",
        rules=[
            rule(
                "r",
                [],
                [alloc("canary", 0), alloc("stable", 10000)],
            )
        ],
    )
    ev = Evaluator(cfg)
    chosen = set()
    for bucket in range(0, 10000, 37):
        monkeypatch.setattr("app.evaluator.hash_bucket", lambda *a, _b=bucket, **k: _b)
        chosen.add(ev.evaluate("s", "u", {}).variant_id)
    assert chosen == {"stable"}


def test_distribution_proportion(simple_config):
    ev = Evaluator(simple_config)
    counts = {"on": 0, "off": 0}
    n = 6000
    for i in range(n):
        r = ev.evaluate("new_checkout", f"user-{i}", {"tier": "gold"})
        counts[r.variant_id] += 1
    # 期望约 50/50，留足裕量避免哈希抖动造成脆弱测试。
    assert counts["on"] == pytest.approx(n / 2, abs=n * 0.06)


def test_first_matching_rule_wins():
    cfg = _make_switch(
        variants=[variant("v1"), variant("v2"), variant("off")],
        rules=[
            rule(
                "first",
                [cond("tier", Operator.EQ, "gold")],
                [alloc("v1", 10000)],
            ),
            rule(
                "second",
                [cond("tier", Operator.EQ, "gold")],
                [alloc("v2", 10000)],
            ),
        ]
    )
    result = Evaluator(cfg).evaluate("s", "u", {"tier": "gold"})
    assert result.variant_id == "v1"
    matched_steps = [t.step for t in result.trace if t.step == "rule_matched"]
    assert matched_steps == ["rule_matched"]
    matched = next(t for t in result.trace if t.step == "rule_matched")
    assert matched.detail["rule_id"] == "first"


def test_rules_evaluated_in_order_and_skip_nonmatching():
    cfg = _make_switch(
        variants=[variant("v1"), variant("v2"), variant("off")],
        rules=[
            rule(
                "silver_only",
                [cond("tier", Operator.EQ, "silver")],
                [alloc("v1", 10000)],
            ),
            rule(
                "everyone",
                [],
                [alloc("v2", 10000)],
            ),
        ]
    )
    result = Evaluator(cfg).evaluate("s", "u", {"tier": "gold"})
    assert result.variant_id == "v2"
    evaluated = [
        t.detail["rule_id"]
        for t in result.trace
        if t.step == "rule_evaluated"
    ]
    assert evaluated == ["silver_only", "everyone"]


def test_all_operators():
    table = [
        (cond("x", Operator.EQ, 5), {"x": 5}, True),
        (cond("x", Operator.EQ, 5), {"x": 6}, False),
        (cond("x", Operator.NEQ, 5), {"x": 6}, True),
        (cond("x", Operator.NEQ, 5), {"x": 5}, False),
        (cond("x", Operator.IN, [1, 2]), {"x": 2}, True),
        (cond("x", Operator.IN, [1, 2]), {"x": 3}, False),
        (cond("x", Operator.NOT_IN, [1, 2]), {"x": 3}, True),
        (cond("name", Operator.CONTAINS, "li"), {"name": "alice"}, True),
        (cond("tags", Operator.CONTAINS, "beta"), {"tags": ["beta", "x"]}, True),
        (cond("x", Operator.GT, 10), {"x": 11}, True),
        (cond("x", Operator.GT, 10), {"x": 10}, False),
        (cond("x", Operator.GTE, 10), {"x": 10}, True),
        (cond("x", Operator.LT, 10), {"x": 9}, True),
        (cond("x", Operator.LTE, 10), {"x": 10}, True),
        (cond("x", Operator.EXISTS, True), {"x": 0}, True),
        (cond("x", Operator.EXISTS, False), {"x": 1}, False),
        (cond("x", Operator.EXISTS, False), {}, True),
        (cond("x", Operator.EQ, 1), {}, False),
        (cond("x", Operator.GT, 1), {"x": "not-a-number"}, False),
        (cond("x", Operator.IN, [1]), {"x": None}, False),
    ]
    for i, (c, ctx, expected) in enumerate(table):
        # 每个用例独立开关、单条规则，避免后续规则串扰。
        cfg = _make_switch(
            variants=[variant("yes"), variant("off")],
            rules=[rule("r", [c], [alloc("yes", 10000)])],
        )
        r = Evaluator(cfg).evaluate("s", "u", ctx)
        assert (r.variant_id == "yes") is expected, f"case {i}: {ctx}"


def test_no_rule_matched_returns_default():
    cfg = _make_switch(
        rules=[
            rule(
                "vip",
                [cond("tier", Operator.EQ, "vip")],
                [alloc("on", 10000)],
            )
        ]
    )
    result = Evaluator(cfg).evaluate("s", "u", {"tier": "free"})
    assert result.variant_id == "off"
    assert result.served_default is True
    assert result.reason == "no_rule_matched"


def test_disabled_switch_returns_default():
    cfg = _make_switch(enabled=False)
    result = Evaluator(cfg).evaluate("s", "u", {})
    assert result.variant_id == "off"
    assert result.reason == "switch_disabled"


def test_default_salt_falls_back_to_switch_id(monkeypatch):
    captured = {}

    def fake_hash(salt, switch_id, user_id):
        captured["salt"] = salt
        return 0

    cfg = _make_switch(rules=[rule("r", [], [alloc("on", 10000)])])
    monkeypatch.setattr("app.evaluator.hash_bucket", fake_hash)
    Evaluator(cfg).evaluate("s", "u", {})
    assert captured["salt"] == "s"


def test_custom_salt_used(monkeypatch):
    captured = {}

    def fake_hash(salt, switch_id, user_id):
        captured["salt"] = salt
        return 0

    cfg = _make_switch(
        salt="pepper-9",
        rules=[rule("r", [], [alloc("on", 10000)])],
    )
    monkeypatch.setattr("app.evaluator.hash_bucket", fake_hash)
    Evaluator(cfg).evaluate("s", "u", {})
    assert captured["salt"] == "pepper-9"


def test_payload_is_returned(simple_config):
    result = Evaluator(simple_config).evaluate(
        "new_checkout", "u1", {"tier": "gold"}
    )
    # 命中的变体 payload 与默认变体 payload 之一（取决于桶），结构必须透传。
    assert result.payload in ({"ui": "new"}, {"ui": "old"})


def test_bucket_trace_contains_ranges(simple_config):
    result = Evaluator(simple_config).evaluate(
        "new_checkout", "u1", {"tier": "gold"}
    )
    bucket_entry = next(t for t in result.trace if t.step == "bucket")
    assert bucket_entry.detail["buckets"] == 10000
    assert 0 <= bucket_entry.detail["bucket"] <= 9999
    assert bucket_entry.detail["ranges"] == [
        {"variant_id": "on", "start": 0, "end": 4999, "weight": 5000},
        {"variant_id": "off", "start": 5000, "end": 9999, "weight": 5000},
    ]
