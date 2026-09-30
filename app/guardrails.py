"""序贯护栏：对二项失败率做 Wald 序贯概率比检验（SPRT）。

每个指标声明基线失败率 p0 = baseline_rate 与最大可接受劣化
p1 = p0 + max_increase，逐批累计对数似然比（LLR）：

    LLR += failures * ln(p1/p0) + (total - failures) * ln((1-p1)/(1-p0))

- LLR >= ln((1-beta)/alpha)  → rollback（劣化证据充分）；
- LLR <= ln(beta/(1-alpha))  → pass（无劣化证据充分）；
- 其余                        → hold（继续观察）。

任一指标首次越界后即冻结：不再消费后续批次，判定与计数保持不变。
总决策：任一指标 rollback → rollback（停止批次取各回滚指标中最早者）；
全部指标 pass → promote（停止批次取最后放行者）；其余 → hold。
"""

from __future__ import annotations

import math

from app.models import (
    GuardrailDecision,
    GuardrailEvaluateRequest,
    GuardrailEvaluateResponse,
    GuardrailMetric,
    GuardrailMetricResult,
    GuardrailPathEntry,
    OverallDecision,
)


def evaluate_metric(metric: GuardrailMetric) -> GuardrailMetricResult:
    """对单个指标逐批累计 LLR，首次越界后冻结。"""
    p0 = metric.baseline_rate
    p1 = p0 + metric.max_increase
    lower = math.log(metric.beta / (1 - metric.alpha))
    upper = math.log((1 - metric.beta) / metric.alpha)
    # 单观测的 LLR 增量：失败计 ln(p1/p0)，成功计 ln((1-p1)/(1-p0))。
    log_fail = math.log(p1 / p0)
    log_ok = math.log((1 - p1) / (1 - p0))

    llr = 0.0
    failures = 0
    total = 0
    path: list[GuardrailPathEntry] = []
    decision = GuardrailDecision.HOLD
    stop_batch: int | None = None

    for index, batch in enumerate(metric.batches, start=1):
        llr += batch.failures * log_fail + (batch.total - batch.failures) * log_ok
        failures += batch.failures
        total += batch.total
        path.append(
            GuardrailPathEntry(
                batch=index,
                failures=batch.failures,
                total=batch.total,
                llr=llr,
            )
        )
        if llr >= upper:
            decision = GuardrailDecision.ROLLBACK
            stop_batch = index
            break
        if llr <= lower:
            decision = GuardrailDecision.PASS
            stop_batch = index
            break

    return GuardrailMetricResult(
        id=metric.id,
        decision=decision,
        stop_batch=stop_batch,
        failures=failures,
        total=total,
        p0=p0,
        p1=p1,
        lower_threshold=lower,
        upper_threshold=upper,
        llr=llr,
        path=path,
    )


def evaluate_guardrails(
    request: GuardrailEvaluateRequest,
) -> GuardrailEvaluateResponse:
    """汇总各指标判定为发布级总决策。"""
    results = [evaluate_metric(metric) for metric in request.metrics]

    rollback_batches = [
        r.stop_batch
        for r in results
        if r.decision == GuardrailDecision.ROLLBACK and r.stop_batch is not None
    ]
    if rollback_batches:
        decision = OverallDecision.ROLLBACK
        stop_batch: int | None = min(rollback_batches)
    elif all(r.decision == GuardrailDecision.PASS for r in results):
        decision = OverallDecision.PROMOTE
        # 全部 pass 时各指标均有停止批次，总停止批次取最后放行者。
        stop_batch = max(
            r.stop_batch for r in results if r.stop_batch is not None
        )
    else:
        decision = OverallDecision.HOLD
        stop_batch = None

    return GuardrailEvaluateResponse(
        decision=decision,
        stop_batch=stop_batch,
        metric_count=len(results),
        batch_count=len(request.metrics[0].batches),
        metrics=results,
    )
