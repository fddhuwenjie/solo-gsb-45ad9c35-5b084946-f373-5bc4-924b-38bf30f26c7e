"""FastAPI 应用：/validate 校验、/evaluate 评估、/explanations/minimize 最小充分上下文、/guardrails/evaluate 序贯护栏、/approvals/evaluate 多人审批、/experiments/attribute 实验归因、/artifacts 制品签发验签、/release-windows/resolve 发布窗口、/rollouts/rebalance 权重重排、/flags/retirement-plan 开关下线方案、/sdk/rollout-plan 制品分波铺开、/sdk/rollout-audit 铺开执行审计与 /sdk/reconcile 决策日志核对。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from app.approvals import evaluate_approvals
from app.artifacts import load_signing_keys, sign_config, verify_artifact
from app.attribution import attribute_experiment
from app.evaluator import ConfigError, Evaluator
from app.explanations import MinimizeRequestError, minimize_context
from app.guardrails import evaluate_guardrails
from app.models import (
    ApprovalEvaluateRequest,
    ApprovalEvaluateResponse,
    Artifact,
    EvaluateRequest,
    EvaluateResponse,
    ExperimentAttributeRequest,
    ExperimentAttributeResponse,
    FlagConfig,
    GuardrailEvaluateRequest,
    GuardrailEvaluateResponse,
    MinimizeContextRequest,
    MinimizeContextResponse,
    RebalanceRequest,
    RebalanceResponse,
    ReconcileRequest,
    ReconcileResponse,
    ReleaseResolveRequest,
    ReleaseResolveResponse,
    RetirementPlanRequest,
    RetirementPlanResponse,
    RolloutAuditRequest,
    RolloutAuditResponse,
    RolloutPlanRequest,
    RolloutPlanResponse,
    SignRequest,
    SignResponse,
    TraceEntry,
    ValidationResponse,
    VerifyRequest,
    VerifyResponse,
)
from app.rebalance import rebalance
from app.reconcile import ReconcileRequestError, reconcile
from app.release_windows import resolve_release_windows
from app.retirement import RetirementRequestError, build_retirement_plan
from app.rollout import build_rollout_plan
from app.rollout_audit import audit_rollout
from app.validation import validate_config

app = FastAPI(
    title="灰度发布决策 API",
    description=(
        "基于开关、变体、依赖与有序规则的灰度发布决策服务。"
        "提供配置校验、用户级变体评估、最小充分上下文解释、"
        "二项失败率序贯护栏（Wald SPRT）、"
        "配置制品签发/验签、多人审批判定、曝光-转化实验归因、"
        "多地区发布窗口求解、变体权重重排、已关闭开关的下线方案生成"
        "与 SDK 节点决策日志核对。"
    ),
    version="1.0.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/validate", response_model=ValidationResponse)
def validate(config: FlagConfig) -> ValidationResponse:
    """校验依赖图、规则引用与权重总和；拒绝未知依赖和环。"""
    issues = validate_config(config)
    return ValidationResponse(valid=not issues, errors=issues)


@app.post("/evaluate", response_model=EvaluateResponse)
def evaluate(request: EvaluateRequest) -> EvaluateResponse:
    """迭代评估依赖与有序规则，返回最终变体、命中轨迹与失败原因。"""
    # 评估前强制校验：无效配置直接 422，拒绝在坏配置上给出静默结果。
    issues = validate_config(request.config)
    if issues:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "配置校验未通过，无法评估",
                "errors": [issue.model_dump() for issue in issues],
            },
        )

    evaluator = Evaluator(request.config)
    try:
        result = evaluator.evaluate(
            switch_id=request.switch_id,
            user_id=request.user_id,
            context=request.context,
        )
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"开关不存在: {request.switch_id}"
        ) from None
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    return EvaluateResponse(
        switch_id=result.switch_id,
        variant_id=result.variant_id,
        payload=result.payload,
        served_default=result.served_default,
        reason=result.reason,
        trace=[TraceEntry(**t.model_dump()) for t in result.trace],
    )


@app.post("/explanations/minimize", response_model=MinimizeContextResponse)
def minimize_context_endpoint(
    request: MinimizeContextRequest,
) -> MinimizeContextResponse:
    """最小充分上下文：枚举保留集合，求维持评估结果不变的最小属性集。"""
    # 与 /evaluate 一致：无效配置直接 422，拒绝在坏配置上做解释。
    issues = validate_config(request.config)
    if issues:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "配置校验未通过，无法评估",
                "errors": [issue.model_dump() for issue in issues],
            },
        )

    try:
        baseline, outcome = minimize_context(request)
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"开关不存在: {request.switch_id}"
        ) from None
    except MinimizeRequestError as exc:
        # 属性域未覆盖/多声明引用属性。
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    return MinimizeContextResponse(
        switch_id=request.switch_id,
        user_id=request.user_id,
        baseline_variant_id=baseline.variant_id,
        baseline_served_default=baseline.served_default,
        baseline_reason=baseline.reason,
        kept_attributes=outcome.kept,
        removed_attributes=outcome.removed,
        checked_combinations=outcome.checked_combinations,
    )


@app.post("/guardrails/evaluate", response_model=GuardrailEvaluateResponse)
def evaluate_guardrails_endpoint(
    request: GuardrailEvaluateRequest,
) -> GuardrailEvaluateResponse:
    """序贯护栏：逐批累计 Wald 对数似然比，输出 rollback / promote / hold。"""
    # 概率越界、指标重复、批次缺项、failures>total 等在模型校验阶段即 422。
    return evaluate_guardrails(request)


@app.post("/approvals/evaluate", response_model=ApprovalEvaluateResponse)
def evaluate_approvals_endpoint(
    request: ApprovalEvaluateRequest,
) -> ApprovalEvaluateResponse:
    """多人审批：折叠事件流，否决角色 reject 判 blocked，否则按组配额判定。"""
    # 重复人员/组/sequence、未知引用、非法阈值等在模型校验阶段即 422。
    return evaluate_approvals(request)


@app.post("/experiments/attribute", response_model=ExperimentAttributeResponse)
def attribute_experiment_endpoint(
    request: ExperimentAttributeRequest,
) -> ExperimentAttributeResponse:
    """实验归因：事件去重排序后，把转化归给窗口内最近曝光，洗脱区间内记 contaminated。"""
    # event_id 内容冲突、负 value、负窗口/洗脱时长等在模型校验阶段即 422。
    return attribute_experiment(request)


@app.post("/artifacts/sign", response_model=SignResponse)
def sign_artifact(request: SignRequest) -> SignResponse:
    """签发配置制品：先复用配置校验，再生成摘要与 HMAC 签名。"""
    # 与 /evaluate 一致：无效配置直接 422，拒绝为坏配置签发制品。
    issues = validate_config(request.config)
    if issues:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "配置校验未通过，无法签发",
                "errors": [issue.model_dump() for issue in issues],
            },
        )

    secret = load_signing_keys().get(request.key_id)
    if secret is None:
        raise HTTPException(
            status_code=404, detail=f"签名密钥不存在: {request.key_id}"
        )

    config_dict = request.config.model_dump(mode="json")
    digest, signature = sign_config(
        config_dict,
        request.key_id,
        request.issued_at,
        request.expires_at,
        secret,
    )
    return SignResponse(
        artifact=Artifact(
            config=request.config,
            key_id=request.key_id,
            issued_at=request.issued_at,
            expires_at=request.expires_at,
            digest=digest,
            signature=signature,
        )
    )


@app.post("/artifacts/verify", response_model=VerifyResponse)
def verify_artifact_endpoint(request: VerifyRequest) -> VerifyResponse:
    """验签：重算摘要与签名（compare_digest），并检查时间窗。"""
    reason = verify_artifact(request.artifact, load_signing_keys())
    return VerifyResponse(valid=reason is None, reason=reason)


@app.post("/release-windows/resolve", response_model=ReleaseResolveResponse)
def resolve_release_windows_endpoint(
    request: ReleaseResolveRequest,
) -> ReleaseResolveResponse:
    """发布窗口：展开各地区周期日历（含跨午夜与 DST 调整）、扣黑窗、求交集。"""
    # 非法时区、重复日历/规则 ID、空区间、非 UTC 查询时间、跨度 > 31 天等
    # 都在模型校验阶段即 422。
    return resolve_release_windows(request)


@app.post("/rollouts/rebalance", response_model=RebalanceResponse)
def rebalance_rollout(request: RebalanceRequest) -> RebalanceResponse:
    """权重重排：枚举目标变体顺序，生成最少扰动现有用户的连续分桶方案。"""
    # 重复变体 ID、非正权重、权重和不为 10000、目标变体超过 8 个等
    # 都在模型校验阶段即 422。
    return rebalance(request)


@app.post("/flags/retirement-plan", response_model=RetirementPlanResponse)
def retirement_plan(request: RetirementPlanRequest) -> RetirementPlanResponse:
    """开关下线方案：候选必须已关闭；外部依赖仅显式期望终态才可改写，否则与活动客户端引用列为 blocker。"""
    # 与 /evaluate 一致：无效配置直接 422，拒绝在坏配置上生成方案。
    issues = validate_config(request.config)
    if issues:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "配置校验未通过，无法生成下线方案",
                "errors": [issue.model_dump() for issue in issues],
            },
        )

    try:
        return build_retirement_plan(request)
    except RetirementRequestError as exc:
        # 未知候选/客户端引用、候选仍开启等请求与配置不一致。
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.post("/sdk/reconcile", response_model=ReconcileResponse)
def reconcile_sdk_logs(request: ReconcileRequest) -> ReconcileResponse:
    """SDK 决策日志核对：按快照激活时间窗（含宽限与时钟偏差）分类事件，重算决策比对。"""
    # 与 /evaluate 一致：任一快照配置未通过校验直接 422，拒绝在坏配置上核对。
    issues = []
    for index, snapshot in enumerate(request.snapshots):
        for issue in validate_config(snapshot.config):
            issues.append(
                issue.model_copy(
                    update={"path": f"snapshots[{index}].{issue.path}"}
                    if issue.path
                    else {"path": f"snapshots[{index}]"}
                )
            )
    if issues:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "配置快照校验未通过，无法核对",
                "errors": [issue.model_dump() for issue in issues],
            },
        )

    try:
        return reconcile(request)
    except ReconcileRequestError as exc:
        # 快照摘要重复、事件引用快照中不存在的开关。
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.post("/sdk/rollout-plan", response_model=RolloutPlanResponse)
def sdk_rollout_plan(request: RolloutPlanRequest) -> RolloutPlanResponse:
    """制品分波铺开：只规划摘要不同的节点，子集精确枚举求最少波次。

    同波节点同时不可用，受 max_parallel、全局/分区不可用权重与故障域
    （同域每波至多一个）约束；并列时每波 ID 升序后取波次序列字典序最小。
    任一节点单独更新仍超全局或其分区权重上限时只返回 blockers。
    """
    # 重复节点 ID、未知分区限额、非正权重、非法上限等在模型校验阶段即 422。
    return build_rollout_plan(request)


@app.post("/sdk/rollout-audit", response_model=RolloutAuditResponse)
def sdk_rollout_audit(request: RolloutAuditRequest) -> RolloutAuditResponse:
    """铺开执行审计：事件按 sequence 折叠重放，逐波独立复用序贯护栏。

    wave_started 必须按波次顺序且上一波 promote；node_completed 只能发生
    在所属波启动后；metric_batch 只能在该波全部节点完成后接收，各波护栏
    独立累计。首个违规即停止：返回违规事件与事前状态；合法日志返回各波
    完成节点、护栏路径与 running/completed/rollback_required 总状态。
    """
    # event_id 内容冲突、sequence 重复、事件引用未知波次/节点/指标、
    # 非 planned 计划、护栏参数非法等在模型校验阶段即 422。
    return audit_rollout(request)
