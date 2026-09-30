"""Pydantic 数据模型：开关、变体、依赖、有序规则、制品、请求/响应与发布窗口。"""

from __future__ import annotations

import enum
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

# 哈希桶总数：摘要映射到 [0, BUCKETS) 即 0~9999，合法权重总和必须等于该值。
BUCKETS = 10_000


def _as_utc(value: datetime) -> datetime:
    """把日期时间归一到 UTC：naive 视为 UTC，aware 换算到 UTC。

    保证签名材料里的时间戳有唯一、可复现的序列化形式。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# 归一到 UTC 的日期时间，用于制品的签发/过期时间。
UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


def _require_utc(value: datetime) -> datetime:
    """只接受带时区且偏移恰为 UTC 的日期时间（如 ...Z）。

    与 _as_utc（naive 视为 UTC、其它时区换算到 UTC）不同：发布窗口求解
    要求请求显式以 UTC 给出，naive 与非 UTC 偏移一律拒绝（422），避免调用方
    误以为本地时间会被静默换算。
    """
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("时间必须显式以 UTC 给出（带 Z 或 +00:00）")
    return value.astimezone(timezone.utc)


# 严格 UTC 日期时间，用于发布窗口求解的查询范围与 blackouts。
StrictUtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]


class Operator(str, enum.Enum):
    """属性规则支持的运算符。"""

    EQ = "eq"
    NEQ = "neq"
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    EXISTS = "exists"


# 只有 EXISTS 不需要右值，其余运算符都必须提供 value。
_OPERATORS_REQUIRING_VALUE = {
    Operator.EQ,
    Operator.NEQ,
    Operator.IN,
    Operator.NOT_IN,
    Operator.CONTAINS,
    Operator.GT,
    Operator.GTE,
    Operator.LT,
    Operator.LTE,
}


class Condition(BaseModel):
    """单个属性条件，从用户 context 中取 attribute 与 value 比较。"""

    model_config = ConfigDict(extra="forbid")

    attribute: str = Field(min_length=1)
    operator: Operator
    value: Any = None

    @model_validator(mode="after")
    def _check_value_present(self) -> "Condition":
        if self.operator in _OPERATORS_REQUIRING_VALUE and self.value is None:
            raise ValueError(
                f"operator '{self.operator.value}' 需要提供 value"
            )
        return self


class Variant(BaseModel):
    """变体：ID 唯一，payload 为透传给调用方的任意 JSON 对象。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    payload: dict[str, Any] | None = None


class Allocation(BaseModel):
    """权重分配项：把 variant_id 映射到一段累计权重区间。"""

    model_config = ConfigDict(extra="forbid")

    variant_id: str = Field(min_length=1)
    weight: int = Field(ge=0, le=BUCKETS)


class Rule(BaseModel):
    """有序规则：conditions 全部命中（AND）后按 allocations 做权重分流。

    conditions 为空表示无条件命中（通常放在规则列表末尾作为兜底）。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    conditions: list[Condition] = Field(default_factory=list)
    allocations: list[Allocation] = Field(min_length=1)


class Dependency(BaseModel):
    """对另一个开关的依赖。

    - 不指定 variant：被依赖开关解析出“非默认变体”即视为满足；
    - 指定 variant：被依赖开关必须恰好解析到该变体才满足。
    """

    model_config = ConfigDict(extra="forbid")

    switch: str = Field(min_length=1)
    variant: str | None = None


class Switch(BaseModel):
    """灰度开关定义。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    enabled: bool = True
    # 盐值缺省时回退为开关 ID，参与哈希的三元组为 (salt, switch_id, user_id)。
    salt: str | None = None
    default_variant: str = Field(min_length=1)
    variants: list[Variant] = Field(min_length=1)
    dependencies: list[Dependency] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)


class FlagConfig(BaseModel):
    """整份开关配置，校验与评估接口共用。"""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    switches: list[Switch] = Field(default_factory=list)


class EvaluateRequest(BaseModel):
    """评估请求：在给定配置下评估某个开关对某个用户的最终变体。"""

    model_config = ConfigDict(extra="forbid")

    config: FlagConfig
    switch_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)


class ValidationIssue(BaseModel):
    """单条校验问题。"""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    path: str | None = None


class ValidationResponse(BaseModel):
    """POST /validate 响应。"""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    errors: list[ValidationIssue] = Field(default_factory=list)


class TraceEntry(BaseModel):
    """评估命中轨迹中的一步，step 为机器可读名称，detail 为附带数据。"""

    model_config = ConfigDict(extra="forbid")

    step: str
    detail: dict[str, Any] = Field(default_factory=dict)


