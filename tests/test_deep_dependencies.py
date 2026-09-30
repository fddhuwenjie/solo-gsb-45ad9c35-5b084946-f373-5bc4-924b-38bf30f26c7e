"""深层依赖图回归测试：合法深链、深层环与菱形 DAG。

链深 1500 超过 Python 默认递归上限（1000）：修复前 /validate 与 /evaluate
会因 RecursionError 返回 500。环检测与评估遍历均为显式栈迭代实现——
不提高递归上限，也不限制链长。
"""

from __future__ import annotations

import sys

from app.evaluator import Evaluator
from app.models import Condition, Dependency, FlagConfig, Operator
from app.validation import validate_config

EVAL_URL = "/evaluate"
VALIDATE_URL = "/validate"

# 必须深过递归上限，测试才有回归意义（不得靠调大上限规避）。
DEPTH = 1500
assert DEPTH > sys.getrecursionlimit()


def _chain_config(depth: int = DEPTH) -> FlagConfig:
    """sw-0 -> sw-1 -> ... -> sw-(depth-1) 的无环链，每个开关 100% 命中 on。"""
    return FlagConfig.model_validate(
        {
            "version": 1,
            "switches": [
                {
                    "id": f"sw-{i}",
                    "default_variant": "off",
                    "variants": [{"id": "on"}, {"id": "off"}],
                    "dependencies": (
                        [{"switch": f"sw-{i + 1}"}] if i + 1 < depth else []
                    ),
                    "rules": [
                        {
                            "id": "r",
                            "conditions": [],
                            "allocations": [
                                {"variant_id": "on", "weight": 10000}
                            ],
                        }
                    ],
                }
                for i in range(depth)
            ],
        }
    )


def _evaluate(client, config: FlagConfig, switch_id: str, context=None):
    return client.post(
        EVAL_URL,
        json={
            "config": config.model_dump(mode="json"),
            "switch_id": switch_id,
            "user_id": "u",
            "context": context or {},
        },
    )


def _dep_prefix(depth: int) -> str:
    """第 depth 层（1 起）开关自身轨迹步骤在根轨迹中的完整 dep: 前缀。"""
    return "".join(f"dep:sw-{i}:" for i in range(1, depth + 1))


# ---------------------------------------------------------------------------
# 1500 层合法链
# ---------------------------------------------------------------------------


def test_validate_deep_acyclic_chain(client):
    resp = client.post(VALIDATE_URL, json=_chain_config().model_dump(mode="json"))
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "errors": []}


