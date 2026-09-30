"""依赖递归评估测试。"""

from __future__ import annotations

from app.evaluator import Evaluator
from app.models import Dependency, FlagConfig, Operator, Switch, Variant
from tests.conftest import alloc, cond, rule, variant


def test_dependency_satisfied_recurses(two_switch_config):
    ev = Evaluator(two_switch_config)
    result = ev.evaluate("feature_a", "u1", {"tier": "gold"})
    # feature_b 对 gold 用户解析为 b_on（非默认），feature_a 依赖满足 -> a_on
    assert result.variant_id == "a_on"
    assert result.served_default is False

    dep_steps = [t.step for t in result.trace if t.step.startswith("dep:feature_b:")]
    assert "dep:feature_b:check" in dep_steps
    check = next(t for t in result.trace if t.step == "dep:feature_b:check")
    assert check.detail["satisfied"] is True
    assert check.detail["resolved_variant"] == "b_on"
    assert check.detail["expected_variant"] == "b_on"


def test_dependency_not_satisfied_returns_default(two_switch_config):
    ev = Evaluator(two_switch_config)
    # 非 gold 用户：feature_b 无规则命中 -> b_off（默认变体）
    result = ev.evaluate("feature_a", "u1", {"tier": "free"})
    assert result.variant_id == "a_off"
    assert result.served_default is True
    assert result.reason == "dependency_not_satisfied"
    check = next(t for t in result.trace if t.step == "dep:feature_b:check")
    assert check.detail["satisfied"] is False
    check_default = next(t for t in result.trace if t.step == "default")
    assert check_default.detail["reason"] == "dependency_not_satisfied"


def test_dependency_satisfied_when_non_default_without_variant_pin():
    # 依赖不指定 variant：被依赖开关解析到任意非默认变体即满足。
    cfg = FlagConfig(
        switches=[
            Switch(
                id="parent",
                default_variant="p_off",
                variants=[Variant(id="p_on"), Variant(id="p_off")],
                dependencies=[Dependency(switch="child")],
                rules=[rule("r", [], [alloc("p_on", 10000)])],
            ),
            Switch(
                id="child",
                default_variant="c_off",
                variants=[Variant(id="c_on"), Variant(id="c_off")],
                rules=[
                    rule(
                        "r",
                        [cond("ok", Operator.EQ, True)],
                        [alloc("c_on", 10000)],
                    )
                ],
            ),
        ]
    )
    ev = Evaluator(cfg)
    assert ev.evaluate("parent", "u", {"ok": True}).variant_id == "p_on"
    failed = ev.evaluate("parent", "u", {"ok": False})
    assert failed.variant_id == "p_off"
    assert failed.reason == "dependency_not_satisfied"


def test_pinned_dependency_requires_exact_variant(two_switch_config):
    ev = Evaluator(two_switch_config)
    # gold 用户下 feature_b -> b_on；改成期望 b_off 后依赖应当不满足。
    two_switch_config.switches[0].dependencies[0].variant = "b_off"
    result = ev.evaluate("feature_a", "u1", {"tier": "gold"})
    assert result.variant_id == "a_off"
    assert result.reason == "dependency_not_satisfied"


def test_chained_dependencies():
    def gate(id, dep):
        return Switch(
            id=id,
            default_variant=f"{id}_off",
            variants=[variant(f"{id}_on"), variant(f"{id}_off")],
            dependencies=[Dependency(switch=dep)] if dep else [],
            rules=[
                rule(
                    "r",
                    [cond("allow", Operator.EQ, True)],
                    [alloc(f"{id}_on", 10000)],
                )
            ],
        )

    # c -> b -> a；a 没有依赖。
    cfg = FlagConfig(switches=[gate("c", "b"), gate("b", "a"), gate("a", None)])
    ev = Evaluator(cfg)

    ok = ev.evaluate("c", "u", {"allow": True})
    assert ok.variant_id == "c_on"

    blocked = ev.evaluate("c", "u", {"allow": False})
    assert blocked.variant_id == "c_off"
    assert blocked.reason == "dependency_not_satisfied"

    # 轨迹中应包含完整递归链 c -> b -> a（嵌套依赖按层级加前缀）。
    dep_checks = {
        t.step
        for t in ok.trace
        if t.step.startswith("dep:") and t.step.endswith(":check")
    }
    assert dep_checks == {"dep:b:check", "dep:b:dep:a:check"}


def test_dependency_on_disabled_switch_fails():
    cfg = FlagConfig(
        switches=[
            Switch(
                id="parent",
                default_variant="p_off",
                variants=[variant("p_on"), variant("p_off")],
                dependencies=[Dependency(switch="child")],
                rules=[rule("r", [], [alloc("p_on", 10000)])],
            ),
            Switch(
                id="child",
                enabled=False,
                default_variant="c_off",
                variants=[variant("c_off")],
            ),
        ]
    )
    result = Evaluator(cfg).evaluate("parent", "u", {})
    assert result.variant_id == "p_off"
    assert result.reason == "dependency_not_satisfied"
    check = next(t for t in result.trace if t.step == "dep:child:check")
    assert check.detail["resolved_variant"] == "c_off"