class EvaluateResponse(BaseModel):
    """POST /evaluate 响应。"""

    model_config = ConfigDict(extra="forbid")

    switch_id: str
    variant_id: str
    payload: dict[str, Any] | None = None
    served_default: bool
    reason: str | None = None
    trace: list[TraceEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 最小充分上下文（评估解释）
# ---------------------------------------------------------------------------

# 域组合上限：所有属性域的（候选值数 + 缺失态）笛卡尔积不得超过该值。
MAX_DOMAIN_COMBINATIONS = 4096


def _canonical_json(value: Any) -> str:
    """JSON 值的规范化序列化（键按字典序、无空白），用于域内候选值判等。

    与 app.artifacts 的 canonical_json 同语义，保留字符串形态便于做集合成员判断。
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


class AttributeDomain(BaseModel):
    """单个属性的有限域：候选 JSON 值 + 是否允许该属性缺失。

    缺失态等价于评估 context 中不包含该属性键（覆盖 exists 与
    非 exists 运算符的“未提供”分支）。
    """

    model_config = ConfigDict(extra="forbid")

    attribute: str = Field(min_length=1)
    values: list[Any] = Field(default_factory=list)
    allow_missing: bool = False

    @model_validator(mode="after")
    def _check_domain(self) -> "AttributeDomain":
        # 域内候选值按规范化 JSON 判等去重：{"b":2,"a":1} 与 {"a":1,"b":2}
        # 视为同一值，重复声明会让枚举集合产生歧义，拒绝（422）。
        seen: set[str] = set()
        for value in self.values:
            key = _canonical_json(value)
            if key in seen:
                raise ValueError(f"属性 '{self.attribute}' 的域内候选值重复")
            seen.add(key)
        if not self.values and not self.allow_missing:
            raise ValueError(
                f"属性 '{self.attribute}' 的域为空（既无候选值也不允许缺失）"
            )
        return self


class MinimizeContextRequest(BaseModel):
    """POST /explanations/minimize 请求。

    domains 须在服务端结合配置校验：恰好覆盖目标开关及其传递依赖的规则
    所引用的全部属性；本模型先做与配置无关的校验（重复域/空域/当前值
    落域/缺失态/组合上限）。
    """

    model_config = ConfigDict(extra="forbid")

    config: FlagConfig
    switch_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    domains: list[AttributeDomain] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_domains(self) -> "MinimizeContextRequest":
        seen: set[str] = set()
        combinations = 1
        for domain in self.domains:
            if domain.attribute in seen:
                raise ValueError(f"属性域重复: '{domain.attribute}'")
            seen.add(domain.attribute)

            states = len(domain.values) + (1 if domain.allow_missing else 0)
            combinations *= states

            # 当前值必须落在域内；context 中没有该属性时其域必须允许缺失。
            if domain.attribute in self.context:
                current = _canonical_json(self.context[domain.attribute])
                if all(
                    _canonical_json(value) != current for value in domain.values
                ):
                    raise ValueError(
                        f"属性 '{domain.attribute}' 的当前值不在其域内"
                    )
            elif not domain.allow_missing:
                raise ValueError(
                    f"属性 '{domain.attribute}' 未出现在 context 中，"
                    f"但其域不允许缺失"
                )

        if combinations > MAX_DOMAIN_COMBINATIONS:
            raise ValueError(
                f"域组合总数 {combinations} 超过上限 {MAX_DOMAIN_COMBINATIONS}"
            )
        return self


class MinimizeContextResponse(BaseModel):
    """POST /explanations/minimize 响应。

    kept_attributes 为按（属性数升序、属性名字典序）枚举到的首个最小
    充分集合；removed_attributes 为被移除属性；checked_combinations 为
    搜索中实际重评的组合数（含空移除集合的一次基准复算，不匹配即短路）。
    """

    model_config = ConfigDict(extra="forbid")

    switch_id: str
    user_id: str
    baseline_variant_id: str
    baseline_served_default: bool
    baseline_reason: str | None = None
    kept_attributes: list[str] = Field(default_factory=list)
    removed_attributes: list[str] = Field(default_factory=list)
    checked_combinations: int = Field(ge=0)


class SignRequest(BaseModel):
    """POST /artifacts/sign 请求：为一份配置在指定时间窗内签发制品。"""

    model_config = ConfigDict(extra="forbid")

    config: FlagConfig
    key_id: str = Field(min_length=1)
    issued_at: UtcDatetime
    expires_at: UtcDatetime

    @model_validator(mode="after")
    def _check_window(self) -> "SignRequest":
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at 必须晚于 issued_at")
        return self


class Artifact(BaseModel):
    """配置制品：配置本体 + 元数据 + SHA-256 摘要 + HMAC-SHA256 签名。

    digest 是规范化 JSON（键按字典序、无空白）的 SHA-256 十六进制摘要；
    signature 覆盖 key_id、issued_at、expires_at、digest 与 config。
    """

    model_config = ConfigDict(extra="forbid")

    config: FlagConfig
    key_id: str = Field(min_length=1)
    issued_at: UtcDatetime
    expires_at: UtcDatetime
    digest: str = Field(min_length=1)
    signature: str = Field(min_length=1)


class SignResponse(BaseModel):
    """POST /artifacts/sign 响应。"""

    model_config = ConfigDict(extra="forbid")

    artifact: Artifact


class VerifyRequest(BaseModel):
    """POST /artifacts/verify 请求：原样回传签发得到的制品。"""

    model_config = ConfigDict(extra="forbid")

    artifact: Artifact


class VerifyResponse(BaseModel):
    """POST /artifacts/verify 响应。

    valid=false 时 reason 为 unknown_key / tampered / not_yet_valid / expired。
    """

    model_config = ConfigDict(extra="forbid")

    valid: bool
    reason: str | None = None


# ---------------------------------------------------------------------------
# 序贯护栏（Wald SPRT，二项失败率）
# ---------------------------------------------------------------------------


class GuardrailBatch(BaseModel):
    """单个观测批次：相对上一批的增量 failures/total。"""

    model_config = ConfigDict(extra="forbid")

    failures: int = Field(ge=0)
    total: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_counts(self) -> "GuardrailBatch":
        if self.failures > self.total:
            raise ValueError("failures 不能大于 total")
        return self


class GuardrailMetric(BaseModel):
    """序贯护栏指标声明。

    p0 = baseline_rate，p1 = p0 + max_increase；alpha / beta 分别为
    假回滚（一类）与漏回滚（二类）错误概率，要求 alpha + beta < 1，
    保证下阈值 ln(beta/(1-alpha)) < 0 < 上阈值 ln((1-beta)/alpha)。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    baseline_rate: float = Field(gt=0, lt=1)
    max_increase: float = Field(gt=0)
    alpha: float = Field(gt=0, lt=1)
    beta: float = Field(gt=0, lt=1)
    batches: list[GuardrailBatch] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_rates(self) -> "GuardrailMetric":
        if self.baseline_rate + self.max_increase >= 1:
            raise ValueError("baseline_rate + max_increase 必须小于 1")
        if self.alpha + self.beta >= 1:
            raise ValueError("alpha + beta 必须小于 1")
        return self


class GuardrailEvaluateRequest(BaseModel):
    """POST /guardrails/evaluate 请求：1~8 个指标，各指标批次数必须一致。"""

    model_config = ConfigDict(extra="forbid")

    metrics: list[GuardrailMetric] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _check_metrics(self) -> "GuardrailEvaluateRequest":
        seen: set[str] = set()
        for metric in self.metrics:
            if metric.id in seen:
                raise ValueError(f"指标 ID 重复: '{metric.id}'")
            seen.add(metric.id)
        counts = {len(metric.batches) for metric in self.metrics}
        if len(counts) > 1:
            raise ValueError("所有指标的批次数必须一致（不允许批次缺项）")
        return self


class GuardrailDecision(str, enum.Enum):
    """单个指标的序贯判定。"""

    ROLLBACK = "rollback"
    PASS = "pass"
    HOLD = "hold"


class OverallDecision(str, enum.Enum):
    """整次发布的总决策。"""

    ROLLBACK = "rollback"
    PROMOTE = "promote"
    HOLD = "hold"


class GuardrailPathEntry(BaseModel):
    """似然路径上的一步：处理完该批后的累计对数似然比。"""

    model_config = ConfigDict(extra="forbid")

    batch: int = Field(ge=1)
    failures: int = Field(ge=0)
    total: int = Field(ge=1)
    llr: float


class GuardrailMetricResult(BaseModel):
    """单个指标的序贯评估结果。

    failures/total 为截至停止批次（冻结点）的累计计数；
    stop_batch 为 1 起始的批次号，hold 时为 null。
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    decision: GuardrailDecision
    stop_batch: int | None = None
    failures: int
    total: int
    p0: float
    p1: float
    lower_threshold: float
    upper_threshold: float
    llr: float
    path: list[GuardrailPathEntry] = Field(default_factory=list)


class GuardrailEvaluateResponse(BaseModel):
    """POST /guardrails/evaluate 响应。

    stop_batch：rollback 时为最早回滚批次，promote 时为最后放行批次，
    hold 时为 null。
    """

    model_config = ConfigDict(extra="forbid")

    decision: OverallDecision
    stop_batch: int | None = None
    metric_count: int
    batch_count: int
    metrics: list[GuardrailMetricResult] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 多人审批（配置发布审批）
# ---------------------------------------------------------------------------


class ApprovalAction(str, enum.Enum):
    """审批事件动作：approve 赞成、reject 反对、revoke 撤回本人此前一票。"""

    APPROVE = "approve"
    REJECT = "reject"
    REVOKE = "revoke"


class ApprovalPerson(BaseModel):
    """人员目录条目：人员 ID 及其持有的角色（角色须来自角色目录）。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    roles: list[str] = Field(default_factory=list)


class ApprovalGroup(BaseModel):
    """审批组：持有 allowed_roles 中任意角色的人员可计入本组。

    min_approvals 为本组达标所需的最少赞成人数（>= 1，否则为非法阈值）。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    allowed_roles: list[str] = Field(min_length=1)
    min_approvals: int = Field(ge=1)


class ApprovalEvent(BaseModel):
    """审批事件：按 sequence 严格递增到达，且必须绑定请求的制品摘要。"""

    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=0)
    person: str = Field(min_length=1)
    action: ApprovalAction
    artifact_digest: str = Field(min_length=1)


class ApprovalEvaluateRequest(BaseModel):
    """POST /approvals/evaluate 请求：角色目录 + 人员 + 审批组 + 否决角色 + 事件流。"""

    model_config = ConfigDict(extra="forbid")

    artifact_digest: str = Field(min_length=1)
    roles: list[str] = Field(default_factory=list)
    people: list[ApprovalPerson] = Field(default_factory=list)
    groups: list[ApprovalGroup] = Field(default_factory=list)
    veto_roles: list[str] = Field(default_factory=list)
    events: list[ApprovalEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_references(self) -> "ApprovalEvaluateRequest":
        catalog = set(self.roles)

        # 人员 ID 不允许重复；人员角色必须来自角色目录。
        person_ids: set[str] = set()
        for person in self.people:
            if person.id in person_ids:
                raise ValueError(f"人员 ID 重复: '{person.id}'")
            person_ids.add(person.id)
            for role in person.roles:
                if role not in catalog:
                    raise ValueError(
                        f"人员 '{person.id}' 引用了未知角色: '{role}'"
                    )

        # 审批组 ID 不允许重复；允许角色必须来自角色目录。
        group_ids: set[str] = set()
        for group in self.groups:
            if group.id in group_ids:
                raise ValueError(f"审批组 ID 重复: '{group.id}'")
            group_ids.add(group.id)
            for role in group.allowed_roles:
                if role not in catalog:
                    raise ValueError(
                        f"审批组 '{group.id}' 引用了未知角色: '{role}'"
                    )

        # 否决角色必须来自角色目录。
        for role in self.veto_roles:
            if role not in catalog:
                raise ValueError(f"否决角色未在角色目录中声明: '{role}'")

        # 事件：sequence 严格递增（重复或乱序均拒绝）、人员已知、绑定请求摘要。
        previous: int | None = None
        for event in self.events:
            if previous is not None and event.sequence <= previous:
                raise ValueError(
                    "事件的 sequence 必须严格递增（不允许重复或乱序）"
                )
            previous = event.sequence
            if event.person not in person_ids:
                raise ValueError(f"事件引用了未知人员: '{event.person}'")
            if event.artifact_digest != self.artifact_digest:
                raise ValueError(
                    "事件的 artifact_digest 必须与请求的 artifact_digest 一致"
                )
        return self


class ApprovalStatus(str, enum.Enum):
    """发布审批总状态。"""

    APPROVED = "approved"
    PENDING = "pending"
    BLOCKED = "blocked"


class ApprovalGroupResult(BaseModel):
    """单个审批组的计入结果：counted 为计入人员（按 ID 升序），shortfall 为缺口。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    min_approvals: int
    counted: list[str] = Field(default_factory=list)
    shortfall: int = Field(ge=0)


class ActiveVote(BaseModel):
    """折叠后仍有效的票：每人最新一条非 revoke 事件。"""

    model_config = ConfigDict(extra="forbid")

    person: str
    action: ApprovalAction
    sequence: int


class VetoEvidence(BaseModel):
    """否决证据：持有否决角色人员的有效 reject。"""

    model_config = ConfigDict(extra="forbid")

    person: str
    roles: list[str] = Field(default_factory=list)
    sequence: int


class ApprovalEvaluateResponse(BaseModel):
    """POST /approvals/evaluate 响应。

    groups 按组 ID 升序；active_votes 与 vetoes 按人员 ID 升序。
    """

    model_config = ConfigDict(extra="forbid")

    status: ApprovalStatus
    artifact_digest: str
    groups: list[ApprovalGroupResult] = Field(default_factory=list)
    active_votes: list[ActiveVote] = Field(default_factory=list)
    vetoes: list[VetoEvidence] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 实验归因（灰度曝光 → 转化的确定性归因）
# ---------------------------------------------------------------------------


class ExposureEvent(BaseModel):
    """曝光事件：某用户在某个 UTC 时刻被分配到某变体。"""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    timestamp: UtcDatetime
    variant_id: str = Field(min_length=1)


class ConversionEvent(BaseModel):
    """转化事件：某用户在某个 UTC 时刻产生一笔非负 value 的转化。"""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    timestamp: UtcDatetime
    value: float = Field(ge=0)


class ExperimentAttributeRequest(BaseModel):
    """POST /experiments/attribute 请求：开关 + 归因窗口 + 洗脱时长 + 乱序事件。

    曝光与转化事件可乱序提交，全局按 event_id 去重：同一 event_id 的内容
    （类型、user_id、timestamp 及变体/value）必须完全一致，否则 422。
    """

    model_config = ConfigDict(extra="forbid")

    switch_id: str = Field(min_length=1)
    # 归因窗口（秒）：曝光不晚于转化且时差不超过该值才可归因，0 表示仅同时刻。
    window_seconds: int = Field(ge=0)
    # 洗脱时长（秒）：变体切换后的半开区间 [切换时刻, 切换时刻+时长)，0 表示不洗脱。
    washout_seconds: int = Field(ge=0)
    exposures: list[ExposureEvent] = Field(default_factory=list)
    conversions: list[ConversionEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_event_id_conflicts(self) -> "ExperimentAttributeRequest":
        # event_id -> 归一化内容元组；首元素为事件类型，曝光与转化同 ID 必冲突。
        seen: dict[str, tuple] = {}
        for event in self.exposures:
            content = ("exposure", event.user_id, event.timestamp, event.variant_id)
            previous = seen.get(event.event_id)
            if previous is not None and previous != content:
                raise ValueError(f"事件 ID 内容冲突: '{event.event_id}'")
            seen[event.event_id] = content
        for event in self.conversions:
            content = ("conversion", event.user_id, event.timestamp, event.value)
            previous = seen.get(event.event_id)
            if previous is not None and previous != content:
                raise ValueError(f"事件 ID 内容冲突: '{event.event_id}'")
            seen[event.event_id] = content
        return self


class AttributionStatus(str, enum.Enum):
    """单笔转化的归因状态。"""

    ATTRIBUTED = "attributed"
    NO_EXPOSURE = "no_exposure"
    EXPIRED = "expired"
    CONTAMINATED = "contaminated"


class VariantAttribution(BaseModel):
    """单个变体的归因汇总：去重曝光用户数、计入转化数与 value 合计。"""

    model_config = ConfigDict(extra="forbid")

    variant_id: str
    exposed_users: int = Field(ge=0)
    conversions: int = Field(ge=0)
    value: float = Field(ge=0)


class ConversionAttribution(BaseModel):
    """单笔转化的归因明细。

    exposure_event_id / variant_id 仅在窗口内匹配到最近曝光（attributed /
    contaminated）时给出；no_exposure / expired 时为 null。
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str
    user_id: str
    timestamp: UtcDatetime
    value: float = Field(ge=0)
    status: AttributionStatus
    exposure_event_id: str | None = None
    variant_id: str | None = None


class ExperimentAttributeResponse(BaseModel):
    """POST /experiments/attribute 响应。

    variants 按 variant_id 升序；attributions 按 (user_id, timestamp, event_id)
    升序，输出顺序与事件提交顺序无关。
    """

    model_config = ConfigDict(extra="forbid")

    switch_id: str
    variants: list[VariantAttribution] = Field(default_factory=list)
    attributions: list[ConversionAttribution] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 发布窗口求解（多地区维护日历的共同可发布 UTC 区间）
# ---------------------------------------------------------------------------

# 本地 HH:MM（24 小时制）；周期窗口的起止都是“墙上时间”，再按各自时区解释。
_LOCAL_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
# 查询跨度上限：31 天（744 小时），恰好等于 31 天合法。
MAX_QUERY_SPAN = timedelta(days=31)


class WeeklyWindow(BaseModel):
    """本地周期维护窗口：weekday（0=周一 … 6=周日）当地 start/end 之间可发布。

    end <= start 表示跨午夜：结束落在下一个本地自然日。起讫相同（零时长）
    的窗口不允许（422）。
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1)
    weekday: int = Field(ge=0, le=6)
    start_local: str
    end_local: str

    @model_validator(mode="after")
    def _check_local_times(self) -> "WeeklyWindow":
        if not _LOCAL_TIME.match(self.start_local):
            raise ValueError("start_local 必须是 HH:MM 形式的本地时间")
        if not _LOCAL_TIME.match(self.end_local):
            raise ValueError("end_local 必须是 HH:MM 形式的本地时间")
        if self.end_local == self.start_local:
            raise ValueError("weekly_window 的 start_local 与 end_local 不能相同")
        return self


class UtcBlackout(BaseModel):
    """UTC 绝对黑窗：半开区间 [start, end) 内一律不可发布。"""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1)
    start: StrictUtcDatetime
    end: StrictUtcDatetime

    @model_validator(mode="after")
    def _check_range(self) -> "UtcBlackout":
        if self.end <= self.start:
            raise ValueError("blackout 的 end 必须晚于 start")
        return self


