"""序贯护栏测试：Wald SPRT 逐批累计、冻结、总决策汇总与请求校验。"""

from __future__ import annotations

import math

import pytest

from app.guardrails import evaluate_guardrails, evaluate_metric
from app.models import (
    GuardrailBatch,
    GuardrailDecision,
    GuardrailEvaluateRequest,
    GuardrailMetric,
    OverallDecision,
)

URL = "/guardrails/evaluate"

# 标准指标：p0=0.01, p1=0.02, alpha=0.05, beta=0.1。
P0, MI, ALPHA, BETA = 0.01, 0.01, 0.05, 0.1
UPPER = math.log((1 - BETA) / ALPHA)  # ln(18) ≈ 2.8904
LOWER = math.log(BETA / (1 - ALPHA))  # ≈ -2.2513
LOG_FAIL = math.log((P0 + MI) / P0)  # ln(2) ≈ 0.6931
LOG_OK = math.log((1 - P0 - MI) / (1 - P0))  # ln(0.98/0.99) ≈ -0.01015


def batch(failures: int, total: int) -> GuardrailBatch:
    return GuardrailBatch(failures=failures, total=total)


def metric(
    id: str = "m",
    batches: list[GuardrailBatch] | None = None,
    **overrides,
) -> GuardrailMetric:
    params = dict(
        id=id,
        baseline_rate=P0,
        max_increase=MI,
        alpha=ALPHA,
        beta=BETA,
        batches=batches if batches is not None else [batch(0, 100)],
    )
    params.update(overrides)
    return GuardrailMetric(**params)


def request_payload(*metrics: GuardrailMetric) -> dict:
    return {
        "metrics": [m.model_dump(mode="json") for m in metrics],
    }


def test_llr_accumulates_binomial_increments():
    # 契约：LLR += f*ln(p1/p0) + (n-f)*ln((1-p1)/(1-p0))。
    m = metric(batches=[batch(1, 100), batch(2, 50)])
    result = evaluate_metric(m)
    first = 1 * LOG_FAIL + 99 * LOG_OK
    second = first + 2 * LOG_FAIL + 48 * LOG_OK
    assert result.path[0].llr == pytest.approx(first)
    assert result.path[1].llr == pytest.approx(second)
    assert result.llr == pytest.approx(second)
    assert result.failures == 3
    assert result.total == 150


def test_thresholds_are_wald_bounds():
    result = evaluate_metric(metric())
    assert result.upper_threshold == pytest.approx(math.log(18))
    assert result.lower_threshold == pytest.approx(math.log(0.1 / 0.95))
    assert result.lower_threshold < 0 < result.upper_threshold
    assert result.p0 == P0
    assert result.p1 == P0 + MI


def test_pass_when_llr_crosses_lower_bound():
    # 全部成功：每批 -1.0152，第 3 批累计 -3.0457 跌破下阈值。
    m = metric(batches=[batch(0, 100)] * 5)
    result = evaluate_metric(m)
    assert result.decision == GuardrailDecision.PASS
    assert result.stop_batch == 3
    assert len(result.path) == 3
    assert result.llr <= result.lower_threshold
    assert result.failures == 0
    assert result.total == 300


def test_rollback_when_llr_crosses_upper_bound():
    # 100 观测 10 失败：LLR ≈ 6.018，第 1 批即越上界。
    m = metric(batches=[batch(10, 100)])
    result = evaluate_metric(m)
    assert result.decision == GuardrailDecision.ROLLBACK
    assert result.stop_batch == 1
    assert result.llr >= result.upper_threshold


def test_hold_when_llr_stays_between_bounds():
    # 每批 1/100：LLR 缓慢下行但 3 批内不触界。
    m = metric(batches=[batch(1, 100)] * 3)
    result = evaluate_metric(m)
    assert result.decision == GuardrailDecision.HOLD
    assert result.stop_batch is None
    assert len(result.path) == 3
    assert result.lower_threshold < result.llr < result.upper_threshold


def test_freeze_after_first_crossing():
    # 第 1 批即回滚；后续全成功批次不再消费，判定与计数冻结。
    m = metric(batches=[batch(10, 100), batch(0, 100), batch(0, 100)])
    result = evaluate_metric(m)
    assert result.decision == GuardrailDecision.ROLLBACK
    assert result.stop_batch == 1
    assert len(result.path) == 1
    assert result.failures == 10
    assert result.total == 100
    assert result.llr == pytest.approx(10 * LOG_FAIL + 90 * LOG_OK)


def test_overall_rollback_takes_earliest_batch():
    fast = metric(id="fast", batches=[batch(10, 100), batch(0, 100), batch(0, 100)])
    slow = metric(id="slow", batches=[batch(1, 100), batch(10, 100), batch(0, 100)])
    resp = evaluate_guardrails(GuardrailEvaluateRequest(metrics=[fast, slow]))
    assert resp.decision == OverallDecision.ROLLBACK
    assert resp.stop_batch == 1  # fast 第 1 批回滚，slow 第 2 批回滚，取最早。
    assert resp.metric_count == 2
    assert resp.batch_count == 3


