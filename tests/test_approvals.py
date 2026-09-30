"""多人审批测试：事件折叠、否决、分组配额分配、字典序最小方案与请求校验。"""

from __future__ import annotations

import pytest

from app.approvals import evaluate_approvals
from app.models import (
    ApprovalAction,
    ApprovalEvaluateRequest,
    ApprovalEvent,
    ApprovalGroup,
    ApprovalPerson,
    ApprovalStatus,
)

URL = "/approvals/evaluate"
DIGEST = "digest-abc"
ROLES = ["dev", "ops", "sec"]


def person(id: str, roles: list[str] | None = None) -> ApprovalPerson:
    return ApprovalPerson(id=id, roles=roles or [])


def group(id: str, roles: list[str], min_approvals: int = 1) -> ApprovalGroup:
    return ApprovalGroup(id=id, allowed_roles=roles, min_approvals=min_approvals)


def event(seq: int, person_id: str, action: ApprovalAction) -> ApprovalEvent:
    return ApprovalEvent(
        sequence=seq, person=person_id, action=action, artifact_digest=DIGEST
    )


def approve(seq: int, person_id: str) -> ApprovalEvent:
    return event(seq, person_id, ApprovalAction.APPROVE)


def reject(seq: int, person_id: str) -> ApprovalEvent:
    return event(seq, person_id, ApprovalAction.REJECT)


def revoke(seq: int, person_id: str) -> ApprovalEvent:
    return event(seq, person_id, ApprovalAction.REVOKE)


def make_request(
    people: list[ApprovalPerson] | None = None,
    groups: list[ApprovalGroup] | None = None,
    events: list[ApprovalEvent] | None = None,
    roles: list[str] | None = None,
    veto_roles: list[str] | None = None,
    digest: str = DIGEST,
) -> ApprovalEvaluateRequest:
    return ApprovalEvaluateRequest(
        artifact_digest=digest,
        roles=ROLES if roles is None else roles,
        people=people or [],
        groups=groups or [],
        veto_roles=veto_roles or [],
        events=events or [],
    )


def payload(request: ApprovalEvaluateRequest) -> dict:
    return request.model_dump(mode="json")


# ---------------------------------------------------------------------------
# 事件折叠
# ---------------------------------------------------------------------------


def test_latest_event_overrides_earlier():
    req = make_request(
        people=[person("alice", ["dev"]), person("bob", ["dev"]), person("carol", ["dev"])],
        groups=[group("g1", ["dev"], 2)],
        events=[
            approve(1, "alice"),
            approve(2, "bob"),
            reject(3, "alice"),  # alice 改票：approve 被 reject 覆盖
            revoke(4, "bob"),  # bob 撤票：不再持有活动票
            approve(5, "carol"),
        ],
    )
    resp = evaluate_approvals(req)
    votes = {v.person: (v.action, v.sequence) for v in resp.active_votes}
    assert votes == {
        "alice": (ApprovalAction.REJECT, 3),
        "carol": (ApprovalAction.APPROVE, 5),
    }
    assert resp.status == ApprovalStatus.PENDING
    (g1,) = resp.groups
    assert g1.counted == ["carol"]
    assert g1.shortfall == 1


def test_revoke_without_prior_vote_is_harmless():
    req = make_request(
        people=[person("alice", ["dev"])],
        groups=[group("g1", ["dev"])],
        events=[revoke(1, "alice")],
    )
    resp = evaluate_approvals(req)
    assert resp.active_votes == []
    assert resp.status == ApprovalStatus.PENDING


def test_reapprove_after_revoke_counts_again():
    req = make_request(
        people=[person("alice", ["dev"])],
        groups=[group("g1", ["dev"])],
        events=[approve(1, "alice"), revoke(2, "alice"), approve(3, "alice")],
    )
    resp = evaluate_approvals(req)
    assert [(v.person, v.action, v.sequence) for v in resp.active_votes] == [
        ("alice", ApprovalAction.APPROVE, 3)
    ]
    assert resp.status == ApprovalStatus.APPROVED


# ---------------------------------------------------------------------------
# 否决
# ---------------------------------------------------------------------------


def test_veto_reject_blocks_even_when_quorum_met():
    req = make_request(
        people=[
            person("alice", ["dev"]),
            person("bob", ["dev"]),
            person("carol", ["sec"]),
        ],
        groups=[group("g1", ["dev"], 2)],
        veto_roles=["sec"],
        events=[approve(1, "alice"), approve(2, "bob"), reject(3, "carol")],
    )
    resp = evaluate_approvals(req)
    assert resp.status == ApprovalStatus.BLOCKED
    assert [(v.person, v.roles, v.sequence) for v in resp.vetoes] == [
        ("carol", ["sec"], 3)
    ]
    # 分组计入结果照常返回，便于展示当前进度。
    assert resp.groups[0].counted == ["alice", "bob"]
    assert resp.groups[0].shortfall == 0