class ReleaseCalendar(BaseModel):
    """单份地区维护日历：IANA 时区 + 本地周期窗口 + UTC 绝对黑窗。"""

    model_config = ConfigDict(extra="forbid")

    calendar_id: str = Field(min_length=1)
    timezone: str = Field(min_length=1)
    weekly_windows: list[WeeklyWindow] = Field(default_factory=list)
    blackouts: list[UtcBlackout] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_timezone_and_rule_ids(self) -> "ReleaseCalendar":
        # 非法 IANA 时区在请求阶段即 422，拒绝带未知时区展开窗口。
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:  # ZoneInfoNotFoundError 及其它键错误
            raise ValueError(f"非法 IANA 时区: '{self.timezone}'") from exc

        seen: set[str] = set()
        for window in self.weekly_windows:
            if window.rule_id in seen:
                raise ValueError(f"规则 ID 在日历内重复: '{window.rule_id}'")
            seen.add(window.rule_id)
        for blackout in self.blackouts:
            if blackout.rule_id in seen:
                raise ValueError(f"规则 ID 在日历内重复: '{blackout.rule_id}'")
            seen.add(blackout.rule_id)
        return self


class ReleaseResolveRequest(BaseModel):
    """POST /release-windows/resolve 请求。"""

    model_config = ConfigDict(extra="forbid")

    start: StrictUtcDatetime
    end: StrictUtcDatetime
    min_duration_seconds: int = Field(ge=1)
    calendars: list[ReleaseCalendar] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_request(self) -> "ReleaseResolveRequest":
        if self.end <= self.start:
            raise ValueError("end 必须晚于 start")
        if self.end - self.start > MAX_QUERY_SPAN:
            raise ValueError("查询跨度不能超过 31 天")

        calendar_ids: set[str] = set()
        # 规则 ID 在整个请求内全局唯一，响应中的贡献规则才能无歧义归属日历。
        rule_ids: set[str] = set()
        for calendar in self.calendars:
            if calendar.calendar_id in calendar_ids:
                raise ValueError(f"日历 ID 重复: '{calendar.calendar_id}'")
            calendar_ids.add(calendar.calendar_id)
            for window in calendar.weekly_windows:
                if window.rule_id in rule_ids:
                    raise ValueError(f"规则 ID 在请求内重复: '{window.rule_id}'")
                rule_ids.add(window.rule_id)
            for blackout in calendar.blackouts:
                if blackout.rule_id in rule_ids:
                    raise ValueError(f"规则 ID 在请求内重复: '{blackout.rule_id}'")
                rule_ids.add(blackout.rule_id)
        return self