def test_overall_promote_requires_all_pass():
    quick = metric(id="quick", batches=[batch(0, 300)] * 3)  # 第 1 批即 pass
    slow = metric(id="slow", batches=[batch(0, 100)] * 3)  # 第 3 批才 pass
    resp = evaluate_guardrails(GuardrailEvaluateRequest(metrics=[quick, slow]))
    assert resp.decision == OverallDecision.PROMOTE
    assert resp.stop_batch == 3  # 最后放行者的批次。
    assert all(
        r.decision == GuardrailDecision.PASS for r in resp.metrics
    )


def test_overall_hold_when_any_metric_undecided():
    passed = metric(id="passed", batches=[batch(0, 100)] * 3)
    waiting = metric(id="waiting", batches=[batch(1, 100)] * 3)
    resp = evaluate_guardrails(GuardrailEvaluateRequest(metrics=[passed, waiting]))
    assert resp.decision == OverallDecision.HOLD
    assert resp.stop_batch is None


def test_rollback_beats_pass_and_hold():
    bad = metric(id="bad", batches=[batch(10, 100)] * 3)
    good = metric(id="good", batches=[batch(0, 100)] * 3)
    waiting = metric(id="waiting", batches=[batch(1, 100)] * 3)
    resp = evaluate_guardrails(
        GuardrailEvaluateRequest(metrics=[bad, good, waiting])
    )
    assert resp.decision == OverallDecision.ROLLBACK
    assert resp.stop_batch == 1


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------


def test_api_happy_path(client):
    resp = client.post(URL, json=request_payload(metric(batches=[batch(0, 100)] * 3)))
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "promote"
    assert body["stop_batch"] == 3
    assert body["metric_count"] == 1
    assert body["batch_count"] == 3
    (m,) = body["metrics"]
    assert m["decision"] == "pass"
    assert m["stop_batch"] == 3
    assert m["failures"] == 0
    assert m["total"] == 300
    assert m["p0"] == P0
    assert m["p1"] == P0 + MI
    assert m["lower_threshold"] < 0 < m["upper_threshold"]
    assert [p["batch"] for p in m["path"]] == [1, 2, 3]
    assert m["path"][-1]["llr"] == pytest.approx(m["llr"])


def test_api_rollback_response(client):
    resp = client.post(URL, json=request_payload(metric(batches=[batch(10, 100)])))
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "rollback"
    assert body["stop_batch"] == 1
    assert body["metrics"][0]["decision"] == "rollback"


@pytest.mark.parametrize("baseline_rate", [0, -0.1, 1, 1.5])
def test_api_rejects_baseline_rate_out_of_range(client, baseline_rate):
    payload = request_payload(metric())
    payload["metrics"][0]["baseline_rate"] = baseline_rate
    resp = client.post(URL, json=payload)
    assert resp.status_code == 422


@pytest.mark.parametrize("max_increase", [0, -0.01])
def test_api_rejects_non_positive_max_increase(client, max_increase):
    payload = request_payload(metric())
    payload["metrics"][0]["max_increase"] = max_increase
    assert client.post(URL, json=payload).status_code == 422


def test_api_rejects_p1_out_of_range(client):
    payload = request_payload(metric())
    payload["metrics"][0]["baseline_rate"] = 0.95
    payload["metrics"][0]["max_increase"] = 0.1  # p1 = 1.05 >= 1
    assert client.post(URL, json=payload).status_code == 422


@pytest.mark.parametrize(
    "alpha,beta",
    [(0, 0.1), (1, 0.1), (0.1, 0), (0.1, 1), (0.5, 0.6)],
)
def test_api_rejects_invalid_error_probabilities(client, alpha, beta):
    payload = request_payload(metric())
    payload["metrics"][0]["alpha"] = alpha
    payload["metrics"][0]["beta"] = beta
    assert client.post(URL, json=payload).status_code == 422


def test_api_rejects_duplicate_metric_ids(client):
    payload = request_payload(metric(id="dup"), metric(id="dup"))
    resp = client.post(URL, json=payload)
    assert resp.status_code == 422


def test_api_rejects_missing_batches(client):
    full = metric(id="full", batches=[batch(0, 100)] * 3)
    short = metric(id="short", batches=[batch(0, 100)] * 2)
    resp = client.post(URL, json=request_payload(full, short))
    assert resp.status_code == 422


def test_api_rejects_failures_greater_than_total(client):
    payload = request_payload(metric(batches=[batch(0, 100)]))
    payload["metrics"][0]["batches"][0]["failures"] = 101
    resp = client.post(URL, json=payload)
    assert resp.status_code == 422


def test_api_rejects_empty_and_overfull_metric_lists(client):
    assert client.post(URL, json={"metrics": []}).status_code == 422
    payload = request_payload(*[metric(id=f"m{i}") for i in range(9)])
    assert client.post(URL, json=payload).status_code == 422
    # 边界：8 个指标合法。
    payload = request_payload(*[metric(id=f"m{i}") for i in range(8)])
    assert client.post(URL, json=payload).status_code == 200


def test_api_rejects_empty_batches_and_bad_counts(client):
    payload = request_payload(metric(batches=[batch(0, 100)]))
    payload["metrics"][0]["batches"] = []
    assert client.post(URL, json=payload).status_code == 422

    payload = request_payload(metric(batches=[batch(0, 100)]))
    payload["metrics"][0]["batches"][0]["total"] = 0
    assert client.post(URL, json=payload).status_code == 422

    payload = request_payload(metric(batches=[batch(0, 100)]))
    payload["metrics"][0]["batches"][0]["failures"] = -1
    assert client.post(URL, json=payload).status_code == 422