def test_revoked_veto_reject_does_not_block():
    req = make_request(
        people=[person("alice", ["dev"]), person("carol", ["sec"])],
        groups=[group("g1", ["dev"])],
        veto_roles=["sec"],
        events=[approve(1, "alice"), reject(2, "carol"), revoke(3, "carol")],
    )
    resp = evaluate_approvals(req)
    assert resp.status == ApprovalStatus.APPROVED
    assert resp.vetoes == []


def test_reject_from_non_veto_role_does_not_block():
    req = make_request(
        people=[person("alice", ["dev"]), person("bob", ["dev"])],
        groups=[group("g1", ["dev"])],
        veto_roles=["sec"],
        events=[approve(1, "alice"), reject(2, "bob")],
    )
    resp = evaluate_approvals(req)
    assert resp.status == ApprovalStatus.APPROVED
    assert resp.vetoes == []
    # bob 的 reject 仍是活动票，只是不构成否决、也不计入赞成。
    assert [(v.person, v.action) for v in resp.active_votes] == [
        ("alice", ApprovalAction.APPROVE),
        ("bob", ApprovalAction.REJECT),
    ]


def test_veto_holder_latest_approve_clears_veto():
    req = make_request(
        people=[person("alice", ["dev"]), person("carol", ["sec", "dev"])],
        groups=[group("g1", ["dev"], 2)],
        veto_roles=["sec"],
        events=[approve(1, "alice"), reject(2, "carol"), approve(3, "carol")],
    )
    resp = evaluate_approvals(req)
    assert resp.status == ApprovalStatus.APPROVED
    assert resp.vetoes == []
    assert resp.groups[0].counted == ["alice", "carol"]


def test_veto_evidence_lists_only_veto_roles():
    req = make_request(
        people=[person("carol", ["sec", "ops"])],
        groups=[group("g1", ["dev"])],
        veto_roles=["sec"],
        events=[reject(1, "carol")],
    )
    resp = evaluate_approvals(req)
    assert resp.status == ApprovalStatus.BLOCKED
    assert resp.vetoes[0].roles == ["sec"]


# ---------------------------------------------------------------------------
# 分组配额与分配
# ---------------------------------------------------------------------------


def test_approved_when_all_groups_reach_quorum():
    req = make_request(
        people=[
            person("alice", ["dev"]),
            person("bob", ["dev"]),
            person("carol", ["ops"]),
        ],
        groups=[group("g1", ["dev"], 2), group("g2", ["ops"], 1)],
        events=[approve(1, "bob"), approve(2, "alice"), approve(3, "carol")],
    )
    resp = evaluate_approvals(req)
    assert resp.status == ApprovalStatus.APPROVED
    assert [(g.id, g.counted, g.shortfall) for g in resp.groups] == [
        ("g1", ["alice", "bob"], 0),
        ("g2", ["carol"], 0),
    ]


def test_pending_when_quorum_not_met():
    req = make_request(
        people=[person("alice", ["dev"])],
        groups=[group("g1", ["dev"], 2)],
        events=[approve(1, "alice")],
    )
    resp = evaluate_approvals(req)
    assert resp.status == ApprovalStatus.PENDING
    assert resp.groups[0].counted == ["alice"]
    assert resp.groups[0].shortfall == 1


def test_approver_without_allowed_role_does_not_count():
    req = make_request(
        people=[person("alice", ["dev"])],
        groups=[group("g1", ["ops"])],
        events=[approve(1, "alice")],
    )
    resp = evaluate_approvals(req)
    assert resp.status == ApprovalStatus.PENDING
    assert resp.groups[0].counted == []
    assert resp.groups[0].shortfall == 1
    # 票仍然有效，只是没有可计入的组。
    assert [(v.person, v.action) for v in resp.active_votes] == [
        ("alice", ApprovalAction.APPROVE)
    ]


def test_same_person_counts_in_at_most_one_group():
    # alice 同时满足两组角色，但只能计入一组，无法让两组同时达标。
    req = make_request(
        people=[person("alice", ["dev", "ops"])],
        groups=[group("g1", ["dev"]), group("g2", ["ops"])],
        events=[approve(1, "alice")],
    )
    resp = evaluate_approvals(req)
    assert resp.status == ApprovalStatus.PENDING
    assert [(g.id, g.counted, g.shortfall) for g in resp.groups] == [
        ("g1", ["alice"], 0),
        ("g2", [], 1),
    ]