class DstBoundary(str, enum.Enum):
    """发生 DST 调整的是周期窗口的哪一端。"""

    START = "start"
    END = "end"


class DstAdjustmentKind(str, enum.Enum):
    """DST 调整类型：春季不存在时刻顺延 / 秋季重复时刻取较早或较晚。"""

    GAP_SHIFTED = "gap_shifted"
    AMBIGUOUS_EARLIER = "ambiguous_earlier"
    AMBIGUOUS_LATER = "ambiguous_later"


class DstAdjustment(BaseModel):
    """一次实际发生的 DST 调整记录。

    local_time 为受影响的本地墙上时刻（naive HH:MM，日期为规则 weekday
    所落的本地日期），resolved_at 为该边界最终解析到的 UTC 时刻。
    """

    model_config = ConfigDict(extra="forbid")

    calendar_id: str
    rule_id: str
    boundary: DstBoundary
    kind: DstAdjustmentKind
    local_time: datetime
    resolved_at: datetime


class WindowContributor(BaseModel):
    """单个日历对一个结果窗口的贡献：命中的周期/黑窗规则 ID（升序）。"""

    model_config = ConfigDict(extra="forbid")

    calendar_id: str
    rule_ids: list[str] = Field(default_factory=list)


class ResolvedWindow(BaseModel):
    """一个共同可发布 UTC 半开区间。"""

    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime
    duration_seconds: int
    contributors: list[WindowContributor] = Field(default_factory=list)
    dst_adjustments: list[DstAdjustment] = Field(default_factory=list)


