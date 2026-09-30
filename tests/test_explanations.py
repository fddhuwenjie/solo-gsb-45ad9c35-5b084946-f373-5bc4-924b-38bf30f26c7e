"""POST /explanations/minimize 接口测试。

覆盖：首条规则遮蔽、依赖链属性的保留与移除、exists 缺失态、并列最小集合
按字典序取先、variant 相同但 reason 不同即不充分，以及未覆盖/多声明/
重复/空域/候选值重复/当前值越界/缺失不允许/组合超限等 422 与 404。
"""

from __future__ import annotations

from app.models import Dependency, FlagConfig, Switch
from tests.conftest import alloc, cond, rule, variant

MIN_URL = "/explanations/minimize"


def _post(client, config, switch_id, context=None, domains=None, user_id="u"):
    return client.post(
        MIN_URL,
        json={
            "config": config.model_dump(mode="json"),
            "switch_id": switch_id,
            "user_id": user_id,
            "context": context or {},
            "domains": [] if domains is None else domains,
        },
    )


def _domain(attribute, values, allow_missing=False):
    return {
        "attribute": attribute,
        "values": values,
        "allow_missing": allow_missing,
    }


def _single_rule_config():
    """单开关：tier=gold 命中后 100% on，否则无规则命中默认 off。"""
    return FlagConfig(
        switches=[
            Switch(
                id="s",
                default_variant="off",
                variants=[variant("on"), variant("off")],
                rules=[
                    rule(
                        "gold",
                        [cond("tier", "eq", "gold")],
                        [alloc("on", 10_000)],
                    )
                ],
            )
        ]
    )


# ---------------------------------------------------------------------------
# 基础 happy path
# ---------------------------------------------------------------------------