def test_shared_approver_unblocks_when_second_approver_arrives():
    req = make_request(
        people=[person("alice", ["dev", "ops"]), person("bob", ["ops"])],
        groups=[group("g1", ["dev"]), group("g2", ["ops"])],
        events=[approve(1, "alice"), approve(2, "bob")],
    )
    resp = evaluate_approvals(req)
    assert resp.status == ApprovalStatus.APPROVED
    assert [(g.id, g.counted) for g in resp.groups] == [
        ("g1", ["alice"]),
        ("g2", ["bob"]),
    ]


def test_lexicographically_smallest_assignment_picks_smallest_person():
    req = make_request(
        people=[person("alice", ["dev"]), person("bob", ["dev"])],
        groups=[group("g1", ["dev"])],
        events=[approve(1, "bob"), approve(2, "alice")],
    )
    resp = evaluate_approvals(req)
    assert resp.status == ApprovalStatus.APPROVED
    assert resp.groups[0].counted == ["alice"]


def test_lexicographic_assignment_skips_person_needed_elsewhere():
    # alice 同时可计入两组；若计入 g1 则 g2 无人可补，字典序最小方案须跳过她。
    req = make_request(
        people=[person("alice", ["dev", "ops"]), person("carol", ["dev"])],
        groups=[group("g1", ["dev"]), group("g2", ["ops"])],
        events=[approve(1, "alice"), approve(2, "carol")],
    )
    resp = evaluate_approvals(req)
    assert resp.status == ApprovalStatus.APPROVED
    assert [(g.id, g.counted) for g in resp.groups] == [
        ("g1", ["carol"]),
        ("g2", ["alice"]),
    ]


def test_pending_assignment_maximizes_then_orders_by_group():
    # 无法全部达标时，先最大化计入总人数，再按 (组 ID, 人员 ID) 取字典序最小：
    # x、y 都计入靠前的 g1，g2 留缺口。
    req = make_request(
        people=[person("x", ["dev", "ops"]), person("y", ["dev"])],
        groups=[group("g1", ["dev"], 2), group("g2", ["ops"])],
        events=[approve(1, "x"), approve(2, "y")],
    )
    resp = evaluate_approvals(req)
    assert resp.status == ApprovalStatus.PENDING
    assert [(g.id, g.counted, g.shortfall) for g in resp.groups] == [
        ("g1", ["x", "y"], 0),
        ("g2", [], 1),
    ]


def test_groups_sorted_by_id_and_votes_sorted_by_person():
    req = make_request(
        people=[person("zoe", ["dev"]), person("amy", ["ops"])],
        groups=[group("g2", ["dev"]), group("g1", ["ops"])],
        events=[approve(1, "zoe"), approve(2, "amy")],
    )
    resp = evaluate_approvals(req)
    assert resp.status == ApprovalStatus.APPROVED
    assert [g.id for g in resp.groups] == ["g1", "g2"]
    assert [v.person for v in resp.active_votes] == ["amy", "zoe"]


def test_empty_events_is_pending():
    req = make_request(
        people=[person("alice", ["dev"])],
        groups=[group("g1", ["dev"])],
    )
    resp = evaluate_approvals(req)
    assert resp.status == ApprovalStatus.PENDING
    assert resp.active_votes == []
    assert resp.groups[0].shortfall == 1


def test_no_groups_is_vacuously_approved():
    req = make_request(people=[person("alice", ["dev"])], events=[approve(1, "alice")])
    resp = evaluate_approvals(req)
    assert resp.status == ApprovalStatus.APPROVED
    assert resp.groups == []


# ---------------------------------------------------------------------------
# 请求校验（模型层 422）
# ---------------------------------------------------------------------------


def test_duplicate_person_rejected():
    with pytest.raises(ValueError, match="人员 ID 重复"):
        make_request(people=[person("alice"), person("alice")])


def test_duplicate_group_rejected():
    with pytest.raises(ValueError, match="审批组 ID 重复"):
        make_request(groups=[group("g1", ["dev"]), group("g1", ["ops"])])


def test_duplicate_sequence_rejected():
    with pytest.raises(ValueError, match="严格递增"):
        make_request(
            people=[person("alice"), person("bob")],
            events=[approve(1, "alice"), approve(1, "bob")],
        )


def test_out_of_order_sequence_rejected():
    with pytest.raises(ValueError, match="严格递增"):
        make_request(
            people=[person("alice"), person("bob")],
            events=[approve(2, "alice"), approve(1, "bob")],
        )


def test_unknown_person_role_rejected():
    with pytest.raises(ValueError, match="未知角色"):
        make_request(people=[person("alice", ["ghost"])])


def test_unknown_group_role_rejected():
    with pytest.raises(ValueError, match="未知角色"):
        make_request(groups=[group("g1", ["ghost"])])


def test_unknown_veto_role_rejected():
    with pytest.raises(ValueError, match="否决角色"):
        make_request(veto_roles=["ghost"])


def test_unknown_event_person_rejected():
    with pytest.raises(ValueError, match="未知人员"):
        make_request(people=[person("alice")], events=[approve(1, "ghost")])