class ReleaseResolveResponse(BaseModel):
    """POST /release-windows/resolve 响应：窗口按 (start, end) 升序。"""

    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime
    min_duration_seconds: int
    windows: list[ResolvedWindow] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 权重重排（为变体权重调整生成最少扰动的连续分桶方案）
# ---------------------------------------------------------------------------

# 目标变体数上限：服务端枚举目标顺序的全排列，8! = 40320 仍在可接受范围。
MAX_REBALANCE_TARGETS = 8


class RebalanceAllocation(BaseModel):
    """重平衡权重项：variant_id + 正权重（占据的桶数），两侧通用。"""

    model_config = ConfigDict(extra="forbid")

    variant_id: str = Field(min_length=1)
    weight: int = Field(gt=0, le=BUCKETS)


class RebalanceRequest(BaseModel):
    """POST /rollouts/rebalance 请求。

    current 为当前有序 allocations（其区间布局固定不变）；target 为目标
    权重（允许新增/删除变体），由服务端枚举其顺序以最小化迁移扰动。
    两侧权重均为正、合计 10000，变体 ID 各自不允许重复。
    """

    model_config = ConfigDict(extra="forbid")

    current: list[RebalanceAllocation] = Field(min_length=1)
    target: list[RebalanceAllocation] = Field(
        min_length=1, max_length=MAX_REBALANCE_TARGETS
    )

    @model_validator(mode="after")
    def _check_allocations(self) -> "RebalanceRequest":
        for label, allocations in (("current", self.current), ("target", self.target)):
            seen: set[str] = set()
            total = 0
            for allocation in allocations:
                if allocation.variant_id in seen:
                    raise ValueError(
                        f"{label} 中变体 ID 重复: '{allocation.variant_id}'"
                    )
                seen.add(allocation.variant_id)
                total += allocation.weight
            if total != BUCKETS:
                raise ValueError(f"{label} 权重总和必须等于 {BUCKETS}")
        return self


class RebalanceSegment(BaseModel):
    """推荐布局的一段：variant_id 占据闭区间 [start, end]（均含端点）。"""

    model_config = ConfigDict(extra="forbid")

    variant_id: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    weight: int = Field(ge=1)


class MigrationInterval(BaseModel):
    """迁移方向一致的相邻桶合并成的闭区间 [start, end]（均含端点）。"""

    model_config = ConfigDict(extra="forbid")

    from_variant: str
    to_variant: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    buckets: int = Field(ge=1)


class MigrationMatrixEntry(BaseModel):
    """迁移矩阵的一项：from_variant -> to_variant 的桶数（仅非零项）。"""

    model_config = ConfigDict(extra="forbid")

    from_variant: str
    to_variant: str
    buckets: int = Field(ge=1)


class RebalanceResponse(BaseModel):
    """POST /rollouts/rebalance 响应。

    order 为推荐的目标变体顺序；segments 按桶号升序完整划分 [0, 9999]
    （每个桶恰好归属一个变体）；migrations 同样按桶号升序完整划分桶域；
    matrix 仅含非零项，按 (from_variant, to_variant) 升序。
    """

    model_config = ConfigDict(extra="forbid")

    order: list[str] = Field(min_length=1)
    segments: list[RebalanceSegment] = Field(min_length=1)
    stable_buckets: int = Field(ge=0)
    migrated_buckets: int = Field(ge=0)
    matrix: list[MigrationMatrixEntry] = Field(default_factory=list)
    migrations: list[MigrationInterval] = Field(min_length=1)


# ---------------------------------------------------------------------------
# 开关下线方案（为已关闭开关生成可执行的分层删除计划）
# ---------------------------------------------------------------------------


class ClientReference(BaseModel):
    """仍在使用某开关的活动客户端引用。"""

    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1)
    switch_id: str = Field(min_length=1)