def test_evaluate_deep_acyclic_chain(client):
    resp = _evaluate(client, _chain_config(), "sw-0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["variant_id"] == "on"
    assert body["served_default"] is False
    assert body["reason"] is None

    steps = [t["step"] for t in body["trace"]]
    assert steps[0] == "start"
    assert steps[-1] == "variant_selected"
    # 每个非叶子开关贡献 start/check/rule_evaluated/rule_matched/bucket/
    # variant_selected 共 6 步，叶子少一步 check。
    assert len(steps) == 6 * (DEPTH - 1) + 5
    # 最深一层依赖的 check 步骤带有完整 dep: 前缀链。
    deepest_check = _dep_prefix(DEPTH - 1) + "check"
    check = next(t for t in body["trace"] if t["step"] == deepest_check)
    assert check["detail"]["dependency"] == f"sw-{DEPTH - 1}"
    assert check["detail"]["satisfied"] is True


def test_evaluate_deep_chain_short_circuits_at_disabled_switch():
    # 链中间的 sw-750 关闭：其下（sw-751 起）不应被评估，失败逐层向上传播。
    mid = DEPTH // 2
    config = _chain_config()
    config.switches[mid].enabled = False

    result = Evaluator(config).evaluate("sw-0", "u", {})
    assert result.variant_id == "off"
    assert result.served_default is True
    assert result.reason == "dependency_not_satisfied"

    steps = [t.step for t in result.trace]
    # sw-750 自己的 default 步骤（原因 switch_disabled）出现在轨迹中……
    disabled_default = _dep_prefix(mid) + "default"
    entry = next(t for t in result.trace if t.step == disabled_default)
    assert entry.detail["reason"] == "switch_disabled"
    # ……但它依赖的 sw-751 从未被评估（首个不满足即短路）。
    assert not any(f"dep:sw-{mid + 1}" in s for s in steps)


def test_evaluate_deep_chain_threads_context_to_leaf():
    # 叶子规则依赖 context：证明 context 一路透传到 1500 层深处。
    config = _chain_config()
    leaf = config.switches[-1]
    leaf.rules[0].conditions = [
        Condition(attribute="tier", operator=Operator.EQ, value="gold")
    ]

    ev = Evaluator(config)
    ok = ev.evaluate("sw-0", "u", {"tier": "gold"})
    assert ok.variant_id == "on"
    assert ok.served_default is False

    blocked = ev.evaluate("sw-0", "u", {"tier": "free"})
    assert blocked.variant_id == "off"
    assert blocked.reason == "dependency_not_satisfied"


def test_minimize_context_deep_chain(client):
    # 解释接口复用同一套校验与评估，深链同样不得触发递归上限。
    resp = client.post(
        "/explanations/minimize",
        json={
            "config": _chain_config().model_dump(mode="json"),
            "switch_id": "sw-0",
            "user_id": "u",
            "context": {},
            "domains": [],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["baseline_variant_id"] == "on"
    assert body["kept_attributes"] == []
    assert body["removed_attributes"] == []


# ---------------------------------------------------------------------------
# 深层环
# ---------------------------------------------------------------------------


def _deep_cycle_config(depth: int = DEPTH) -> FlagConfig:
    """sw-0 -> ... -> sw-(depth-1) -> sw-0 的 1500 节点环。"""
    config = _chain_config(depth)
    config.switches[-1].dependencies = [Dependency(switch="sw-0")]
    return config


def test_validate_deep_cycle_reports_full_cycle(client):
    resp = client.post(
        VALIDATE_URL, json=_deep_cycle_config().model_dump(mode="json")
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    cycles = [e for e in body["errors"] if e["code"] == "dependency_cycle"]
    # 同一个环只报一次，且消息给出完整环路径。
    assert len(cycles) == 1
    expected = " -> ".join([f"sw-{i}" for i in range(DEPTH)] + ["sw-0"])
    assert cycles[0]["message"] == "依赖图存在环: " + expected


def test_evaluate_deep_cycle_rejected_with_422(client):
    resp = _evaluate(client, _deep_cycle_config(), "sw-0")
    assert resp.status_code == 422
    codes = {e["code"] for e in resp.json()["detail"]["errors"]}
    assert "dependency_cycle" in codes


def test_validate_deep_cycle_via_engine():
    issues = validate_config(_deep_cycle_config())
    assert [i.code for i in issues] == ["dependency_cycle"]


# ---------------------------------------------------------------------------
# 菱形 DAG
# ---------------------------------------------------------------------------


def _diamond_config(layers: int) -> FlagConfig:
    """layers 层菱形：a_i -> b_i, c_i；b_i 与 c_i 共同 -> a_{i+1}。

    共 3*layers+1 个开关、深度 2*layers+1；b/c 两条支路共享下游节点，
    校验遍历必须对已完成（BLACK）节点去重，不能指数膨胀或递归。
    """
    switches = []

    def node(sid, deps):
        return {
            "id": sid,
            "default_variant": "v",
            "variants": [{"id": "v"}],
            "dependencies": [{"switch": d} for d in deps],
        }

    for i in range(layers):
        switches.append(node(f"a-{i}", [f"b-{i}", f"c-{i}"]))
        switches.append(node(f"b-{i}", [f"a-{i + 1}"]))
        switches.append(node(f"c-{i}", [f"a-{i + 1}"]))
    switches.append(node(f"a-{layers}", []))
    return FlagConfig.model_validate({"version": 1, "switches": switches})


def test_validate_deep_diamond_dag(client):
    # 750 层菱形：深度 1501、2251 个开关，共享节点被反复到达仍合法。
    resp = client.post(
        VALIDATE_URL, json=_diamond_config(750).model_dump(mode="json")
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "errors": []}


def _eval_diamond_config() -> FlagConfig:
    """四层菱形 DAG：top -> b, c；b -> d；c -> d，全部 100% 命中 on。"""

    def node(sid, deps):
        return {
            "id": sid,
            "default_variant": "off",
            "variants": [{"id": "on"}, {"id": "off"}],
            "dependencies": [{"switch": d} for d in deps],
            "rules": [
                {
                    "id": "r",
                    "conditions": [],
                    "allocations": [{"variant_id": "on", "weight": 10000}],
                }
            ],
        }

    return FlagConfig.model_validate(
        {
            "version": 1,
            "switches": [
                node("top", ["b", "c"]),
                node("b", ["d"]),
                node("c", ["d"]),
                node("d", []),
            ],
        }
    )


def test_evaluate_diamond_dag_order_and_shared_descendant(client):
    resp = _evaluate(client, _eval_diamond_config(), "top")
    assert resp.status_code == 200
    body = resp.json()
    assert body["variant_id"] == "on"

    steps = [t["step"] for t in body["trace"]]
    # 共享的 d 在两条支路下各评估一次。
    assert "dep:b:dep:d:check" in steps
    assert "dep:c:dep:d:check" in steps
    # 依赖按声明顺序求值：b 支路的全部轨迹先于 c 支路。
    b_indices = [i for i, s in enumerate(steps) if s.startswith("dep:b:")]
    c_indices = [i for i, s in enumerate(steps) if s.startswith("dep:c:")]
    assert b_indices and c_indices
    assert max(b_indices) < min(c_indices)


def test_evaluate_diamond_short_circuits_on_first_unsatisfied(client):
    # b 关闭 -> top 的第一个依赖不满足即短路，c 支路根本不被评估。
    config = _eval_diamond_config()
    config.switches[1].enabled = False

    resp = _evaluate(client, config, "top")
    assert resp.status_code == 200
    body = resp.json()
    assert body["variant_id"] == "off"
    assert body["served_default"] is True
    assert body["reason"] == "dependency_not_satisfied"

    steps = [t["step"] for t in body["trace"]]
    assert any(s.startswith("dep:b:") for s in steps)
    assert not any(s.startswith("dep:c:") for s in steps)