def test_minimize_keeps_required_attribute_and_echoes_baseline(client):
    config = _single_rule_config()
    resp = _post(
        client,
        config,
        "s",
        context={"tier": "gold", "nickname": "保留键"},
        domains=[_domain("tier", ["gold", "silver"])],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["switch_id"] == "s"
    assert body["user_id"] == "u"
    assert body["baseline_variant_id"] == "on"
    assert body["baseline_served_default"] is False
    assert body["baseline_reason"] is None
    # 去掉 tier 后 silver 会落到默认 off，故唯一最小集合是 {tier}。
    assert body["kept_attributes"] == ["tier"]
    assert body["removed_attributes"] == []
    # k=0：2 个域值（gold✓/silver✗）；k=1：空移除集合重评 1 次。
    assert body["checked_combinations"] == 3


def test_minimize_default_baseline_compares_reason(client):
    config = _single_rule_config()
    resp = _post(
        client,
        config,
        "s",
        context={"tier": "silver"},
        domains=[_domain("tier", ["gold", "silver"])],
    )
    body = resp.json()
    assert body["baseline_variant_id"] == "off"
    assert body["baseline_served_default"] is True
    assert body["baseline_reason"] == "no_rule_matched"
    assert body["kept_attributes"] == ["tier"]
    # k=0：第一个值 gold 即变为 on（variant 不同），短路；k=1：重评 1 次。
    assert body["checked_combinations"] == 2


def test_minimize_no_referenced_attributes(client):
    # 只有无条件兜底规则：没有任何属性被引用，域必须为空，空集合立即充分。
    config = FlagConfig(
        switches=[
            Switch(
                id="s",
                default_variant="off",
                variants=[variant("on"), variant("off")],
                rules=[rule("always", [], [alloc("on", 10_000)])],
            )
        ]
    )
    resp = _post(client, config, "s", context={"anything": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kept_attributes"] == []
    assert body["removed_attributes"] == []
    assert body["checked_combinations"] == 1
    assert body["baseline_variant_id"] == "on"


# ---------------------------------------------------------------------------
# 首条规则遮蔽
# ---------------------------------------------------------------------------


def _shadowing_config():
    """r1(tier=gold) 遮蔽 r2(region=cn-north)；两条都不命中时默认 off。"""
    return FlagConfig(
        switches=[
            Switch(
                id="s",
                default_variant="off",
                variants=[variant("on"), variant("off")],
                rules=[
                    rule(
                        "gold",
                        [cond("tier", "eq", "gold")],
                        [alloc("on", 10_000)],
                    ),
                    rule(
                        "north",
                        [cond("region", "eq", "cn-north")],
                        [alloc("off", 10_000)],
                    ),
                ],
            )
        ]
    )


def test_minimize_first_rule_shadowing(client):
    config = _shadowing_config()
    resp = _post(
        client,
        config,
        "s",
        context={"tier": "gold", "region": "cn-north"},
        domains=[
            _domain("region", ["cn-north", "cn-south"]),
            _domain("tier", ["gold", "silver"]),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    # 基准下 r1 先命中，r2 被遮蔽：保留 tier 后 region 无论取何值结果恒为 on。
    assert body["kept_attributes"] == ["tier"]
    assert body["removed_attributes"] == ["region"]
    # k=0：(cn-north,gold)✓、(cn-north,silver)✗ -> 2；
    # k=1：{region}：gold✓/silver✗ -> 2；{tier}：cn-north✓/cn-south✓ -> 2。
    assert body["checked_combinations"] == 6


def test_minimize_shadowed_rule_attribute_still_requires_domain(client):
    # 遮蔽只对基准 context 成立；r2 引用的 region 仍必须声明域，否则 422。
    config = _shadowing_config()
    resp = _post(
        client,
        config,
        "s",
        context={"tier": "gold", "region": "cn-north"},
        domains=[_domain("tier", ["gold", "silver"])],
    )
    assert resp.status_code == 422
    assert "region" in resp.json()["detail"]
    assert "未覆盖" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 依赖链
# ---------------------------------------------------------------------------


def _chain_config(base_threshold):
    """top --(须 m_on)--> mid --> base（非默认变体即满足）。

    base：level >= base_threshold 才解析到 b_on；
    mid：base 依赖满足且 tier=gold 才解析到 m_on；
    top：mid 恰为 m_on 后走无条件规则 100% a_on。
    """
    return FlagConfig(
        switches=[
            Switch(
                id="top",
                default_variant="a_off",
                variants=[variant("a_on"), variant("a_off")],
                dependencies=[Dependency(switch="mid", variant="m_on")],
                rules=[rule("always", [], [alloc("a_on", 10_000)])],
            ),
            Switch(
                id="mid",
                default_variant="m_off",
                variants=[variant("m_on"), variant("m_off")],
                dependencies=[Dependency(switch="base")],
                rules=[
                    rule(
                        "gold",
                        [cond("tier", "eq", "gold")],
                        [alloc("m_on", 10_000)],
                    )
                ],
            ),
            Switch(
                id="base",
                default_variant="b_off",
                variants=[variant("b_on"), variant("b_off")],
                rules=[
                    rule(
                        "level_rule",
                        [cond("level", "gte", base_threshold)],
                        [alloc("b_on", 10_000)],
                    )
                ],
            ),
        ]
    )


def test_minimize_dependency_chain_removable_attribute(client):
    config = _chain_config(base_threshold=0)
    resp = _post(
        client,
        config,
        "top",
        context={"level": 20, "tier": "gold"},
        domains=[
            _domain("level", [5, 20]),
            _domain("tier", ["gold", "free"]),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["baseline_variant_id"] == "a_on"
    assert body["baseline_served_default"] is False
    # base 规则 level>=0 恒成立：传递依赖引用的 level 被正确识别为可移除。
    assert body["kept_attributes"] == ["tier"]
    assert body["removed_attributes"] == ["level"]
    # k=0：(5,gold)✓、(5,free)✗ -> 2；
    # k=1：{level}：gold✓/free✗ -> 2；{tier}：5✓/20✓ -> 2。
    assert body["checked_combinations"] == 6


def test_minimize_dependency_chain_necessary_attribute(client):
    config = _chain_config(base_threshold=10)
    resp = _post(
        client,
        config,
        "top",
        context={"level": 20, "tier": "gold"},
        domains=[
            _domain("level", [5, 20]),
            _domain("tier", ["gold", "free"])],
    )
    assert resp.status_code == 200
    body = resp.json()
    # level=5 会沿依赖链把 top 打回默认：两个属性都必须保留，无属性可移除。
    assert body["kept_attributes"] == ["level", "tier"]
    assert body["removed_attributes"] == []
    # k=0：1 短路；k=1：{level} 2、{tier} 1 短路；k=2：空移除集合 1。
    assert body["checked_combinations"] == 5


def test_minimize_uncovered_dependency_attribute(client):
    # 只给 mid 的 tier 声明域、漏了 base 引用的 level：传递闭包覆盖校验。
    config = _chain_config(base_threshold=10)
    resp = _post(
        client,
        config,
        "top",
        context={"level": 20, "tier": "gold"},
        domains=[_domain("tier", ["gold", "free"])],
    )
    assert resp.status_code == 422
    assert "level" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# exists 缺失态
# ---------------------------------------------------------------------------


def test_minimize_exists_missing_state(client):
    # r1：token 不存在 -> 100% off（规则命中，served_default=False）；
    # r2：无条件兜底 -> on。基准 context 中无 token。
    config = FlagConfig(
        switches=[
            Switch(
                id="s",
                default_variant="off",
                variants=[variant("on"), variant("off")],
                rules=[
                    rule(
                        "no_token",
                        [cond("token", "exists", False)],
                        [alloc("off", 10_000)],
                    ),
                    rule("fallback", [], [alloc("on", 10_000)]),
                ],
            )
        ]
    )
    resp = _post(
        client,
        config,
        "s",
        context={},
        domains=[_domain("token", ["x", "y"], allow_missing=True)],
    )
    assert resp.status_code == 200
    body = resp.json()
    # 注意 off 是规则选出的（不是默认变体）：triple 比较必须含 served_default。
    assert body["baseline_variant_id"] == "off"
    assert body["baseline_served_default"] is False
    assert body["baseline_reason"] is None
    assert body["kept_attributes"] == ["token"]
    assert body["removed_attributes"] == []
    # k=0：首个候选值 "x" 即存在 -> on，1 短路；k=1：空移除集合重评 1 次。
    assert body["checked_combinations"] == 2


def test_minimize_exists_missing_not_allowed_rejected(client):
    # context 中无 token 且域不允许缺失：当前 context 自身不在域覆盖范围内。
    config = FlagConfig(
        switches=[
            Switch(
                id="s",
                default_variant="off",
                variants=[variant("on"), variant("off")],
                rules=[
                    rule(
                        "has_token",
                        [cond("token", "exists", True)],
                        [alloc("on", 10_000)],
                    )
                ],
            )
        ]
    )
    resp = _post(
        client,
        config,
        "s",
        context={},
        domains=[_domain("token", ["x"], allow_missing=False)],
    )
    assert resp.status_code == 422
    assert "缺失" in str(resp.json()["detail"])


# ---------------------------------------------------------------------------
# 并列最小集合
# ---------------------------------------------------------------------------


def test_minimize_tied_minimal_sets_lexicographic(client):
    # r1：tier=gold AND country=CN -> on；
    # r2：region=north AND locale=zh -> on；否则默认 off。
    # 基准四属性皆满足：{country,tier} 与 {locale,region} 同为最小充分，
    # 按属性数、属性名字典序枚举取前者。
    config = FlagConfig(
        switches=[
            Switch(
                id="s",
                default_variant="off",
                variants=[variant("on"), variant("off")],
                rules=[
                    rule(
                        "r1",
                        [
                            cond("tier", "eq", "gold"),
                            cond("country", "eq", "CN"),
                        ],
                        [alloc("on", 10_000)],
                    ),
                    rule(
                        "r2",
                        [
                            cond("region", "eq", "north"),
                            cond("locale", "eq", "zh"),
                        ],
                        [alloc("on", 10_000)],
                    ),
                ],
            )
        ]
    )
    resp = _post(
        client,
        config,
        "s",
        context={
            "tier": "gold",
            "country": "CN",
            "region": "north",
            "locale": "zh",
        },
        domains=[
            _domain("tier", ["gold", "free"]),
            _domain("country", ["CN", "US"]),
            _domain("region", ["north", "south"]),
            _domain("locale", ["zh", "en"]),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["baseline_variant_id"] == "on"
    assert body["kept_attributes"] == ["country", "tier"]
    assert body["removed_attributes"] == ["locale", "region"]
    # 逐集合计数：k=0:4；k=1:4+4+4+6=18；k=2 第三个组合 {country,tier}:4。
    assert body["checked_combinations"] == 34


# ---------------------------------------------------------------------------
# reason 必须一致（variant 相同、served_default 相同也不充分）
# ---------------------------------------------------------------------------


def test_minimize_reason_mismatch_is_not_sufficient(client):
    # base：z_level>=10 -> b_on；top 依赖 base 非默认，另有 tier=gold 规则。
    # 基准 z_level=5、tier=free：top -> a_off / dependency_not_satisfied。
    config = FlagConfig(
        switches=[
            Switch(
                id="top",
                default_variant="a_off",
                variants=[variant("a_on"), variant("a_off")],
                dependencies=[Dependency(switch="base")],
                rules=[
                    rule(
                        "gold",
                        [cond("tier", "eq", "gold")],
                        [alloc("a_on", 10_000)],
                    )
                ],
            ),
            Switch(
                id="base",
                default_variant="b_off",
                variants=[variant("b_on"), variant("b_off")],
                rules=[
                    rule(
                        "level_rule",
                        [cond("z_level", "gte", 10)],
                        [alloc("b_on", 10_000)],
                    )
                ],
            ),
        ]
    )
    resp = _post(
        client,
        config,
        "top",
        context={"tier": "free", "z_level": 5},
        domains=[
            _domain("tier", ["gold", "free"]),
            _domain("z_level", [5, 20]),
        ],
    )
    body = resp.json()
    assert body["baseline_variant_id"] == "a_off"
    assert body["baseline_served_default"] is True
    assert body["baseline_reason"] == "dependency_not_satisfied"
    # 保留 {tier} 时 z_level=20 会使 top 变成 a_off / no_rule_matched：
    # variant_id 与 served_default 均相同，仅 reason 不同 -> 不充分，
    # 因此枚举继续，最小集合为 {z_level}。
    assert body["kept_attributes"] == ["z_level"]
    assert body["removed_attributes"] == ["tier"]
    # k=0：2 短路；k=1：{tier} 2（第 2 个因 reason 不同失败）、{z_level} 2。
    assert body["checked_combinations"] == 6


# ---------------------------------------------------------------------------
# 422 / 404 拒绝
# ---------------------------------------------------------------------------


def test_minimize_rejects_uncovered_attribute(client):
    config = _single_rule_config()
    resp = _post(
        client,
        config,
        "s",
        context={"tier": "gold"},
        domains=[],
    )
    assert resp.status_code == 422
    assert "tier" in resp.json()["detail"]
    assert "未覆盖" in resp.json()["detail"]


def test_minimize_rejects_extra_domain(client):
    config = _single_rule_config()
    resp = _post(
        client,
        config,
        "s",
        # age 出现在 context 中、值域也合法，但没有任何规则引用它：
        # 域必须恰好覆盖被引用属性，多声明同样拒绝。
        context={"tier": "gold", "age": 18},
        domains=[
            _domain("tier", ["gold", "silver"]),
            _domain("age", [18, 30]),
        ],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "age" in detail
    assert "未被" in detail


def test_minimize_rejects_duplicate_domain(client):
    config = _single_rule_config()
    resp = _post(
        client,
        config,
        "s",
        context={"tier": "gold"},
        domains=[
            _domain("tier", ["gold"]),
            _domain("tier", ["silver"]),
        ],
    )
    assert resp.status_code == 422
    assert "重复" in str(resp.json()["detail"])


def test_minimize_rejects_empty_domain(client):
    config = _single_rule_config()
    resp = _post(
        client,
        config,
        "s",
        context={"tier": "gold"},
        domains=[_domain("tier", [], allow_missing=False)],
    )
    assert resp.status_code == 422
    assert "为空" in str(resp.json()["detail"])


def test_minimize_rejects_duplicate_domain_values(client):
    config = _single_rule_config()
    resp = _post(
        client,
        config,
        "s",
        context={"tier": "gold"},
        domains=[_domain("tier", ["gold", "gold"])],
    )
    assert resp.status_code == 422
    assert "候选值重复" in str(resp.json()["detail"])


def test_minimize_rejects_value_outside_domain(client):
    config = _single_rule_config()
    resp = _post(
        client,
        config,
        "s",
        context={"tier": "platinum"},
        domains=[_domain("tier", ["gold", "silver"])],
    )
    assert resp.status_code == 422
    assert "不在其域内" in str(resp.json()["detail"])


def test_minimize_rejects_combination_overflow(client):
    # 13 个二元属性 -> 8192 组合，超过 4096 上限（模型阶段即 422）。
    attrs = [chr(ord("a") + i) for i in range(13)]
    config = FlagConfig(
        switches=[
            Switch(
                id="s",
                default_variant="off",
                variants=[variant("on"), variant("off")],
                rules=[
                    rule(
                        "all",
                        [cond(a, "eq", 1) for a in attrs],
                        [alloc("on", 10_000)],
                    )
                ],
            )
        ]
    )
    resp = _post(
        client,
        config,
        "s",
        context={a: 1 for a in attrs},
        domains=[_domain(a, [0, 1]) for a in attrs],
    )
    assert resp.status_code == 422
    detail = str(resp.json()["detail"])
    assert "8192" in detail
    assert "4096" in detail


def _boundary_config():
    # 12 个二元属性 -> 4096 组合，恰好等于上限。
    attrs = [chr(ord("a") + i) for i in range(12)]
    return attrs, FlagConfig(
        switches=[
            Switch(
                id="s",
                default_variant="off",
                variants=[variant("on"), variant("off")],
                rules=[
                    rule(
                        "first",
                        [cond("a", "eq", 1)],
                        [alloc("on", 10_000)],
                    ),
                    rule(
                        # 基准下被首条规则遮蔽，但令其余 11 个属性均被引用。
                        "shadow",
                        [cond(a, "eq", 1) for a in attrs if a != "a"],
                        [alloc("off", 10_000)],
                    ),
                ],
            )
        ]
    )


def test_minimize_accepts_combination_limit_boundary(client):
    attrs, config = _boundary_config()
    resp = _post(
        client,
        config,
        "s",
        context={a: 1 for a in attrs},
        domains=[_domain(a, [0, 1]) for a in attrs],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kept_attributes"] == ["a"]
    assert body["removed_attributes"] == [a for a in attrs if a != "a"]
    # k=0：全 0 组合 1 短路；k=1：字典序首个保留集 {a}，移除 11 属性 2^11。
    assert body["checked_combinations"] == 1 + 2**11


def test_minimize_rejects_invalid_config(client):
    config = _single_rule_config()
    config.switches[0].rules[0].allocations[0].weight = 1
    resp = _post(
        client,
        config,
        "s",
        context={"tier": "gold"},
        domains=[_domain("tier", ["gold", "silver"])],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "校验未通过" in detail["message"]
    assert any(e["code"] == "invalid_weight_sum" for e in detail["errors"])


def test_minimize_unknown_switch_returns_404(client):
    config = _single_rule_config()
    resp = _post(
        client,
        config,
        "ghost",
        context={},
        domains=[],
    )
    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]


def test_minimize_json_value_canonical_equality(client):
    # 当前值键序与域候选值不同，按规范化 JSON 判等视为同值并被接受。
    config = FlagConfig(
        switches=[
            Switch(
                id="s",
                default_variant="off",
                variants=[variant("on"), variant("off")],
                rules=[
                    rule(
                        "meta",
                        [cond("meta", "eq", {"a": 1, "b": 2})],
                        [alloc("on", 10_000)],
                    )
                ],
            )
        ]
    )
    resp = _post(
        client,
        config,
        "s",
        context={"meta": {"b": 2, "a": 1}},
        domains=[_domain("meta", [{"a": 1, "b": 2}])],
    )
    assert resp.status_code == 200
    body = resp.json()
    # 域内只有一种状态：移除 meta 后重评仍等于基准，空集合即最小充分。
    assert body["kept_attributes"] == []
    assert body["removed_attributes"] == ["meta"]
    assert body["checked_combinations"] == 1


def test_minimize_rejects_extra_request_field(client):
    config = _single_rule_config()
    resp = client.post(
        MIN_URL,
        json={
            "config": config.model_dump(mode="json"),
            "switch_id": "s",
            "user_id": "u",
            "context": {"tier": "gold"},
            "domains": [_domain("tier", ["gold"])],
            "unexpected": True,
        },
    )
    assert resp.status_code == 422


def test_minimize_requires_user_id(client):
    config = _single_rule_config()
    resp = client.post(
        MIN_URL,
        json={
            "config": config.model_dump(mode="json"),
            "switch_id": "s",
            "context": {"tier": "gold"},
            "domains": [_domain("tier", ["gold"])],
        },
    )
    assert resp.status_code == 422