class RetirementPlanRequest(BaseModel):
    """POST /flags/retirement-plan 请求。

    switch_ids 为待下线候选：必须存在且 enabled=false（关闭开关评估时恒
    返回默认变体，默认变体即终态）；client_references 为仍在使用各开关的
    {client_id, switch_id} 活动引用，引用候选开关即构成 blocker。
    """

    model_config = ConfigDict(extra="forbid")

    config: FlagConfig
    switch_ids: list[str] = Field(min_length=1)
    client_references: list[ClientReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_duplicates(self) -> "RetirementPlanRequest":
        seen: set[str] = set()
        for switch_id in self.switch_ids:
            if switch_id in seen:
                raise ValueError(f"候选开关 ID 重复: '{switch_id}'")
            seen.add(switch_id)
        seen_refs: set[tuple[str, str]] = set()
        for ref in self.client_references:
            key = (ref.client_id, ref.switch_id)
            if key in seen_refs:
                raise ValueError(
                    f"客户端引用重复: client_id='{ref.client_id}' "
                    f"switch_id='{ref.switch_id}'"
                )
            seen_refs.add(key)
        return self


class BlockerKind(str, enum.Enum):
    """下线阻断原因类型。"""

    CLIENT_REFERENCE = "client_reference"
    EXTERNAL_DEPENDENCY = "external_dependency"


class RetirementBlocker(BaseModel):
    """一个下线阻断项。

    kind=client_reference：客户端 client_id 仍在引用候选开关；
    kind=external_dependency：外部开关 dependent_id 对候选的依赖未显式
    期望终态（expected_variant 为 null 表示未指定变体，即期望任意非默认
    变体），自动删除该依赖会改变外部开关行为。
    """

    model_config = ConfigDict(extra="forbid")

    kind: BlockerKind
    switch_id: str
    terminal_variant: str
    client_id: str | None = None
    dependent_id: str | None = None
    expected_variant: str | None = None


class DependencyRewrite(BaseModel):
    """一条外部依赖改写：从 dependent_id 上移除对候选开关的依赖边。"""

    model_config = ConfigDict(extra="forbid")

    dependent_id: str
    removed_dependency: str


class RetirementStatus(str, enum.Enum):
    """下线方案总状态。"""

    READY = "ready"
    BLOCKED = "blocked"


class RetirementPlanResponse(BaseModel):
    """POST /flags/retirement-plan 响应。

    status=ready：先应用 rewrites，再按 deletion_layers 逐层删除候选，
    result_config 为应用全部步骤后的完整配置（每步均通过 /validate）。
    status=blocked：仅给出全部 blockers，不提供任何部分方案
    （rewrites/deletion_layers 为空，result_config 为 null）。
    """

    model_config = ConfigDict(extra="forbid")

    status: RetirementStatus
    blockers: list[RetirementBlocker] = Field(default_factory=list)
    rewrites: list[DependencyRewrite] = Field(default_factory=list)
    deletion_layers: list[list[str]] = Field(default_factory=list)
    result_config: FlagConfig | None = None


# ---------------------------------------------------------------------------
# SDK 决策日志核对（配置快照激活时间窗 + 宽限 + 时钟偏差，重算决策比对）
# ---------------------------------------------------------------------------


class ConfigSnapshot(BaseModel):
    """配置快照：某一版配置及其激活时刻（UTC）。

    摘要不在请求中给出，由服务端沿用制品的规范化 SHA-256 重算，
    节点事件上报的 digest 据此识别所用版本。
    """

    model_config = ConfigDict(extra="forbid")

    config: FlagConfig
    activated_at: UtcDatetime


class NodeDecisionEvent(BaseModel):
    """SDK 节点上报的一次本地决策：所报摘要、评估入参与评估结果三元组。

    variant_id / served_default / reason 为节点本地记录的评估结果，
    服务端对可识别摘要复用评估器重算后逐字段比对。
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    timestamp: UtcDatetime
    digest: str = Field(min_length=1)
    switch_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    variant_id: str = Field(min_length=1)
    served_default: bool
    reason: str | None = None


class ReconcileRequest(BaseModel):
    """POST /sdk/reconcile 请求：配置快照史 + 宽限 + 时钟偏差 + 节点事件。

    - 快照按 activated_at 排序后定义各摘要的允许区间：新版激活至下一版激活，
      旧版合法上报延续到“下一版激活 + grace_seconds”；
    - 事件时间按 clock_skew_seconds 扩为 ±偏差闭区间参与时间分类；
    - 事件全局按 event_id 去重：同 ID 内容完全一致只计一次，内容冲突 → 422；
    - activated_at 不允许重复（同一时刻两版激活会让允许区间无定义）。
      快照摘要重复（同配置再次激活）与事件引用快照中未知开关在业务层拒绝（422）。
    """

    model_config = ConfigDict(extra="forbid")

    snapshots: list[ConfigSnapshot] = Field(min_length=1)
    grace_seconds: int = Field(ge=0)
    clock_skew_seconds: int = Field(ge=0)
    events: list[NodeDecisionEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_request(self) -> "ReconcileRequest":
        activated: set[datetime] = set()
        for snapshot in self.snapshots:
            if snapshot.activated_at in activated:
                raise ValueError("配置快照的 activated_at 不允许重复")
            activated.add(snapshot.activated_at)

        # event_id -> 归一化内容；时间已归一到 UTC，context 按规范化 JSON 判等。
        seen: dict[str, tuple] = {}
        for event in self.events:
            content = (
                event.node_id,
                event.timestamp,
                event.digest,
                event.switch_id,
                event.user_id,
                _canonical_json(event.context),
                event.variant_id,
                event.served_default,
                event.reason,
            )
            previous = seen.get(event.event_id)
            if previous is not None and previous != content:
                raise ValueError(f"事件 ID 内容冲突: '{event.event_id}'")
            seen[event.event_id] = content
        return self


class ReconcileClassification(str, enum.Enum):
    """单条节点事件相对所报摘要允许区间的时间分类。"""

    CONSISTENT = "consistent"
    AMBIGUOUS_TIME = "ambiguous_time"
    EARLY_VERSION = "early_version"
    STALE_VERSION = "stale_version"
    UNKNOWN_DIGEST = "unknown_digest"


class RecomputedDecision(BaseModel):
    """服务端复用评估器在事件所报快照配置上重算的结果三元组。"""

    model_config = ConfigDict(extra="forbid")

    variant_id: str
    served_default: bool
    reason: str | None = None


class ReconcileResult(BaseModel):
    """单条事件的核对结果。

    unknown_digest 时 recomputed 为 null、decision_mismatch 为 null
    （没有可用于重算的配置）；其余分类均给出重算结果，三元组任一不符
    即 decision_mismatch=true。
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str
    node_id: str
    timestamp: UtcDatetime
    digest: str
    classification: ReconcileClassification
    decision_mismatch: bool | None = None
    recomputed: RecomputedDecision | None = None


class NodeReconcileSummary(BaseModel):
    """单个节点的事件计数：五种分类合计等于 events，decision_mismatches 独立计数。"""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    events: int = Field(default=0, ge=0)
    consistent: int = Field(default=0, ge=0)
    ambiguous_time: int = Field(default=0, ge=0)
    early_version: int = Field(default=0, ge=0)
    stale_version: int = Field(default=0, ge=0)
    unknown_digest: int = Field(default=0, ge=0)
    decision_mismatches: int = Field(default=0, ge=0)


class ReconcileResponse(BaseModel):
    """POST /sdk/reconcile 响应。

    results 按 (node_id, timestamp, event_id) 升序；
    nodes 按 node_id 升序；event_count 为去重后的事件数。
    """

    model_config = ConfigDict(extra="forbid")

    event_count: int = Field(ge=0)
    results: list[ReconcileResult] = Field(default_factory=list)
    nodes: list[NodeReconcileSummary] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# SDK 配置制品分波铺开规划（并发/故障域/全局与分区不可用权重约束）
# ---------------------------------------------------------------------------

# 单次规划的节点数上限：规划问题按节点子集做精确枚举，2^16 仍可接受。
MAX_ROLLOUT_NODES = 16


class RolloutNode(BaseModel):
    """待铺开节点：当前摘要不同于 target_digest 才需要更新。

    traffic_weight 为正整数流量权重；同一 failure_domain 的节点同时不可用
    会让该故障域完全失联，因此每波至多从同一 failure_domain 选一个节点。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    current_digest: str = Field(min_length=1)
    region: str = Field(min_length=1)
    failure_domain: str = Field(min_length=1)
    traffic_weight: int = Field(gt=0)


class RolloutPlanRequest(BaseModel):
    """POST /sdk/rollout-plan 请求。

    region_max_unavailable_weight 给出各 region 每波允许的最大不可用流量
    权重；节点的 region 必须在其中声明（未知分区限额拒绝，422），
    未被任何节点使用的限额声明允许保留。
    """

    model_config = ConfigDict(extra="forbid")

    target_digest: str = Field(min_length=1)
    nodes: list[RolloutNode] = Field(min_length=1, max_length=MAX_ROLLOUT_NODES)
    max_parallel: int = Field(gt=0)
    max_unavailable_weight: int = Field(gt=0)
    region_max_unavailable_weight: dict[str, int] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_request(self) -> "RolloutPlanRequest":
        # 节点 ID 全局唯一。
        seen: set[str] = set()
        for node in self.nodes:
            if node.id in seen:
                raise ValueError(f"节点 ID 重复: '{node.id}'")
            seen.add(node.id)

        # 分区限额键不允许空串，值必须为正整数。
        for region, limit in self.region_max_unavailable_weight.items():
            if not region:
                raise ValueError("分区名不允许为空串")
            if limit <= 0:
                raise ValueError(f"分区 '{region}' 的每波最大不可用权重必须为正整数")

        # 节点所在分区必须声明限额（未知分区限额 → 422）。
        for node in self.nodes:
            if node.region not in self.region_max_unavailable_weight:
                raise ValueError(
                    f"节点 '{node.id}' 的分区缺少限额声明: '{node.region}'"
                )
        return self


class RolloutBlockerKind(str, enum.Enum):
    """分波铺开的阻断原因类型。"""

    GLOBAL_WEIGHT_EXCEEDED = "global_weight_exceeded"
    REGION_WEIGHT_EXCEEDED = "region_weight_exceeded"


class RolloutBlocker(BaseModel):
    """一个阻断项：该节点单独更新（单独占一波）即超限。

    kind=global_weight_exceeded：节点流量权重大于全局每波最大不可用权重；
    kind=region_weight_exceeded：节点流量权重大于其分区每波最大不可用权重。
    """

    model_config = ConfigDict(extra="forbid")

    kind: RolloutBlockerKind
    node_id: str
    region: str
    traffic_weight: int = Field(gt=0)
    limit: int = Field(gt=0)


class RolloutWave(BaseModel):
    """一个铺开波：波内节点同时更新（同时不可用）。

    node_ids 按 ID 升序；global_unavailable_weight 为本波全部节点流量权重
    之和，region_unavailable_weight 为本波实际涉及分区的权重和（按分区名
    升序）；completed_node_ids 为含本波在内累计已完成的全部节点
    （含请求时摘要已等于 target_digest 的节点），completed_node_count 为
    其数量。
    """

    model_config = ConfigDict(extra="forbid")

    wave: int = Field(ge=1)
    node_ids: list[str] = Field(min_length=1)
    global_unavailable_weight: int = Field(ge=1)
    region_unavailable_weight: dict[str, int] = Field(default_factory=dict)
    completed_node_ids: list[str] = Field(default_factory=list)
    completed_node_count: int = Field(ge=1)


class RolloutStatus(str, enum.Enum):
    """铺开规划总状态。"""

    PLANNED = "planned"
    BLOCKED = "blocked"


class RolloutPlanResponse(BaseModel):
    """POST /sdk/rollout-plan 响应。

    status=planned：waves 为波次最少的精确规划，并列时把每波 ID 升序后
    取波次序列字典序最小者；status=blocked：只返回 blockers 与
    up_to_date_nodes，不给出任何部分计划（waves 为空）。
    """

    model_config = ConfigDict(extra="forbid")

    target_digest: str
    status: RolloutStatus
    up_to_date_nodes: list[str] = Field(default_factory=list)
    planned_node_count: int = Field(ge=0)
    waves: list[RolloutWave] = Field(default_factory=list)
    blockers: list[RolloutBlocker] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# SDK 分波铺开执行审计（事件序贯折叠 + 每波独立复用序贯护栏）
# ---------------------------------------------------------------------------

# 与 /guardrails/evaluate 一致：每波最多声明 8 个护栏指标。
MAX_ROLLOUT_AUDIT_METRICS = 8


class RolloutGuardrailSpec(BaseModel):
    """每波独立复用的序贯护栏指标声明（参数同 GuardrailMetric，不含批次）。

    各波的指标批次从空开始独立累计；metric_batch 事件按 wave 分别喂入，
    判定路径由 /guardrails/evaluate 同一套 Wald SPRT 逻辑计算。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    baseline_rate: float = Field(gt=0, lt=1)
    max_increase: float = Field(gt=0)
    alpha: float = Field(gt=0, lt=1)
    beta: float = Field(gt=0, lt=1)

    @model_validator(mode="after")
    def _check_rates(self) -> "RolloutGuardrailSpec":
        if self.baseline_rate + self.max_increase >= 1:
            raise ValueError("baseline_rate + max_increase 必须小于 1")
        if self.alpha + self.beta >= 1:
            raise ValueError("alpha + beta 必须小于 1")
        return self


class RolloutAuditEventType(str, enum.Enum):
    """铺开执行事件类型：波启动、节点完成、指标批次。"""

    WAVE_STARTED = "wave_started"
    NODE_COMPLETED = "node_completed"
    METRIC_BATCH = "metric_batch"


class RolloutAuditEvent(BaseModel):
    """一条执行审计事件，所有事件带唯一 event_id 与 sequence。

    - wave_started：仅使用 wave（启动的波次号，1 起始）；
    - node_completed：使用 wave 与 node_id（节点在所属波启动后才能完成）；
    - metric_batch：使用 wave / metric_id / failures / total，
      每波全部节点完成后才可接收，batch 为该波该指标的 1 起始递增批次号。
    类型无关字段必须缺省，避免一条事件表达多个含义。
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    type: RolloutAuditEventType
    wave: int | None = Field(default=None, ge=1)
    node_id: str | None = None
    metric_id: str | None = None
    batch: int | None = Field(default=None, ge=1)
    failures: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check_shape(self) -> "RolloutAuditEvent":
        if self.type == RolloutAuditEventType.WAVE_STARTED:
            if self.wave is None:
                raise ValueError("wave_started 事件必须给出 wave")
            if self.node_id is not None or self.metric_id is not None:
                raise ValueError("wave_started 事件不允许携带 node_id / metric_id")
            if self.batch is not None or self.failures is not None or self.total is not None:
                raise ValueError("wave_started 事件不允许携带批次字段")
        elif self.type == RolloutAuditEventType.NODE_COMPLETED:
            if self.wave is None or self.node_id is None or not self.node_id:
                raise ValueError("node_completed 事件必须给出 wave 与非空 node_id")
            if self.metric_id is not None:
                raise ValueError("node_completed 事件不允许携带 metric_id")
            if self.batch is not None or self.failures is not None or self.total is not None:
                raise ValueError("node_completed 事件不允许携带批次字段")
        else:
            missing = [
                name
                for name, value in (
                    ("wave", self.wave),
                    ("metric_id", self.metric_id),
                    ("batch", self.batch),
                    ("failures", self.failures),
                    ("total", self.total),
                )
                if value is None or name == "metric_id" and not value
            ]
            if missing:
                raise ValueError(
                    f"metric_batch 事件缺少字段: {', '.join(missing)}"
                )
            if self.node_id is not None:
                raise ValueError("metric_batch 事件不允许携带 node_id")
            if self.failures > self.total:  # type: ignore[operator]
                raise ValueError("failures 不能大于 total")
        return self


class RolloutAuditRequest(BaseModel):
    """POST /sdk/rollout-audit 请求：status=planned 原计划 + 护栏声明 + 事件流。

    事件按 sequence 折叠重放（请求内可乱序提交）；同一 event_id 同内容
    去重，内容冲突 → 422；sequence 不允许重复 → 422。
    """

    model_config = ConfigDict(extra="forbid")

    plan: RolloutPlanResponse
    guardrails: list[RolloutGuardrailSpec] = Field(
        min_length=1, max_length=MAX_ROLLOUT_AUDIT_METRICS
    )
    events: list[RolloutAuditEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_request(self) -> "RolloutAuditRequest":
        # 只接受 status=planned 的原计划：blocked 计划没有可执行波次。
        if self.plan.status != RolloutStatus.PLANNED:
            raise ValueError("只接受 status=planned 的原计划")
        if self.plan.blockers:
            raise ValueError("planned 计划不允许携带 blockers")

        # 计划内部一致性：波次号从 1 连续，节点不跨波重复，
        # planned_node_count 等于各波节点数。
        wave_node_sets: list[set[str]] = []
        for index, wave in enumerate(self.plan.waves, start=1):
            if wave.wave != index:
                raise ValueError(f"计划波次号必须从 1 连续：期望 {index}，实际 {wave.wave}")
            ids = wave.node_ids
            if len(ids) != len(set(ids)):
                raise ValueError(f"第 {index} 波的 node_ids 存在重复")
            wave_node_sets.append(set(ids))
        all_planned = [node_id for wave_ids in wave_node_sets for node_id in wave_ids]
        if len(all_planned) != len(set(all_planned)):
            raise ValueError("同一节点不允许出现在多个波次")
        if len(all_planned) != self.plan.planned_node_count:
            raise ValueError(
                "planned_node_count 与各波 node_ids 的节点总数不一致"
            )
        overlap = set(all_planned) & set(self.plan.up_to_date_nodes)
        if overlap:
            raise ValueError(
                "已达目标节点不允许再出现在波次中: "
                + ", ".join(sorted(overlap))
            )

        # 护栏指标 ID 唯一。
        metric_ids: set[str] = set()
        for spec in self.guardrails:
            if spec.id in metric_ids:
                raise ValueError(f"护栏指标 ID 重复: '{spec.id}'")
            metric_ids.add(spec.id)

        # 事件引用完整性（业务引用错误属于请求不合法，422）。
        wave_count = len(self.plan.waves)
        up_to_date = set(self.plan.up_to_date_nodes)
        for event in self.events:
            if event.wave is not None and not 1 <= event.wave <= wave_count:
                raise ValueError(
                    f"事件 '{event.event_id}' 引用了计划外波次: {event.wave}"
                )
            if event.type == RolloutAuditEventType.NODE_COMPLETED:
                wave_ids = wave_node_sets[event.wave - 1]  # type: ignore[operator]
                if event.node_id not in wave_ids:
                    raise ValueError(
                        f"事件 '{event.event_id}' 的节点 '{event.node_id}' "
                        f"不属于第 {event.wave} 波"
                    )
            if event.type == RolloutAuditEventType.METRIC_BATCH:
                if event.metric_id not in metric_ids:
                    raise ValueError(
                        f"事件 '{event.event_id}' 引用了未声明的护栏指标: "
                        f"'{event.metric_id}'"
                    )

        # event_id 折叠键：同 ID 内容必须完全一致（含 sequence 与类型相关字段）。
        seen: dict[str, tuple] = {}
        for event in self.events:
            content = (
                event.sequence,
                event.type,
                event.wave,
                event.node_id,
                event.metric_id,
                event.batch,
                event.failures,
                event.total,
            )
            previous = seen.get(event.event_id)
            if previous is not None and previous != content:
                raise ValueError(f"事件 ID 内容冲突: '{event.event_id}'")
            seen[event.event_id] = content

        # 去重后 sequence 必须唯一。
        sequences = {event.sequence for event_id, event in self._deduped().items()}
        if len(sequences) != len(seen):
            raise ValueError("事件 sequence 不允许重复")
        return self

    def _deduped(self) -> dict[str, RolloutAuditEvent]:
        """按 event_id 去重（保留首个）；调用时内容冲突已由校验拒绝。"""
        unique: dict[str, RolloutAuditEvent] = {}
        for event in self.events:
            unique.setdefault(event.event_id, event)
        return unique


class RolloutAuditStatus(str, enum.Enum):
    """铺开执行总状态。"""

    RUNNING = "running"
    COMPLETED = "completed"
    ROLLBACK_REQUIRED = "rollback_required"


class RolloutWaveStatus(str, enum.Enum):
    """单个波次的执行阶段。"""

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_METRICS = "awaiting_metrics"
    PROMOTED = "promoted"
    ROLLBACK_REQUIRED = "rollback_required"


class RolloutViolationKind(str, enum.Enum):
    """序贯执行违规类型（遇到首个即停止重放）。"""

    EVENT_AFTER_ROLLBACK = "event_after_rollback"
    WAVE_SKIPPED = "wave_skipped"
    WAVE_ALREADY_STARTED = "wave_already_started"
    NODE_BEFORE_WAVE_START = "node_before_wave_start"
    NODE_ALREADY_COMPLETED = "node_already_completed"
    METRICS_BEFORE_WAVE_START = "metrics_before_wave_start"
    METRICS_BEFORE_NODES_COMPLETE = "metrics_before_nodes_complete"
    METRIC_BATCH_GAP = "metric_batch_gap"
    METRIC_FROZEN = "metric_frozen"
    METRICS_AFTER_PROMOTE = "metrics_after_promote"


class RolloutViolation(BaseModel):
    """首个违规事件及其事前状态（违规事件不改变状态）。"""

    model_config = ConfigDict(extra="forbid")

    kind: RolloutViolationKind
    message: str
    event: RolloutAuditEvent


class RolloutWaveAudit(BaseModel):
    """单个波次的执行审计视图。

    pending 阶段 metrics 为空；启动后给出各指标独立累计的判定与路径；
    decision 为该波护栏总决策（running/未收批次时为 null）。
    """

    model_config = ConfigDict(extra="forbid")

    wave: int = Field(ge=1)
    status: RolloutWaveStatus
    started: bool = False
    completed_node_ids: list[str] = Field(default_factory=list)
    batch_count: int = Field(default=0, ge=0)
    decision: OverallDecision | None = None
    metrics: list[GuardrailMetricResult] = Field(default_factory=list)


class RolloutAuditState(BaseModel):
    """截至某一时点的铺开执行状态（违规时为事前状态快照）。

    all_completed_node_ids 含计划中已完成节点与原计划 up_to_date_nodes。
    """

    model_config = ConfigDict(extra="forbid")

    status: RolloutAuditStatus
    current_wave: int | None = None
    all_completed_node_ids: list[str] = Field(default_factory=list)
    replayed_event_count: int = Field(default=0, ge=0)
    waves: list[RolloutWaveAudit] = Field(default_factory=list)


class RolloutAuditResponse(BaseModel):
    """POST /sdk/rollout-audit 响应。

    valid=false 时 violation 给出首个违规事件，state 为该事件处理前的
    事前状态；valid=true 时 state 为折叠完全部合法事件后的状态。
    """

    model_config = ConfigDict(extra="forbid")

    valid: bool
    state: RolloutAuditState
    violation: RolloutViolation | None = None