def test_event_digest_mismatch_rejected():
    req_event = ApprovalEvent(
        sequence=1,
        person="alice",
        action=ApprovalAction.APPROVE,
        artifact_digest="other-digest",
    )
    with pytest.raises(ValueError, match="artifact_digest"):
        make_request(people=[person("alice")], events=[req_event])


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------


def test_api_happy_path(client):
    req = make_request(
        people=[person("alice", ["dev"]), person("bob", ["dev"])],
        groups=[group("g1", ["dev"], 2)],
        events=[approve(1, "alice"), approve(2, "bob")],
    )
    resp = client.post(URL, json=payload(req))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["artifact_digest"] == DIGEST
    assert body["groups"] == [
        {"id": "g1", "min_approvals": 2, "counted": ["alice", "bob"], "shortfall": 0}
    ]
    assert body["active_votes"] == [
        {"person": "alice", "action": "approve", "sequence": 1},
        {"person": "bob", "action": "approve", "sequence": 2},
    ]
    assert body["vetoes"] == []


def test_api_blocked_response(client):
    req = make_request(
        people=[person("alice", ["dev"]), person("carol", ["sec"])],
        groups=[group("g1", ["dev"])],
        veto_roles=["sec"],
        events=[approve(1, "alice"), reject(2, "carol")],
    )
    resp = client.post(URL, json=payload(req))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "blocked"
    assert body["vetoes"] == [{"person": "carol", "roles": ["sec"], "sequence": 2}]


def test_api_pending_response(client):
    req = make_request(
        people=[person("alice", ["dev"])],
        groups=[group("g1", ["dev"], 2)],
        events=[approve(1, "alice")],
    )
    resp = client.post(URL, json=payload(req))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["groups"][0]["shortfall"] == 1


def test_api_rejects_duplicate_person(client):
    body = payload(make_request(people=[person("alice")]))
    body["people"].append({"id": "alice", "roles": []})
    assert client.post(URL, json=body).status_code == 422


def test_api_rejects_duplicate_group(client):
    body = payload(make_request(groups=[group("g1", ["dev"])]))
    body["groups"].append({"id": "g1", "allowed_roles": ["ops"], "min_approvals": 1})
    assert client.post(URL, json=body).status_code == 422


def test_api_rejects_duplicate_and_unordered_sequence(client):
    people = [person("alice"), person("bob")]
    body = payload(make_request(people=people, events=[approve(1, "alice"), approve(2, "bob")]))
    body["events"][1]["sequence"] = 1  # 重复 sequence
    assert client.post(URL, json=body).status_code == 422

    body = payload(make_request(people=people, events=[approve(1, "alice"), approve(2, "bob")]))
    body["events"][0]["sequence"] = 2
    body["events"][1]["sequence"] = 1  # 乱序
    assert client.post(URL, json=body).status_code == 422


def test_api_rejects_unknown_references(client):
    body = payload(make_request(people=[person("alice", ["dev"])]))
    body["people"][0]["roles"] = ["ghost"]
    assert client.post(URL, json=body).status_code == 422

    body = payload(make_request(groups=[group("g1", ["dev"])]))
    body["groups"][0]["allowed_roles"] = ["ghost"]
    assert client.post(URL, json=body).status_code == 422

    body = payload(make_request())
    body["veto_roles"] = ["ghost"]
    assert client.post(URL, json=body).status_code == 422

    body = payload(make_request(people=[person("alice")], events=[approve(1, "alice")]))
    body["events"][0]["person"] = "ghost"
    assert client.post(URL, json=body).status_code == 422


def test_api_rejects_event_digest_mismatch(client):
    body = payload(make_request(people=[person("alice")], events=[approve(1, "alice")]))
    body["events"][0]["artifact_digest"] = "another-digest"
    assert client.post(URL, json=body).status_code == 422


@pytest.mark.parametrize("min_approvals", [0, -1])
def test_api_rejects_invalid_threshold(client, min_approvals):
    body = payload(make_request(groups=[group("g1", ["dev"])]))
    body["groups"][0]["min_approvals"] = min_approvals
    assert client.post(URL, json=body).status_code == 422


def test_api_rejects_empty_allowed_roles_and_bad_action(client):
    body = payload(make_request(groups=[group("g1", ["dev"])]))
    body["groups"][0]["allowed_roles"] = []
    assert client.post(URL, json=body).status_code == 422

    body = payload(make_request(people=[person("alice")], events=[approve(1, "alice")]))
    body["events"][0]["action"] = "comment"
    assert client.post(URL, json=body).status_code == 422


def test_api_rejects_extra_fields(client):
    body = payload(make_request())
    body["surprise"] = True
    assert client.post(URL, json=body).status_code == 422
