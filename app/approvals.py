"""多人审批：折叠审批事件流，判定配置发布是否满足分组配额。

语义概述：

- 事件按 sequence 严格递增到达（请求校验保证），依序折叠：每人的最新
  事件覆盖旧事件，revoke 清除本人此前一票；折叠后仍有效的
  approve/reject 称为“活动票”；
- 否决：任一持有否决角色的人员存在有效 reject → 直接 blocked；
- 否则把有效 approve 分配给审批组：人员须持有组的允许角色方可计入，
  同一人最多计入一组，判断所有组能否同时达到 min_approvals；
- 分配方案：在所有“计入总人数最多”的分配中，取按 (组 ID, 人员 ID)
  字典序最小者——按组 ID 升序、人员 ID 升序逐对贪心，仅当剩余人员
  仍能补到最大总人数（最大流）时才固定该对。全部组达标 → approved，
  否则 → pending。
"""

from __future__ import annotations

from collections import deque

from app.models import (
    ActiveVote,
    ApprovalAction,
    ApprovalEvaluateRequest,
    ApprovalEvaluateResponse,
    ApprovalGroup,
    ApprovalGroupResult,
    ApprovalStatus,
    VetoEvidence,
)

# 最大流节点：源点、汇点、人员节点与组节点用带前缀的二元组区分，避免与 ID 冲突。
_SRC = ("src",)
_SNK = ("snk",)


def _node_person(person: str) -> tuple[str, str]:
    return ("person", person)


def _node_group(group_id: str) -> tuple[str, str]:
    return ("group", group_id)


def _max_flow(
    people: list[str],
    group_caps: dict[str, int],
    eligibility: dict[str, set[str]],
) -> int:
    """二分图最大流：源→人(1)、人→可计入的组(1)、组→汇(剩余缺口)。

    所有源边容量为 1，每次 BFS 增广恰好推进 1 单位流量。
    """
    capacity: dict[tuple, dict[tuple, int]] = {}

    def add_edge(u: tuple, v: tuple, cap: int) -> None:
        capacity.setdefault(u, {}).setdefault(v, 0)
        capacity.setdefault(v, {}).setdefault(u, 0)
        capacity[u][v] += cap

    for person in people:
        add_edge(_SRC, _node_person(person), 1)
        for group_id in eligibility.get(person, ()):
            add_edge(_node_person(person), _node_group(group_id), 1)
    for group_id, cap in group_caps.items():
        if cap > 0:
            add_edge(_node_group(group_id), _SNK, cap)

    flow = 0
    while True:
        parent: dict[tuple, tuple | None] = {_SRC: None}
        queue = deque([_SRC])
        while queue and _SNK not in parent:
            u = queue.popleft()
            for v, residual in capacity.get(u, {}).items():
                if residual > 0 and v not in parent:
                    parent[v] = u
                    queue.append(v)
        if _SNK not in parent:
            return flow
        node = _SNK
        while parent[node] is not None:
            prev = parent[node]
            capacity[prev][node] -= 1
            capacity[node][prev] += 1
            node = prev
        flow += 1


def _assign(
    approvers: list[str],
    groups: list[ApprovalGroup],
    eligibility: dict[str, set[str]],
) -> dict[str, list[str]]:
    """把有效赞成者分配给审批组，返回 组 ID -> 计入人员（升序）。

    先以最大流求出可计入的最大总人数 target（等于各组 min_approvals
    之和时即全部达标）；再按 (组 ID, 人员 ID) 升序逐对贪心：仅当固定
    该对后剩余实例仍能补到 target 时才计入，从而得到字典序最小方案。
    """
    demands = {group.id: group.min_approvals for group in groups}
    target = _max_flow(approvers, demands, eligibility)

    remaining_people = sorted(approvers)
    remaining_caps = dict(demands)
    assignment: dict[str, list[str]] = {group.id: [] for group in groups}
    assigned = 0

    for group in sorted(groups, key=lambda g: g.id):
        group_id = group.id
        for person in sorted(remaining_people):
            if remaining_caps[group_id] == 0:
                break
            if person not in remaining_people:
                continue
            if group_id not in eligibility.get(person, set()):
                continue
            # 试把 person 计入本组：剩余人员与缺口必须仍能补到 target。
            rest_people = [p for p in remaining_people if p != person]
            rest_caps = dict(remaining_caps)
            rest_caps[group_id] -= 1
            if _max_flow(rest_people, rest_caps, eligibility) == target - assigned - 1:
                assignment[group_id].append(person)
                remaining_people = rest_people
                remaining_caps = rest_caps
                assigned += 1
    return assignment


def evaluate_approvals(request: ApprovalEvaluateRequest) -> ApprovalEvaluateResponse:
    """折叠事件流并给出发布审批总状态与各组计入情况。"""
    roles_by_person = {p.id: set(p.roles) for p in request.people}

    # 依序折叠：每人的最新事件覆盖旧事件；revoke 使本人不再持有活动票。
    latest: dict[str, ApprovalAction] = {}
    sequence_of: dict[str, int] = {}
    for event in request.events:
        latest[event.person] = event.action
        sequence_of[event.person] = event.sequence
    active = {
        person: action
        for person, action in latest.items()
        if action != ApprovalAction.REVOKE
    }

    # 否决证据：持有否决角色且存在有效 reject 的人员。
    veto_roles = set(request.veto_roles)
    vetoes = sorted(
        (
            VetoEvidence(
                person=person,
                roles=sorted(roles_by_person[person] & veto_roles),
                sequence=sequence_of[person],
            )
            for person, action in active.items()
            if action == ApprovalAction.REJECT
            and roles_by_person[person] & veto_roles
        ),
        key=lambda veto: veto.person,
    )

    # 有效赞成者按角色匹配分配到审批组（同一人最多计入一组）。
    approvers = [
        person for person, action in active.items() if action == ApprovalAction.APPROVE
    ]
    eligibility = {
        person: {
            group.id
            for group in request.groups
            if roles_by_person[person] & set(group.allowed_roles)
        }
        for person in approvers
    }
    assignment = _assign(approvers, request.groups, eligibility)

    group_results = [
        ApprovalGroupResult(
            id=group.id,
            min_approvals=group.min_approvals,
            counted=assignment[group.id],
            shortfall=group.min_approvals - len(assignment[group.id]),
        )
        for group in sorted(request.groups, key=lambda g: g.id)
    ]

    active_votes = sorted(
        (
            ActiveVote(
                person=person,
                action=action,
                sequence=sequence_of[person],
            )
            for person, action in active.items()
        ),
        key=lambda vote: vote.person,
    )

    if vetoes:
        status = ApprovalStatus.BLOCKED
    elif all(result.shortfall == 0 for result in group_results):
        status = ApprovalStatus.APPROVED
    else:
        status = ApprovalStatus.PENDING

    return ApprovalEvaluateResponse(
        status=status,
        artifact_digest=request.artifact_digest,
        groups=group_results,
        active_votes=active_votes,
        vetoes=vetoes,
    )
