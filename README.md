# 灰度发布决策 API

基于 **FastAPI + Pydantic** 的灰度发布（Feature Flag / Rollout）决策服务。
通过「开关、变体、依赖、有序规则」描述一份灰度配置，服务提供三类能力：

- **校验**：依赖图（未知依赖、环）、规则引用、权重总和；
- **评估**：对某个用户沿依赖图迭代计算（显式栈，深度不受 Python 递归
  上限约束）依赖与有序属性规则，命中后用
  `SHA-256(盐值, 开关 ID, 用户 ID)` 映射到 `0～9999` 的桶，按累计权重区间选取变体；
- **最小充分上下文**：为一次评估求最小充分属性集合——先以当前 context
  得到基准结果，再按（属性数升序、属性名字典序）枚举保留集合，对其余属性的
  域内所有候选 JSON 值与缺失态逐一重评，仅 `variant_id / served_default /
  reason` 始终与基准一致才算充分，返回首个最小集合、移除属性与已检查组合数；
- **序贯护栏**：对二项失败率指标做 Wald 序贯概率比检验（SPRT），
  逐批累计对数似然比，给出 `rollback / promote / hold` 的发布级决策；
- **制品**：把校验通过的配置签发为带 SHA-256 摘要与 HMAC-SHA256 签名的
  配置制品，并可离线风格地验签（防篡改 + 时间窗）；
- **多人审批**：按 `sequence` 折叠 approve/reject/revoke 事件流，
  否决角色一票即 `blocked`，否则把有效赞成票按角色匹配分配到审批组，
  判定所有组能否同时达到 `min_approvals`，输出 `approved / pending / blocked`；
- **实验归因**：对乱序的曝光/转化事件按 `event_id` 全局去重并排序，
  把每笔转化归给归因窗口内不晚于它的最近曝光（变体切换后的半开洗脱区间内
  记为 `contaminated`），输出各变体的去重曝光用户数、转化数与 value 合计；
- **发布窗口求解**：给定多份含 IANA 时区的地区维护日历（本地按 weekday
  定义、可跨午夜的周期窗口 + UTC 黑窗），计算查询范围内所有日历**共同**
  可发布的 UTC 半开区间，正确处理春季跳时（不存在时刻顺延）与秋季重复
  （开始取较早、结束取较晚），输出各日历贡献的规则 ID 与发生过的 DST 调整；
- **权重重排**：为变体权重调整生成**尽量少扰动现有用户**的连续分桶方案——
  当前有序 allocations 的区间布局固定，枚举目标变体顺序（允许新增/删除，
  最多 8 个），以分配结果不变的桶数最大为优先、目标 ID 序列字典序打破平局，
  输出推荐顺序、闭区间分段、稳定/迁移桶数、`from_variant -> to_variant`
  迁移矩阵与同向相邻合并的迁移区间；
- **开关下线方案**：为已关闭（`enabled=false`）的开关生成可执行的下线
  计划——关闭开关的默认变体视为终态，外部依赖仅当显式期望终态时才自动
  改写移除，否则与活动客户端引用一起列为 blocker（有 blocker 不出部分
  方案）；无 blocker 时先改写外部依赖，再按“依赖方先于被依赖方”分层
  删除候选（同层 ID 升序），结果配置逐步通过现有校验；
- **决策日志核对**：核对 SDK 节点上报的本地决策日志——为每版配置快照
  重算制品规范化 SHA-256 摘要，按激活时刻与宽限得到各摘要的允许区间，
  把事件时间扩为 ±时钟偏差区间后分类（`consistent / ambiguous_time /
  early_version / stale_version / unknown_digest`），并对可识别摘要
  复用评估器重算 `variant_id / served_default / reason` 比对节点上报值；
- **制品分波铺开**：把一份配置制品（`target_digest`）安全铺开到 1～16 个
  SDK 节点——只规划当前摘要不同的节点，同一波节点同时不可用，受
  `max_parallel`、全局与各 region 每波最大不可用流量权重及故障域
  （同一 `failure_domain` 每波至多一个节点）约束，用节点子集精确枚举
  求出**最少波次**，多解时每波 ID 升序后取波次序列**字典序最小**者；
  任一节点单独更新仍超全局或其分区权重上限时返回 `blockers`，不出部分计划。

响应包含**最终变体、命中轨迹（trace）与失败原因（reason）**。

---

## 1. 一条命令启动（Docker Compose，端口 8000）

```bash
docker compose up --build
```

启动后服务监听 **8000** 端口：

- 服务地址：<http://localhost:8000>
- 健康检查：<http://localhost:8000/health>
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- OpenAPI 描述：<http://localhost:8000/openapi.json>

停止：

```bash
docker compose down
```

### 本地（非 Docker）运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 签名密钥（可选，仅制品接口需要）

制品签发/验签的密钥**只**从环境变量 `SIGNING_KEYS` 读取，值为
`key_id -> secret` 的 JSON 映射：

```bash
export SIGNING_KEYS='{"key-a": "secret-alpha", "key-b": "secret-beta"}'
```

Docker Compose 下通过 shell 环境透传：

```bash
SIGNING_KEYS='{"key-a": "secret-alpha"}' docker compose up --build
```

未配置时服务照常启动，`/validate`、`/evaluate` 不受影响；
`/artifacts/sign` 会返回 404，`/artifacts/verify` 会判定 `unknown_key`。

---

## 2. 数据模型

| 对象 | 说明 |
| --- | --- |
| `FlagConfig` | 整份配置，包含若干开关 |
| `Switch` | 开关：`id`、`enabled`、`salt`、`default_variant`、`variants`、`dependencies`、`rules` |
| `Variant` | 变体：`id` + 可选 `payload`（任意 JSON，原样透传） |
| `Dependency` | 依赖另一开关：`switch` + 可选 `variant`（期望变体） |
| `Rule` | 有序规则：`id`、`conditions`（AND）、`allocations`（权重） |
| `Condition` | 属性条件：`attribute`、`operator`、`value` |
| `Allocation` | 权重项：`variant_id`、`weight`（0～10000，同一条规则内总和必须为 **10000**） |

条件支持的运算符：

`eq`、`neq`、`in`、`not_in`、`contains`、`gt`、`gte`、`lt`、`lte`、`exists`。

- `conditions` 为空数组表示**无条件命中**（适合作为末尾兜底规则）。
- 规则按数组顺序求值，**首条全部条件命中的规则生效**。
- 依赖语义：
  - 不指定 `variant`：被依赖开关解析到**任意非默认变体**即满足；
  - 指定 `variant`：被依赖开关必须**恰好**解析到该变体才满足；
  - 任一依赖不满足 → 返回默认变体，`reason = dependency_not_satisfied`；
  - 依赖按声明顺序求值，**首个不满足即短路**（后续依赖不再评估）；
  - 依赖图遍历（环检测、评估、传递闭包）均为**显式栈迭代实现**：
    合法依赖链的深度不受 Python 递归上限约束（数千层深链照常校验与评估），
    也不设链长上限。
- `salt` 缺省时回退为开关 `id`。

---

## 3. 接口

### `POST /validate` — 校验配置

请求体就是一份 `FlagConfig`。返回：

```json
{ "valid": true, "errors": [] }
```

校验不通过时 `valid=false`，`errors` 给出 `code / message / path`，错误码包括：

| code | 含义 |
| --- | --- |
| `duplicate_switch` | 开关 ID 重复 |
| `duplicate_variant` | 开关内变体 ID 重复 |
| `duplicate_rule` | 开关内规则 ID 重复 |
| `unknown_default_variant` | 默认变体未在 variants 中声明 |
| `unknown_rule_variant` | 规则引用了不存在的变体 |
| `duplicate_allocation` | 同一条规则内变体被重复分配 |
| `invalid_weight_sum` | 规则权重总和不等于 10000 |
| `unknown_dependency` | 依赖了不存在的开关（未知依赖） |
| `unknown_dependency_variant` | 依赖的期望变体在目标开关中不存在 |
| `dependency_cycle` | 依赖图存在环（含自依赖） |

请求示例：

```bash
curl -s http://localhost:8000/validate \
  -H 'Content-Type: application/json' \
  --data @examples/config.json
```

### `POST /evaluate` — 评估变体

请求体：

```json
{
  "config": { "...": "完整 FlagConfig（同 /validate 的请求体）" },
  "switch_id": "new_checkout",
  "user_id": "user-10086",
  "context": { "tier": "gold", "age": 27 }
}
```

评估流程：

1. 配置未通过校验 → `422`（携带全部校验错误）；开关不存在 → `404`；
2. `enabled=false` → 默认变体（`switch_disabled`）；
3. 按声明顺序**迭代**计算每个依赖（显式栈替代递归，深链不触发递归上限）；
   任一不满足 → 默认变体（`dependency_not_satisfied`）；
4. 按顺序求规则：首条条件全中的规则生效；没有命中 → 默认变体（`no_rule_matched`）；
5. 计算
   `bucket = int(sha256(f"{salt}:{switch_id}:{user_id}").hexdigest()[:8], 16) % 10000`，
   得到 `0～9999` 的稳定桶号；
6. 按规则内 allocations 的**累计权重区间**选取变体（同一用户永远落在同一桶，结果稳定）。

请求示例（直接使用仓库自带示例配置）：

```bash
curl -s http://localhost:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d "$(jq -c '{config: ., switch_id: "new_checkout", user_id: "user-10086", context: {tier: "gold", age: 27}}' examples/config.json)"
```

响应示例（桶号随用户 ID 确定，同一用户结果稳定）：

```json
{
  "switch_id": "new_checkout",
  "variant_id": "off",
  "payload": {"ui": "classic_checkout", "coupon": false},
  "served_default": false,
  "reason": null,
  "trace": [
    {"step": "start", "detail": {"switch_id": "new_checkout", "enabled": true}},
    {
      "step": "rule_evaluated",
      "detail": {
        "rule_id": "internal_testers",
        "conditions": [
          {"attribute": "tier", "operator": "eq", "value": "staff", "matched": false}
        ],
        "matched": false
      }
    },
    {
      "step": "rule_evaluated",
      "detail": {
        "rule_id": "gold_users_half",
        "conditions": [
          {"attribute": "tier", "operator": "eq", "value": "gold", "matched": true},
          {"attribute": "age", "operator": "gte", "value": 18, "matched": true}
        ],
        "matched": true
      }
    },
    {"step": "rule_matched", "detail": {"rule_id": "gold_users_half"}},
    {
      "step": "bucket",
      "detail": {
        "salt": "checkout-rollout-2026",
        "user_id": "user-10086",
        "bucket": 7588,
        "buckets": 10000,
        "ranges": [
          {"variant_id": "on", "start": 0, "end": 4999, "weight": 5000},
          {"variant_id": "off", "start": 5000, "end": 9999, "weight": 5000}
        ]
      }
    },
    {"step": "variant_selected", "detail": {"variant_id": "off"}}
  ]
}
```

返回默认变体时，`served_default=true`，`reason` 为失败原因，trace 末尾带
`default` 步骤。依赖逐层产生的轨迹以 `dep:<被依赖开关>:` 为前缀（嵌套依赖
前缀逐层拼接）。

### `POST /explanations/minimize` — 最小充分上下文

为一次评估求**最小充分上下文**：在评估结果不变的前提下，当前 context 中
最少需要保留哪些属性。请求复用 `/evaluate` 的 `config / switch_id /
user_id / context`，并为每个相关属性声明有限域：

```json
{
  "config": { "...": "完整 FlagConfig（同 /validate 的请求体）" },
  "switch_id": "new_checkout",
  "user_id": "user-10086",
  "context": { "tier": "gold", "region": "cn-north" },
  "domains": [
    {"attribute": "tier", "values": ["gold", "silver"], "allow_missing": false},
    {"attribute": "region", "values": ["cn-north", "cn-south"], "allow_missing": true}
  ]
}
```

- 每个域给出候选 JSON 值 `values` 与是否允许缺失 `allow_missing`
  （缺失态等价于 context 中不含该键，覆盖 `exists` 与其他运算符的“未提供”分支）；
- `domains` 必须**恰好覆盖**目标开关及其**传递依赖**（沿依赖边迭代遍历）的规则
  条件所引用的全部属性：被引用但未声明域、声明了未被引用的属性都返回 `422`。
  这与评估时是否真正走到某条规则无关——即使一条规则在基准下被首条规则遮蔽，
  它引用的属性仍需声明域；
- 当前 `context` 中凡声明了域的属性，其值必须落在域内（按规范化 JSON 判等，
  键序不同视为同值）；未出现在 `context` 中的属性，其域必须允许缺失；
- 组合上限：所有域的 `(候选值数 + 缺失态)` 笛卡尔积 ≤ **4096**，超出 → `422`；
- context 中未被引用的键不参与枚举，在所有试算中原样保留。

求解流程：

1. 复用与 `/evaluate` 相同的配置校验与评估：配置未通过校验 → `422`
   （携带全部校验错误），开关不存在 → `404`；以当前 context 得到**基准结果**；
2. 按**属性数升序、同数时属性名字典序**枚举保留集合；
3. 对每个保留集合，把其余属性按属性名字典序展开为“所有域值 × 缺失态”的
   组合逐一重评；仅当 `variant_id`、`served_default` 与 `reason` **始终**
   与基准一致，该集合才算充分（任一组合不一致即短路，进入下一个保留集合）；
4. 返回枚举到的**首个**最小集合——并列最小集合按字典序取前者。

响应（`200`）：

```json
{
  "switch_id": "new_checkout",
  "user_id": "user-10086",
  "baseline_variant_id": "on",
  "baseline_served_default": false,
  "baseline_reason": null,
  "kept_attributes": ["tier"],
  "removed_attributes": ["region"],
  "checked_combinations": 6
}
```

| 字段 | 说明 |
| --- | --- |
| `baseline_variant_id` / `baseline_served_default` / `baseline_reason` | 当前 context 的基准评估三元组，试算结果始终与之比较 |
| `kept_attributes` | 首个最小充分集合（属性名升序） |
| `removed_attributes` | 被移除、对结果无影响的属性 |
| `checked_combinations` | 搜索中实际重评的组合数（含空移除集合的一次基准复算） |

注意充分性比较的是整个三元组：两个变体即使 `variant_id` 相同，只要
`served_default`（规则选出 vs. 默认兜底）或 `reason`（如 `no_rule_matched`
与 `dependency_not_satisfied`）不同，仍判定为不充分。

请求校验（不满足时返回 `422`）：

| 规则 | 说明 |
| --- | --- |
| 覆盖 | 域必须恰好覆盖目标开关及传递依赖引用的全部属性（未覆盖、多声明均拒绝） |
| 重复 | 同一属性的域不允许重复声明；同一域内候选值（规范化 JSON 判等）不允许重复 |
| 空域 | `values` 为空且 `allow_missing=false` 的域拒绝 |
| 当前值 | 声明了域的属性当前值必须在域内；未提供的属性其域必须允许缺失 |
| 组合上限 | 全部域状态（候选值 + 缺失态）的笛卡尔积 ≤ 4096（恰好 4096 合法） |
| 额外字段 | 请求与域对象均拒绝未声明字段 |
| 配置/开关 | 复用 `/validate` 校验，未通过 → `422`；开关不存在 → `404` |

请求示例：

```bash
curl -s http://localhost:8000/explanations/minimize \
  -H 'Content-Type: application/json' \
  -d "$(jq -c '{config: ., switch_id: "new_checkout", user_id: "user-10086", context: {tier: "gold", region: "cn-north"}, domains: [{attribute: "tier", values: ["gold", "silver"], allow_missing: false}, {attribute: "region", values: ["cn-north", "cn-south"], allow_missing: true}]}' examples/config.json)"
```

### `POST /guardrails/evaluate` — 序贯护栏评估

对 1～8 个二项失败率指标做 **Wald 序贯概率比检验（SPRT）**，用于灰度发布
期间的自动回滚/放行决策。

请求体：

```json
{
  "metrics": [
    {
      "id": "error_rate",
      "baseline_rate": 0.01,
      "max_increase": 0.01,
      "alpha": 0.05,
      "beta": 0.1,
      "batches": [
        {"failures": 1, "total": 100},
        {"failures": 12, "total": 100},
        {"failures": 0, "total": 100}
      ]
    }
  ]
}
```

- 每个指标声明 `baseline_rate`（基线失败率 p0）、`max_increase`（最大可接受
  劣化）、`alpha`（假回滚概率）与 `beta`（漏回滚概率），并给出等长的观测
  批次；每批为相对上一批的**增量** `failures/total`；
- 令 `p1 = p0 + max_increase`，逐批累计对数似然比：
  `LLR += failures·ln(p1/p0) + (total − failures)·ln((1−p1)/(1−p0))`；
- 判定阈值（Wald 近似）：`LLR ≥ ln((1−β)/α)` → **rollback**；
  `LLR ≤ ln(β/(1−α))` → **pass**；其余 → **hold**；
- 任一指标**首次越界后即冻结**：不再消费后续批次，判定、计数与似然路径
  都停在越界那一批；
- 总决策：**任一指标 rollback → `rollback`**（`stop_batch` 取各回滚指标中
  最早的批次）；**全部 pass → `promote`**（`stop_batch` 取最后放行者的批次）；
  其余 → `hold`（`stop_batch` 为 `null`）。

响应（`200`）包含计数、阈值、似然路径、停止批次与总决策：

```json
{
  "decision": "rollback",
  "stop_batch": 2,
  "metric_count": 1,
  "batch_count": 3,
  "metrics": [
    {
      "id": "error_rate",
      "decision": "rollback",
      "stop_batch": 2,
      "failures": 13,
      "total": 200,
      "p0": 0.01,
      "p1": 0.02,
      "lower_threshold": -2.2513,
      "upper_threshold": 2.8904,
      "llr": 7.1124,
      "path": [
        {"batch": 1, "failures": 1, "total": 100, "llr": -0.3119},
        {"batch": 2, "failures": 12, "total": 100, "llr": 7.1124}
      ]
    }
  ]
}
```

其中每个指标的 `failures/total` 是截至停止批次（冻结点）的累计计数，
`path` 记录每批处理后的累计 LLR（冻结后不再追加）。

请求校验（不满足时返回 `422`）：

| 规则 | 说明 |
| --- | --- |
| 指标数量 | 1～8 个 |
| 指标 ID | 不允许重复 |
| 概率范围 | `0 < baseline_rate < 1`、`max_increase > 0`、`baseline_rate + max_increase < 1`、`0 < alpha, beta < 1` 且 `alpha + beta < 1` |
| 批次 | 每个指标至少 1 批，且所有指标批次数一致（不允许缺项） |
| 计数 | `total ≥ 1`、`failures ≥ 0` 且 `failures ≤ total` |

请求示例：

```bash
curl -s http://localhost:8000/guardrails/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"metrics": [{"id": "error_rate", "baseline_rate": 0.01, "max_increase": 0.01, "alpha": 0.05, "beta": 0.1, "batches": [{"failures": 1, "total": 100}, {"failures": 12, "total": 100}]}]}'
```

### `POST /approvals/evaluate` — 多人审批判定

判断一次配置发布（以 `artifact_digest` 标识）是否已满足多人审批条件。

请求体：

```json
{
  "artifact_digest": "b7f3...",
  "roles": ["dev", "ops", "sec"],
  "people": [
    {"id": "alice", "roles": ["dev"]},
    {"id": "bob", "roles": ["ops"]},
    {"id": "carol", "roles": ["sec"]}
  ],
  "groups": [
    {"id": "dev_group", "allowed_roles": ["dev"], "min_approvals": 1},
    {"id": "ops_group", "allowed_roles": ["ops"], "min_approvals": 1}
  ],
  "veto_roles": ["sec"],
  "events": [
    {"sequence": 1, "person": "alice", "action": "approve", "artifact_digest": "b7f3..."},
    {"sequence": 2, "person": "bob", "action": "approve", "artifact_digest": "b7f3..."}
  ]
}
```

- `roles` 为角色目录；`people` 声明每个人员持有的角色（须来自目录）；
- `groups` 为审批组：持有 `allowed_roles` 中任意角色的人员的赞成票可计入该组，
  达到 `min_approvals` 人即该组达标；
- `veto_roles` 为否决角色：持有否决角色的人员的有效 `reject` 一票否决；
- `events` 为按 `sequence` **严格递增**的 `approve / reject / revoke` 事件流，
  每个事件都必须绑定请求的 `artifact_digest`。

判定流程：

1. **依序折叠事件**：每人的最新事件覆盖旧事件；`revoke` 清除本人此前一票。
   折叠后仍为 `approve / reject` 的票称为**活动票**；
2. **否决**：任一持有否决角色的人员存在有效 `reject` → 总状态直接
   `blocked`（响应的 `vetoes` 给出否决证据：人员、命中的否决角色与序号）；
3. 否则把有效 `approve` **分配**给审批组：人员须持有组的允许角色方可计入，
   **同一人最多计入一组**，判断所有组能否**同时**达到 `min_approvals`；
4. 分配方案不唯一时，取按 **(组 ID, 人员 ID) 字典序最小**的方案
   （形式化：在所有“计入总人数最多”的分配中，按组 ID 升序、人员 ID 升序
   逐对比较取最小；借助最大流判定可行性）；
5. 全部组达标 → `approved`，否则 → `pending`。

响应（`200`）包含总状态、各组计入人员与缺口、活动票与否决证据：

```json
{
  "status": "approved",
  "artifact_digest": "b7f3...",
  "groups": [
    {"id": "dev_group", "min_approvals": 1, "counted": ["alice"], "shortfall": 0},
    {"id": "ops_group", "min_approvals": 1, "counted": ["bob"], "shortfall": 0}
  ],
  "active_votes": [
    {"person": "alice", "action": "approve", "sequence": 1},
    {"person": "bob", "action": "approve", "sequence": 2}
  ],
  "vetoes": []
}
```

`groups` 按组 ID 升序，`counted` 为计入本组的人员（按 ID 升序），
`shortfall = min_approvals − 计入人数`；`active_votes` 与 `vetoes` 按人员 ID
升序。即使总状态为 `blocked`，`groups` 仍返回当前的计入情况，便于展示进度。

请求校验（不满足时返回 `422`）：

| 规则 | 说明 |
| --- | --- |
| 重复实体 | 人员 ID、审批组 ID、`sequence` 均不允许重复 |
| 事件顺序 | `sequence` 必须严格递增（乱序同重复一样拒绝） |
| 未知引用 | 人员角色、组允许角色、否决角色须在角色目录中；事件人员须在人员目录中 |
| 摘要绑定 | 每个事件的 `artifact_digest` 必须等于请求的 `artifact_digest` |
| 非法阈值 | `min_approvals ≥ 1`；`allowed_roles` 至少 1 个角色 |

请求示例：

```bash
curl -s http://localhost:8000/approvals/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"artifact_digest": "b7f3", "roles": ["dev", "sec"], "people": [{"id": "alice", "roles": ["dev"]}, {"id": "carol", "roles": ["sec"]}], "groups": [{"id": "dev_group", "allowed_roles": ["dev"], "min_approvals": 1}], "veto_roles": ["sec"], "events": [{"sequence": 1, "person": "alice", "action": "approve", "artifact_digest": "b7f3"}]}'
```

### `POST /experiments/attribute` — 实验归因（曝光 → 转化）

按灰度曝光事件为转化做**确定性归因**：给定一个开关的曝光与转化事件流
（可乱序提交），把每笔转化归给恰当的曝光/变体，并按变体汇总。

请求体：

```json
{
  "switch_id": "new_checkout",
  "window_seconds": 3600,
  "washout_seconds": 600,
  "exposures": [
    {"event_id": "e1", "user_id": "u1", "timestamp": "2026-01-01T00:00:00Z", "variant_id": "on"},
    {"event_id": "e2", "user_id": "u1", "timestamp": "2026-01-01T00:05:00Z", "variant_id": "off"}
  ],
  "conversions": [
    {"event_id": "c1", "user_id": "u1", "timestamp": "2026-01-01T00:05:30Z", "value": 9.9},
    {"event_id": "c2", "user_id": "u2", "timestamp": "2026-01-01T00:10:00Z", "value": 4.0}
  ]
}
```

- `window_seconds`：归因窗口（秒，`≥ 0`）——曝光时刻 ≤ 转化时刻且时差
  **≤ 窗口**（闭区间，恰好相等仍可归因）；`0` 表示仅同一时刻可归因；
- `washout_seconds`：洗脱时长（秒，`≥ 0`）——变体切换后的**半开**洗脱区间
  `[切换时刻, 切换时刻 + 洗脱时长)`；`0` 表示不洗脱；
- 事件都有唯一 `event_id`、`user_id` 与 UTC `timestamp`（naive 时间按 UTC
  处理，带时区时间归一到 UTC）；曝光另含 `variant_id`，转化另含非负 `value`。

处理流程：

1. **全局按 `event_id` 去重**：同一 `event_id` 的事件内容（类型、用户、时刻、
   变体/value）完全一致时只计一次；内容冲突（含曝光与转化共用同一 ID）→ `422`；
2. 按 **(user_id, timestamp, event_id)** 排序；
3. 每笔转化归给**窗口内不晚于它的最近曝光**，同一时刻有多个曝光时取
   `event_id` 最大者：
   - 无先前曝光（该用户无曝光，或转化早于其首次曝光）→ `no_exposure`；
   - 最近曝光超出归因窗口 → `expired`（不会改归更早的曝光）；
   - 落在某次**变体切换**（同一用户排序后相邻两次曝光 `variant_id` 不同）后的
     半开洗脱区间内 → `contaminated`，**不计入任何变体**（判定优先级：
     `no_exposure → expired → contaminated → attributed`）；
   - 其余 → `attributed`，计入所匹配曝光的变体；
4. 各变体汇总：**去重曝光用户数**、计入转化数与 `value` 合计
   （只有 `attributed` 转化计入）。

响应（`200`）：`variants` 按 `variant_id` 升序，`attributions` 按
`(user_id, timestamp, event_id)` 升序，输出顺序与事件提交顺序无关：

```json
{
  "switch_id": "new_checkout",
  "variants": [
    {"variant_id": "off", "exposed_users": 1, "conversions": 1, "value": 9.9},
    {"variant_id": "on", "exposed_users": 1, "conversions": 0, "value": 0.0}
  ],
  "attributions": [
    {
      "event_id": "c1",
      "user_id": "u1",
      "timestamp": "2026-01-01T00:05:30Z",
      "value": 9.9,
      "status": "attributed",
      "exposure_event_id": "e2",
      "variant_id": "off"
    },
    {
      "event_id": "c2",
      "user_id": "u2",
      "timestamp": "2026-01-01T00:10:00Z",
      "value": 4.0,
      "status": "no_exposure",
      "exposure_event_id": null,
      "variant_id": null
    }
  ]
}
```

`exposure_event_id` / `variant_id` 仅在窗口内匹配到最近曝光
（`attributed` / `contaminated`）时给出；`no_exposure` / `expired` 时为
`null`。即使变体没有计入转化，只要存在曝光也会在 `variants` 中列出。

请求校验（不满足时返回 `422`）：

| 规则 | 说明 |
| --- | --- |
| `event_id` 冲突 | 同一 `event_id` 内容不一致（含曝光与转化共用 ID） |
| 非负约束 | 转化 `value ≥ 0`、`window_seconds ≥ 0`、`washout_seconds ≥ 0` |
| 必填非空 | `switch_id`、`event_id`、`user_id`、`variant_id` 不允许空串 |
| 额外字段 | 请求与各事件对象均拒绝未声明字段 |

完全一致的重复事件（含同一时刻的不同时区写法）会被静默去重，不报 `422`。

请求示例：

```bash
curl -s http://localhost:8000/experiments/attribute \
  -H 'Content-Type: application/json' \
  -d '{"switch_id": "new_checkout", "window_seconds": 3600, "washout_seconds": 600, "exposures": [{"event_id": "e1", "user_id": "u1", "timestamp": "2026-01-01T00:00:00Z", "variant_id": "on"}], "conversions": [{"event_id": "c1", "user_id": "u1", "timestamp": "2026-01-01T00:30:00Z", "value": 9.9}]}'
```

### `POST /artifacts/sign` — 签发配置制品

请求体：

```json
{
  "config": { "...": "完整 FlagConfig（同 /validate 的请求体）" },
  "key_id": "key-a",
  "issued_at": "2026-01-01T00:00:00Z",
  "expires_at": "2026-12-31T23:59:59Z"
}
```

签发流程：

1. 先复用与 `/evaluate` 相同的配置校验，未通过 → `422`（携带全部校验错误）；
2. `key_id` 不在 `SIGNING_KEYS` 中 → `404`；`expires_at` 不晚于 `issued_at` → `422`；
3. 对配置做**规范化 JSON**（对象键按字典序、保留数组顺序、去掉空白），
   计算 `digest = SHA-256(规范化配置)`；
4. 以对应密钥对签名材料计算 `signature = HMAC-SHA256`，签名材料覆盖
   `key_id`、`issued_at`、`expires_at`、`digest` 与 `config`（同为规范化 JSON）。

响应（`200`）：

```json
{
  "artifact": {
    "config": { "...": "原配置" },
    "key_id": "key-a",
    "issued_at": "2026-01-01T00:00:00Z",
    "expires_at": "2026-12-31T23:59:59Z",
    "digest": "64 位十六进制 SHA-256",
    "signature": "64 位十六进制 HMAC-SHA256"
  }
}
```

请求示例：

```bash
curl -s http://localhost:8000/artifacts/sign \
  -H 'Content-Type: application/json' \
  -d "$(jq -c '{config: ., key_id: "key-a", issued_at: "2026-01-01T00:00:00Z", expires_at: "2026-12-31T23:59:59Z"}' examples/config.json)"
```

### `POST /artifacts/verify` — 验签配置制品

请求体为 `{"artifact": ...}`，即签发响应可原样回传。服务重算摘要与签名，
用 `hmac.compare_digest` 做常量时间比较，并检查时间窗。响应恒为 `200`：

```json
{ "valid": true, "reason": null }
```

验签失败时 `valid=false`，`reason` 取值：

| reason | 含义 |
| --- | --- |
| `unknown_key` | `key_id` 不在 `SIGNING_KEYS` 中（如密钥已轮换下线） |
| `tampered` | 配置或元数据被改动，摘要/签名对不上 |
| `not_yet_valid` | 当前时间早于 `issued_at` |
| `expired` | 当前时间晚于 `expires_at` |

时间窗为闭区间 `[issued_at, expires_at]`：恰好落在两个端点上均视为有效。
判定顺序为 `unknown_key` → `tampered` → `not_yet_valid` → `expired`。

### `POST /release-windows/resolve` — 共同可发布窗口求解

给定多份地区维护日历与一个不超过 31 天的查询范围，计算**全部日历共同**
可发布的 UTC 半开区间 `[start, end)`，并按最短时长过滤。

请求体：

```json
{
  "start": "2026-11-01T00:00:00Z",
  "end": "2026-11-02T00:00:00Z",
  "min_duration_seconds": 1800,
  "calendars": [
    {
      "calendar_id": "us-east",
      "timezone": "America/New_York",
      "weekly_windows": [
        {"rule_id": "us-window", "weekday": 6, "start_local": "01:00", "end_local": "03:00"}
      ],
      "blackouts": []
    },
    {
      "calendar_id": "eu-central",
      "timezone": "Europe/Berlin",
      "weekly_windows": [
        {"rule_id": "eu-window", "weekday": 6, "start_local": "06:00", "end_local": "08:00"}
      ],
      "blackouts": []
    }
  ]
}
```

> 2026-11-01 是美国东部夏令时结束日：纽约本地 `01:00` 发生两次，开始取
> 较早（05:00Z）、结束 `03:00` 已唯一（08:00Z）；柏林 `06:00→08:00`
> （CET，UTC+1）为 `05:00→07:00Z`，两者共同窗口即 `05:00→07:00Z`。

日历模型：

| 字段 | 说明 |
| --- | --- |
| `calendar_id` | 日历 ID，请求内唯一 |
| `timezone` | IANA 时区名（如 `America/New_York`、`Asia/Tokyo`、`UTC`），非法时区 → `422` |
| `weekly_windows[].weekday` | `0=周一 … 6=周日` |
| `weekly_windows[].start_local` / `end_local` | 本地**墙上时间** `HH:MM`（24 小时制） |
| `weekly_windows` 跨午夜 | `end_local <= start_local` 表示结束落在**下一个本地自然日**（两者相等为空区间 → `422`） |
| `blackouts` | UTC 绝对半开黑窗 `[start, end)`，`end <= start` → `422` |
| `rule_id` | 规则 ID，在**整个请求内**唯一（周期窗口与黑窗共用一个 ID 空间），重复 → `422` |

求解语义：

1. **逐日展开**周期窗口为 UTC 区间。本地边界受夏令时影响时：
   - **春季跳时（不存在的墙上时刻）**：该边界**顺延到第一个有效本地时刻**
     （如纽约 2026-03-08 的 `02:30` 顺延为 `03:30 EDT`），并在结果中记录一次
     `gap_shifted` 调整；顺延导致区间坍塌为空（`end <= start`）时，该次
     出现被静默丢弃；
   - **秋季重复（同一墙上时刻发生两次）**：**开始取较早一次**
     （`ambiguous_earlier`）、**结束取较晚一次**（`ambiguous_later`），
     窗口因此比平时长 1 小时；
2. 同一 `(weekday, 墙上时刻)` 被多条规则共享（重复边界）时，**开始取较早、
   结束取较晚**，合并后的区间携带全部命中规则；
3. 每份日历先把展开结果按**重叠或相邻**（端点相接也算）合并，再**扣除
   blackouts**（重叠黑窗同样先合并）；切割保留原周期规则与 DST 调整出处；
4. 求**全部日历**可用区间的交集（任一日历不可发布即不可发布），再裁剪到
   查询半开范围 `[start, end)`；
5. 丢弃时长 `< min_duration_seconds` 的结果（恰好等于下限保留）。

响应（`200`）按 `(start, end)` 升序，逐窗口给出 UTC 起止、时长、各日历
贡献的周期规则 ID，以及发生过的 DST 调整（`resolved_at` 落在窗口内，
**含端点**；按 `resolved_at → calendar_id → rule_id → boundary` 排序）。
下面就是上面请求的真实返回：

```json
{
  "start": "2026-11-01T00:00:00Z",
  "end": "2026-11-02T00:00:00Z",
  "min_duration_seconds": 1800,
  "windows": [
    {
      "start": "2026-11-01T05:00:00Z",
      "end": "2026-11-01T07:00:00Z",
      "duration_seconds": 7200,
      "contributors": [
        {"calendar_id": "eu-central", "rule_ids": ["eu-window"]},
        {"calendar_id": "us-east", "rule_ids": ["us-window"]}
      ],
      "dst_adjustments": [
        {
          "calendar_id": "us-east",
          "rule_id": "us-window",
          "boundary": "start",
          "kind": "ambiguous_earlier",
          "local_time": "2026-11-01T01:00:00",
          "resolved_at": "2026-11-01T05:00:00Z"
        }
      ]
    }
  ]
}
```

- `boundary` 为 `start` / `end`，标识受影响的是窗口哪一端；
- `kind` 为 `gap_shifted`（春季顺延）/ `ambiguous_earlier`（秋季取较早）/
  `ambiguous_later`（秋季取较晚）；
- `local_time` 是规则 weekday 所落本地日期的**naive** 墙上时刻（不带时区后缀），
  `resolved_at` 是该边界最终解析到的 UTC 时刻；
- 黑窗规则只用于切割，不作为 `contributors` 的正向贡献。

请求校验（不满足时返回 `422`）：

| 规则 | 说明 |
| --- | --- |
| 查询时间 | `start` / `end` 必须**显式 UTC**（`Z` 或 `+00:00`）；naive、其它偏移一律拒绝 |
| 黑窗时间 | blackouts 的 `start` / `end` 同样必须显式 UTC |
| 查询范围 | `end > start`，跨度不超过 **31 天**（恰好 31 天合法） |
| 最短时长 | `min_duration_seconds ≥ 1`；窗口时长 `<` 该值被过滤 |
| 时区 | `timezone` 必须是合法 IANA 名 |
| 重复 ID | 日历 ID 不允许重复；规则 ID 在整个请求内（含周期窗口与黑窗）不允许重复 |
| 空区间 | 周期窗口 `start_local == end_local`、黑窗 `end <= start` 均拒绝 |
| 本地时间 | `HH:MM` 24 小时制（`00:00`～`23:59`）；`weekday` 取 0～6 |
| 日历数 | 至少 1 份日历；额外字段一律拒绝 |

注意：日历没有任何周期窗口、窗口展开后为空（如整条落在跳时坍塌中）或
被黑窗完全覆盖时，是合法请求，返回 `windows: []`（不是 `422`）。

请求示例：

```bash
curl -s http://localhost:8000/release-windows/resolve \
  -H 'Content-Type: application/json' \
  -d '{"start":"2026-03-01T00:00:00Z","end":"2026-03-15T00:00:00Z","min_duration_seconds":1800,"calendars":[{"calendar_id":"us","timezone":"America/New_York","weekly_windows":[{"rule_id":"w","weekday":1,"start_local":"20:00","end_local":"23:00"}]}]}'
```

### `POST /rollouts/rebalance` — 权重重排（最少扰动连续分桶）

为变体权重调整生成**尽量少扰动现有用户**的连续分桶方案：当前有序
allocations 的区间布局固定不变，服务端枚举目标变体顺序，找出让
“分配结果不变的桶数”最大的目标布局。

请求体：

```json
{
  "current": [
    {"variant_id": "A", "weight": 1000},
    {"variant_id": "B", "weight": 5000},
    {"variant_id": "C", "weight": 4000}
  ],
  "target": [
    {"variant_id": "A", "weight": 4500},
    {"variant_id": "B", "weight": 4000},
    {"variant_id": "C", "weight": 1500}
  ]
}
```

- 桶域固定为 `0～9999`（共 10000 桶）；`current` 按给定顺序把权重铺成
  连续闭区间，该布局固定；
- `target` 只给权重（**允许新增/删除变体**，最多 **8** 个），服务端枚举
  其全部排列，每种排列按顺序铺成一种连续分桶布局；
- 首选**稳定桶数**（同变体新旧区间重叠的桶数之和）最大的排列，并列时按
  **目标 ID 序列字典序**取最小者——结果与 `target` 的书写顺序无关。

响应（`200`，上面请求的真实返回；保持请求顺序 `(A,B,C)` 只能稳定 4000
桶，重排为 `(B,A,C)` 稳定 4500 桶）：

```json
{
  "order": ["B", "A", "C"],
  "segments": [
    {"variant_id": "B", "start": 0, "end": 3999, "weight": 4000},
    {"variant_id": "A", "start": 4000, "end": 8499, "weight": 4500},
    {"variant_id": "C", "start": 8500, "end": 9999, "weight": 1500}
  ],
  "stable_buckets": 4500,
  "migrated_buckets": 5500,
  "matrix": [
    {"from_variant": "A", "to_variant": "B", "buckets": 1000},
    {"from_variant": "B", "to_variant": "A", "buckets": 2000},
    {"from_variant": "B", "to_variant": "B", "buckets": 3000},
    {"from_variant": "C", "to_variant": "A", "buckets": 2500},
    {"from_variant": "C", "to_variant": "C", "buckets": 1500}
  ],
  "migrations": [
    {"from_variant": "A", "to_variant": "B", "start": 0, "end": 999, "buckets": 1000},
    {"from_variant": "B", "to_variant": "B", "start": 1000, "end": 3999, "buckets": 3000},
    {"from_variant": "B", "to_variant": "A", "start": 4000, "end": 5999, "buckets": 2000},
    {"from_variant": "C", "to_variant": "A", "start": 6000, "end": 8499, "buckets": 2500},
    {"from_variant": "C", "to_variant": "C", "start": 8500, "end": 9999, "buckets": 1500}
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `order` | 推荐的目标变体顺序（稳定桶最大、并列时 ID 序列字典序最小） |
| `segments` | 推荐布局的分段：每段为闭区间 `[start, end]`，按桶号升序**完整划分** `0～9999`——每个桶恰好归属一个变体 |
| `stable_buckets` / `migrated_buckets` | 分配不变 / 发生迁移的桶数，合计 10000 |
| `matrix` | `from_variant -> to_variant` 迁移矩阵（仅非零项，含对角线稳定项），按 `(from_variant, to_variant)` 升序 |
| `migrations` | 把相邻且迁移方向相同的桶合并后的区间（闭区间、按桶号升序，同样完整划分桶域） |

请求校验（不满足时返回 `422`）：

| 规则 | 说明 |
| --- | --- |
| 权重 | 两侧权重均为正整数（`1～10000`），且各自合计恰为 **10000** |
| 重复 ID | `current` / `target` 内变体 ID 均不允许重复 |
| 变体数 | `target` 为 1～8 个（枚举全排列）；`current` 至少 1 个 |
| 额外字段 | 请求与权重项均拒绝未声明字段 |

请求示例：

```bash
curl -s http://localhost:8000/rollouts/rebalance \
  -H 'Content-Type: application/json' \
  -d '{"current":[{"variant_id":"A","weight":1000},{"variant_id":"B","weight":5000},{"variant_id":"C","weight":4000}],"target":[{"variant_id":"A","weight":4500},{"variant_id":"B","weight":4000},{"variant_id":"C","weight":1500}]}'
```

### `POST /flags/retirement-plan` — 开关下线方案

为已关闭（`enabled=false`）的开关生成**可执行的下线方案**：关闭开关评估时
恒返回默认变体，因此其默认变体视为**终态**。

请求体：

```json
{
  "config": { "...": "完整 FlagConfig（同 /validate 的请求体）" },
  "switch_ids": ["old_checkout"],
  "client_references": [
    {"client_id": "web-app", "switch_id": "coupon_popup"}
  ]
}
```

- `switch_ids`：待下线候选（至少 1 个、不允许重复）；必须存在且
  `enabled=false`，否则 `422`；
- `client_references`：仍在使用各开关的活动客户端引用
  （`{client_id, switch_id}` 对不允许重复）；引用候选开关即构成 blocker。

判定流程：

1. 复用与 `/evaluate` 相同的配置校验，未通过 → `422`（携带全部校验错误）；
   候选不存在、候选仍 `enabled=true`、客户端引用指向未知开关 → `422`；
2. **外部依赖**：非候选开关依赖候选时，仅当依赖**显式期望终态**
   （`variant` 等于候选默认变体）才可自动删除该依赖边（改写）——候选删除后
   该依赖恒满足，移除不改变外部开关行为；其余情形（未指定 `variant` 或期望
   其它变体）移除依赖会改变行为，列为 blocker；
3. **活动客户端引用**候选开关同样列为 blocker；候选之间的依赖不构成
   blocker——依赖边随候选删除一并消失，由删除顺序保证合法性；
4. 存在任一 blocker → `status=blocked`，只返回全部 blockers，**不给出任何
   部分方案**（`rewrites` / `deletion_layers` 为空、`result_config` 为 null）；
5. 无 blocker → `status=ready`：先输出外部依赖改写 `rewrites`，再按
   **“依赖方先于被依赖方”** 对候选分层（同层 ID 升序）输出
   `deletion_layers`，并给出应用全部改写与删除后的完整 `result_config`。
   按“改写 → 逐层删除”的顺序应用，每一步的配置都能通过 `/validate`。

响应（`200`，无 blocker 时）：

```json
{
  "status": "ready",
  "blockers": [],
  "rewrites": [
    {"dependent_id": "coupon_popup", "removed_dependency": "old_checkout"}
  ],
  "deletion_layers": [["old_checkout"]],
  "result_config": { "...": "删除候选并改写依赖后的完整 FlagConfig" }
}
```

`status=blocked` 时 `blockers` 的条目：

| 字段 | 说明 |
| --- | --- |
| `kind` | `client_reference`（活动客户端引用）/ `external_dependency`（外部依赖未显式期望终态） |
| `switch_id` | 被阻断的候选开关 |
| `terminal_variant` | 候选的默认变体（终态） |
| `client_id` | 仅 `client_reference`：仍在引用候选的客户端 |
| `dependent_id` | 仅 `external_dependency`：依赖候选的外部开关 |
| `expected_variant` | 仅 `external_dependency`：依赖期望的变体（null 表示未指定，即期望任意非默认变体） |

输出排序：`blockers` 按 `(switch_id, kind, 引用方)` 升序；`rewrites` 按
`(dependent_id, removed_dependency)` 升序；同层候选按 ID 升序——相同配置下
候选与引用的书写顺序不影响响应。

请求校验（不满足时返回 `422`）：

| 规则 | 说明 |
| --- | --- |
| 候选 | 至少 1 个、不允许重复、必须存在且 `enabled=false` |
| 客户端引用 | `{client_id, switch_id}` 对不允许重复；`switch_id` 必须存在 |
| 配置 | 复用 `/validate` 校验，未通过 → `422` |
| 额外字段 | 请求与客户端引用对象均拒绝未声明字段 |

请求示例：

```bash
curl -s http://localhost:8000/flags/retirement-plan \
  -H 'Content-Type: application/json' \
  -d '{"config":{"version":1,"switches":[{"id":"old_checkout","enabled":false,"default_variant":"off","variants":[{"id":"on"},{"id":"off"}]},{"id":"coupon_popup","enabled":true,"default_variant":"popup_off","variants":[{"id":"popup_on"},{"id":"popup_off"}],"dependencies":[{"switch":"old_checkout","variant":"off"}],"rules":[{"id":"all","conditions":[],"allocations":[{"variant_id":"popup_off","weight":10000}]}]}]},"switch_ids":["old_checkout"],"client_references":[]}'
```

### `POST /sdk/reconcile` — SDK 决策日志核对

核对 SDK 节点上报的本地决策日志：给定配置快照史（各版配置的激活时刻）、
宽限与时钟偏差，把每条事件按**所报摘要的允许区间**分类，并复用评估器
重算决策与节点上报值比对。

请求体：

```json
{
  "snapshots": [
    {"config": { "...": "完整 FlagConfig（同 /validate 的请求体）" }, "activated_at": "2026-03-01T00:00:00Z"},
    {"config": { "...": "下一版 FlagConfig" }, "activated_at": "2026-03-01T01:00:00Z"}
  ],
  "grace_seconds": 300,
  "clock_skew_seconds": 60,
  "events": [
    {
      "event_id": "e1",
      "node_id": "node-a",
      "timestamp": "2026-03-01T00:30:00Z",
      "digest": "64 位十六进制 SHA-256",
      "switch_id": "new_checkout",
      "user_id": "user-10086",
      "context": {"tier": "gold"},
      "variant_id": "on",
      "served_default": false,
      "reason": null
    }
  ]
}
```

- 快照摘要**不在请求中给出**：服务端沿用制品的规范化 SHA-256
  （`SHA-256(规范化配置 JSON)`，与 `/artifacts/sign` 同一摘要）为每个快照
  重算，事件以 `digest` 指认自己使用的配置版本；
- 快照按 `activated_at` 升序排列后，第 i 版摘要的**允许区间**为
  `[activated_at[i], activated_at[i+1] + grace_seconds)`：新版自激活起生效
  （快照生效至下一版激活），旧版合法上报延续到“下一版激活 + 宽限”；
  最后一版的上界为 +∞；宽限重叠期内新旧两版均为合法上报；
- 每条事件的时间按 `clock_skew_seconds` 扩为 **±偏差闭区间** `[t−s, t+s]`
  后与允许区间比较（零偏差时退化为单个时刻，不会判出 `ambiguous_time`）：

| classification | 含义 |
| --- | --- |
| `consistent` | ±偏差区间完全落入所报摘要的允许区间 |
| `ambiguous_time` | 与允许区间相交但未完全落入（时钟偏差跨边界，无法确证） |
| `early_version` | 整体在允许区间之前（所报版本当时尚未激活） |
| `stale_version` | 整体在允许区间之后（所报版本已超宽限过期） |
| `unknown_digest` | 所报摘要不在任何快照中（不参与重算） |

- 可识别摘要的事件**复用评估器**在对应快照配置上重算
  `variant_id / served_default / reason`，任一不符即
  `decision_mismatch=true`（`unknown_digest` 时 `recomputed` 与
  `decision_mismatch` 均为 `null`）；
- 事件全局按 `event_id` 去重：同 ID 内容完全一致只计一次，内容冲突 → `422`。

响应（`200`）：`results` 按 `(node_id, timestamp, event_id)` 升序，
`nodes` 按 `node_id` 升序给出每节点的事件数与各分类计数
（五种分类合计等于 `events`，`decision_mismatches` 独立计数），
`event_count` 为去重后的事件总数：

```json
{
  "event_count": 1,
  "results": [
    {
      "event_id": "e1",
      "node_id": "node-a",
      "timestamp": "2026-03-01T00:30:00Z",
      "digest": "90fe...",
      "classification": "consistent",
      "decision_mismatch": false,
      "recomputed": {"variant_id": "on", "served_default": false, "reason": null}
    }
  ],
  "nodes": [
    {
      "node_id": "node-a",
      "events": 1,
      "consistent": 1,
      "ambiguous_time": 0,
      "early_version": 0,
      "stale_version": 0,
      "unknown_digest": 0,
      "decision_mismatches": 0
    }
  ]
}
```

请求校验（不满足时返回 `422`）：

| 规则 | 说明 |
| --- | --- |
| 快照 | 至少 1 份；`activated_at` 不允许重复；同一配置再次激活导致摘要重复（允许区间不唯一）拒绝 |
| 快照配置 | 每份配置复用 `/validate` 校验，未通过 → `422`（错误路径带 `snapshots[i]` 前缀） |
| 时长 | `grace_seconds ≥ 0`、`clock_skew_seconds ≥ 0` |
| 事件引用 | 可识别摘要的事件必须引用该快照配置中存在的开关（无法重算即拒绝） |
| `event_id` 冲突 | 同一 `event_id` 内容不一致（含节点、时刻、摘要、入参或结果不同） |
| 必填非空 | `event_id`、`node_id`、`digest`、`switch_id`、`user_id`、`variant_id` 不允许空串 |
| 额外字段 | 请求、快照与事件对象均拒绝未声明字段 |

完全一致的重复事件（含同一时刻的不同时区写法）会被静默去重，不报 `422`。

请求示例：

```bash
curl -s http://localhost:8000/sdk/reconcile \
  -H 'Content-Type: application/json' \
  -d "$(jq -c '{snapshots: [{config: ., activated_at: "2026-03-01T00:00:00Z"}], grace_seconds: 300, clock_skew_seconds: 60, events: []}' examples/config.json)"
```

### `POST /sdk/rollout-plan` — 配置制品分波铺开规划

把一份配置制品（以 `target_digest` 标识）安全铺开到若干 SDK 节点：规划
**分波更新顺序**，同一波内的节点同时更新、同时不可用，服务用节点子集
精确枚举求出**最少波次**，并在多解中取确定性的字典序最优方案。

请求体：

```json
{
  "target_digest": "sha256-of-new-artifact",
  "nodes": [
    {"id": "node-a", "current_digest": "sha256-old", "region": "cn-east", "failure_domain": "az-1", "traffic_weight": 30},
    {"id": "node-b", "current_digest": "sha256-old", "region": "cn-east", "failure_domain": "az-2", "traffic_weight": 30},
    {"id": "node-c", "current_digest": "sha256-of-new-artifact", "region": "cn-north", "failure_domain": "az-1", "traffic_weight": 40}
  ],
  "max_parallel": 2,
  "max_unavailable_weight": 50,
  "region_max_unavailable_weight": {"cn-east": 40, "cn-north": 60}
}
```

请求字段：

| 字段 | 说明 |
| --- | --- |
| `target_digest` | 目标制品摘要（非空字符串） |
| `nodes` | 1～**16** 个节点：`id`（唯一）、`current_digest`、`region`、`failure_domain`、`traffic_weight`（**正整数**流量权重） |
| `max_parallel` | 每波最多同时更新的节点数（**正整数**） |
| `max_unavailable_weight` | 每波允许的**全局**最大不可用流量权重（**正整数**） |
| `region_max_unavailable_weight` | 各 region 每波允许的最大不可用流量权重（映射非空，值为**正整数**）；节点的 region 必须在此声明 |

规划语义：

1. **只规划摘要不同的节点**：`current_digest == target_digest` 的节点无需
   更新，进入 `up_to_date_nodes`（仍计入各波的累计完成节点），不占任何波；
2. 同一波节点**同时不可用**，一波必须同时满足：
   - 节点数不超过 `max_parallel`；
   - 波内全部节点流量权重之和不超过全局 `max_unavailable_weight`（含端点）；
   - 波内每个 region 的权重和不超过该 region 的限额；
   - 同一 `failure_domain` **至多一个节点**（避免整个故障域同时失联）；
3. **最少波次为精确解**：枚举每个节点子集能否单独成波（剪枝 DFS），
   子集 DP 求覆盖全部待更新节点的最少波数；
4. **平局规则**：同为最少波次的多个方案中，把每波节点 ID 升序后，取波次
   序列（元组序列）**字典序最小**者——注意元组按位置比较，与子集的整数
   大小无关；结果与节点的提交顺序无关；
5. **不可行判定（blockers）**：任一节点**单独更新**（独占一波）时其权重
   仍超过全局或其 region 上限，则无法铺开：该节点分别给出
   `global_weight_exceeded` / `region_weight_exceeded` blocker（两者可同时
   命中），响应 `status=blocked` 且**不给出任何部分计划**（`waves` 为空）。
   并发数与故障域约束总能通过逐节点成波满足，不产生 blocker；
6. 节点上限 16：`2^16` 个候选子集保证精确枚举在常数时间内完成。

响应（`200`，`status=planned`）：

```json
{
  "target_digest": "sha256-of-new-artifact",
  "status": "planned",
  "up_to_date_nodes": ["node-c"],
  "planned_node_count": 2,
  "waves": [
    {
      "wave": 1,
      "node_ids": ["node-a"],
      "global_unavailable_weight": 30,
      "region_unavailable_weight": {"cn-east": 30},
      "completed_node_ids": ["node-a", "node-c"],
      "completed_node_count": 2
    },
    {
      "wave": 2,
      "node_ids": ["node-b"],
      "global_unavailable_weight": 30,
      "region_unavailable_weight": {"cn-east": 30},
      "completed_node_ids": ["node-a", "node-b", "node-c"],
      "completed_node_count": 3
    }
  ],
  "blockers": []
}
```

| 字段 | 说明 |
| --- | --- |
| `up_to_date_nodes` | 请求时摘要已等于目标、无需更新的节点（ID 升序） |
| `planned_node_count` | 实际参与分波规划的节点数（摘要不同者） |
| `waves[].wave` | 从 1 起的波次序号 |
| `waves[].node_ids` | 本波同时更新的节点（ID 升序） |
| `waves[].global_unavailable_weight` | 本波全部节点流量权重之和（即本波全局不可用权重） |
| `waves[].region_unavailable_weight` | 本波**实际涉及** region 的不可用权重和（按 region 名升序） |
| `waves[].completed_node_ids` / `completed_node_count` | 含本波及之前各波累计完成的全部节点（含 `up_to_date_nodes`） |

`status=blocked` 时 `blockers` 的条目：

| 字段 | 说明 |
| --- | --- |
| `kind` | `global_weight_exceeded`（单节点权重超全局上限）/ `region_weight_exceeded`（超其 region 上限） |
| `node_id` / `region` / `traffic_weight` | 阻断节点与其当前流量权重 |
| `limit` | 被突破的上限值 |

`blockers` 按 `(node_id, kind)` 升序（同节点先全局后分区）；此时
`up_to_date_nodes` 与 `planned_node_count` 仍照常给出，便于排查。

请求校验（不满足时返回 `422`）：

| 规则 | 说明 |
| --- | --- |
| 节点数 | 1～16 个（恰好 16 合法，17 拒绝） |
| 重复 ID | 节点 `id` 不允许重复 |
| 未知分区限额 | 节点的 `region` 必须在 `region_max_unavailable_weight` 中声明（声明了但未被节点使用的限额允许保留） |
| 权重 | `traffic_weight` 必须为正整数；`max_parallel`、全局与各 region 上限必须为正整数 |
| 必填非空 | `target_digest`、节点 `id` / `current_digest` / `region` / `failure_domain` 不允许空串 |
| 额外字段 | 请求与节点对象均拒绝未声明字段 |

请求示例：

```bash
curl -s http://localhost:8000/sdk/rollout-plan \
  -H 'Content-Type: application/json' \
  -d '{"target_digest":"new","max_parallel":2,"max_unavailable_weight":50,"region_max_unavailable_weight":{"r1":50},"nodes":[{"id":"a","current_digest":"old","region":"r1","failure_domain":"fd-a","traffic_weight":20},{"id":"b","current_digest":"old","region":"r1","failure_domain":"fd-b","traffic_weight":20}]}'
```

### `POST /sdk/rollout-audit` — SDK 分波铺开执行审计

在 [分波铺开规划](#post-sdkrollout-plan--配置制品分波铺开规划)产出
`status=planned` 的原计划后，审计 SDK 节点实际上报的执行事件流：事件按
`sequence` 序贯折叠重放，每波护栏彼此独立地复用
[序贯护栏（Wald SPRT）](#post-guardrailsevaluate--序贯护栏评估)累计判定，
**遇到首个违规即停止**。

请求体：

```json
{
  "plan": {
    "target_digest": "new",
    "status": "planned",
    "up_to_date_nodes": ["node-c"],
    "planned_node_count": 2,
    "waves": [
      {"wave": 1, "node_ids": ["node-a"], "global_unavailable_weight": 30, "region_unavailable_weight": {"cn-east": 30}, "completed_node_ids": ["node-a", "node-c"], "completed_node_count": 2},
      {"wave": 2, "node_ids": ["node-b"], "global_unavailable_weight": 30, "region_unavailable_weight": {"cn-east": 30}, "completed_node_ids": ["node-a", "node-b", "node-c"], "completed_node_count": 3}
    ],
    "blockers": []
  },
  "guardrails": [
    {"id": "error_rate", "baseline_rate": 0.01, "max_increase": 0.01, "alpha": 0.05, "beta": 0.1}
  ],
  "events": [
    {"event_id": "e1", "sequence": 1, "type": "wave_started", "wave": 1},
    {"event_id": "e2", "sequence": 2, "type": "node_completed", "wave": 1, "node_id": "node-a"},
    {"event_id": "e3", "sequence": 3, "type": "metric_batch", "wave": 1, "metric_id": "error_rate", "batch": 1, "failures": 0, "total": 100}
  ]
}
```

请求字段：

| 字段 | 说明 |
| --- | --- |
| `plan` | `/sdk/rollout-plan` 返回的**原计划**，必须 `status=planned`（`blocked` 拒绝，`422`） |
| `guardrails` | 1～8 个护栏指标声明，字段同 `/guardrails/evaluate` 的指标（`id` 唯一），**不含批次**；每波从空批次独立累计 |
| `events` | 执行事件，可乱序提交；每条带唯一 `event_id` 与 `sequence`（≥0） |

三种事件类型（类型无关字段必须缺省，否则 `422`）：

| `type` | 必填字段 | 含义 |
| --- | --- | --- |
| `wave_started` | `wave` | 启动指定波次 |
| `node_completed` | `wave`、`node_id` | 该波内一个节点完成更新 |
| `metric_batch` | `wave`、`metric_id`、`batch`、`failures`、`total` | 该波一个指标的增量观测批次；`batch` 为该波该指标 1 起始的批次号，`0 ≤ failures ≤ total` |

折叠与序贯护栏：

- 事件先按 `event_id` **去重**（同 ID 内容完全一致只计一次，内容冲突 →
  `422`），再按 `sequence` 升序重放（`sequence` 重复 → `422`）；
- `wave_started` 必须严格按波次顺序：**`promote` 后方可启动下一波**——
  前一波未启动（越波）或护栏仍为 `hold` 时启动下一波均为 `wave_skipped`；
  重复启动已启动波为 `wave_already_started`；
- 节点**只能在所属波启动后完成**（`node_before_wave_start`），同一节点重复
  完成为 `node_already_completed`；
- **每波全部节点完成后才可接收指标**：波未启动收指标为
  `metrics_before_wave_start`，仍有节点未完成收指标为
  `metrics_before_nodes_complete`；
- 每波每指标的 `batch` 必须从 1 起**连续递增**（重复/跳号为
  `metric_batch_gap`）；指标首次越过 SPRT 阈值即冻结（同
  `/guardrails/evaluate` 语义），冻结后再到批次为 `metric_frozen`；
- 各波护栏累计**彼此独立**：波总决策为该波全部指标的汇总（任一 rollback →
  `rollback`、全部 pass → `promote`、其余 → `hold`）；某波一旦判定
  `rollback`，其后**任何事件**（同波后续批次、节点事件或越波启动）均为
  `event_after_rollback`（`rollback` 后再执行均违规）；
- **遇到首个违规即停止**：违规事件不改变状态，响应给出该事件及处理前的
  **事前状态**（`state.replayed_event_count` 为已成功折叠的事件数）。

响应（`200`）——合法日志 `valid=true`，`state.status` 为
`running` / `completed` / `rollback_required`：

```json
{
  "valid": true,
  "violation": null,
  "state": {
    "status": "running",
    "current_wave": 1,
    "all_completed_node_ids": ["node-a", "node-c"],
    "replayed_event_count": 3,
    "waves": [
      {
        "wave": 1,
        "status": "awaiting_metrics",
        "started": true,
        "completed_node_ids": ["node-a"],
        "batch_count": 1,
        "decision": "hold",
        "metrics": [
          {
            "id": "error_rate",
            "decision": "hold",
            "stop_batch": null,
            "failures": 0,
            "total": 100,
            "p0": 0.01,
            "p1": 0.02,
            "lower_threshold": -2.25129,
            "upper_threshold": 2.89037,
            "llr": -1.01523,
            "path": [{"batch": 1, "failures": 0, "total": 100, "llr": -1.01523}]
          }
        ]
      },
      {"wave": 2, "status": "pending", "started": false, "completed_node_ids": [], "batch_count": 0, "decision": null, "metrics": []}
    ]
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `state.status` | `running`（尚未全部 promote）/ `completed`（全部波 promote 且节点完成）/ `rollback_required`（任一波护栏 rollback） |
| `state.current_wave` | 最近已启动的波次号；一波未启动时为 `null` |
| `state.all_completed_node_ids` | 各波已完成节点并集 + 原计划 `up_to_date_nodes`（ID 升序） |
| `state.replayed_event_count` | 成功折叠（去重后、违规前）的事件数 |
| `waves[].status` | `pending` / `running`（已启动未完成节点）/ `awaiting_metrics`（节点全完成、护栏未终判）/ `promoted` / `rollback_required` |
| `waves[].completed_node_ids` | **本波**已完成的节点（ID 升序） |
| `waves[].decision` | 本波护栏总决策 `promote` / `hold` / `rollback`；未收任何批次时为 `null` |
| `waves[].metrics[]` | 启动后各指标的独立累计结果，结构同 `/guardrails/evaluate` 的 `metrics[]`（`path` 为逐批 LLR 路径，越界后冻结）；`pending` 波为空 |

违规响应（仍为 `200`，便于审计工具读取）`valid=false`，`violation` 给出
`kind`、中文 `message` 与原始 `event`，`state` 为**事前状态**：

```json
{
  "valid": false,
  "violation": {
    "kind": "wave_skipped",
    "message": "第 1 波护栏尚未 promote（当前判定：hold），不允许启动第 2 波（event_id=e9）",
    "event": {"event_id": "e9", "sequence": 9, "type": "wave_started", "wave": 2}
  },
  "state": { "...": "处理 e9 之前的状态快照" }
}
```

`violation.kind` 取值：`wave_skipped`、`wave_already_started`、
`node_before_wave_start`、`node_already_completed`、
`metrics_before_wave_start`、`metrics_before_nodes_complete`、
`metric_batch_gap`、`metric_frozen`、`event_after_rollback`。

请求校验（不满足时返回 `422`）：

| 规则 | 说明 |
| --- | --- |
| 原计划 | 必须 `status=planned` 且 `blockers` 为空；波次号从 1 连续、节点不跨波重复、`planned_node_count` 与各波节点总数一致 |
| 护栏 | 1～8 个，`id` 唯一；比率与 α/β 约束同 `/guardrails/evaluate` |
| 事件引用 | `wave` 必须在计划范围内；`node_id` 必须属于该波（已达目标节点不在任何波）；`metric_id` 必须已声明 |
| 事件形状 | 三类事件只允许携带各自的必填字段；`failures ≤ total` |
| 折叠 | 同一 `event_id` 内容冲突（含 `sequence`、类型或任一字段不同）；去重后 `sequence` 重复 |
| 额外字段 | 请求与事件对象均拒绝未声明字段 |

请求示例：

```bash
curl -s http://localhost:8000/sdk/rollout-audit \
  -H 'Content-Type: application/json' \
  -d '{"plan":{"target_digest":"new","status":"planned","up_to_date_nodes":[],"planned_node_count":1,"waves":[{"wave":1,"node_ids":["a"],"global_unavailable_weight":1,"region_unavailable_weight":{"r1":1},"completed_node_ids":["a"],"completed_node_count":1}],"blockers":[]},"guardrails":[{"id":"m","baseline_rate":0.01,"max_increase":0.01,"alpha":0.05,"beta":0.1}],"events":[{"event_id":"s1","sequence":1,"type":"wave_started","wave":1},{"event_id":"n1","sequence":2,"type":"node_completed","wave":1,"node_id":"a"},{"event_id":"b1","sequence":3,"type":"metric_batch","wave":1,"metric_id":"m","batch":1,"failures":0,"total":100}]}'
```

### `GET /health`

```json
{ "status": "ok" }
```

---

## 4. 测试

```bash
pip install -r requirements.txt
pytest
```

覆盖内容：

- 校验：未知依赖、未知变体引用、权重总和、重复实体、自依赖/两节点/三节点环、菱形 DAG 合法；
- 深层依赖图（回归）：1500 层合法链的校验/评估/最小上下文（超过默认递归上限，
  修复前返回 500）、深链中间开关关闭即短路且失败逐层传播、context 透传到链尾、
  1500 节点环报完整环路径且只报一次、深环评估 422、750 层菱形 DAG（共享下游节点）
  合法、菱形评估的依赖声明顺序与首个不满足即短路；
- 评估：哈希确定性与桶边界、累计权重区间、零权重变体、权重分布比例、首条规则优先、
  全部运算符、无规则命中/开关关闭返回默认变体、盐值回退；
- 依赖：递归满足/不满足、变体精确匹配、链式依赖、被依赖开关关闭；
- 最小充分上下文：首条规则遮蔽属性的移除、遮蔽规则属性仍须声明域、
  依赖链属性的保留/移除（传递闭包覆盖）、`exists` 缺失态探测与不允许缺失、
  并列最小集合按属性数与字典序取先、`variant_id` 相同但 `reason` 不同即不充分、
  无引用属性空集合、规范化 JSON 键序判等、4096 边界与超限、
  未覆盖/多声明/重复域/空域/重复候选值/当前值越界/坏配置 422、未知开关 404；
- 序贯护栏：LLR 逐批累计公式、Wald 阈值、pass/rollback/hold 判定、越界后冻结、
  多指标总决策（最早回滚批次、全部 pass 才 promote）、概率越界/指标重复/
  批次缺项/failures>total 等 422 校验；
- 多人审批：事件折叠（覆盖/撤票/撤后再投）、否决角色一票 blocked 与否决证据、
  角色匹配计入、同一人最多计入一组、字典序最小分配（跳过被其他组需要的人、
  pending 时先最大化计入人数）、重复人员/组/sequence、乱序、未知引用、
  摘要不符、非法阈值等 422 校验；
- 实验归因：相同事件去重（含同时刻不同时区写法）与 event_id 内容冲突 422、
  最近曝光选择与同一时刻 ID 最大者、归因窗口闭区间边界、零窗口、
  `no_exposure / expired / contaminated / attributed` 四种状态及优先级、
  半开洗脱区间端点、多次变体切换、同变体重复曝光不算切换、去重曝光用户数、
  value 合计、乱序输入结果一致与固定输出顺序、负 value/时长/空 ID/额外字段 422；
- 制品：规范化 JSON（键序/数组序/空白）、签发确定性与 422/404、验签往返、
  篡改配置/元数据/摘要/签名、未知密钥、时间边界（闭区间端点与两侧）、
  多密钥（签名互不相同、交叉替换失败、密钥轮换）；
- 发布窗口：跨午夜展开（含正/负 UTC 偏移时区）、半开查询范围裁剪、
  春季跳时不存在边界顺延与坍塌丢弃、两端在 gap 外不记调整、
  秋季重复开始取较早/结束取较晚与重复小时加倍、重复边界开始较早/结束较晚、
  相邻出现合并与规则并集、黑窗中部切割/贴边裁剪/重叠合并/全覆盖、
  多日历交集与单方黑窗切割、min_duration 过滤与闭下限、结果排序、
  恰好 31 天合法、非法时区/重复日历与规则 ID/空周期窗口与空黑窗/
  naive 与非 UTC 查询及黑窗时间/超 31 天跨度/非法 HH:MM/额外字段 422；
- 权重重排：重排优于原顺序（稳定桶更多才换序）、并列按目标 ID 序列字典序
  择优、新增/删除变体、目标书写顺序不影响结果、分段与迁移区间完整划分
  桶域（每个桶恰好归属一个变体）、矩阵对角线即稳定桶、8 个目标变体合法、
  重复 ID/非正权重/权重和不为 10000/超 8 个目标/空列表/空 ID/额外字段 422；
- 开关下线方案：终态满足的依赖改写与改写前后行为一致、重复依赖边只改写
  一次、未指定/非终态期望依赖与活动客户端引用列为 blocker、有 blocker
  不出部分方案、候选间依赖级联分层（依赖方先于被依赖方、同层 ID 升序）、
  改写与 blocker 稳定排序、逐步重放每步通过 /validate 且终态即结果配置、
  未知/重复候选、候选未关闭、重复客户端引用、引用未知开关、坏配置与
  额外字段 422；
- 决策日志核对：±偏差区间与允许区间的完全落入/相交/在前/在后边界
  （激活起点含、宽限终点不含、零偏差不产生 ambiguous、零宽限即激活即过期）、
  宽限重叠区新旧两版均合法、多版切换（生效期一致、超宽限 stale、
  未激活 early、最后一版无上界）、快照乱序提交结果一致、
  重算三元组逐字段比对与 /evaluate 交叉一致、按所报版本配置比对、
  unknown_digest 跳过重算、相同事件去重（含同时刻不同时区写法）与
  event_id 内容冲突 422、结果按 (节点, 时间, 事件 ID) 排序与节点计数、
  快照 activated_at 重复/摘要重复/空快照/负时长/事件引用快照未知开关/
  坏快照配置/空 ID/额外字段 422；
- 制品分波铺开：全部已达目标时空规划（up_to_date 全量列出）、已达目标节点
  预置累计完成列表、单波铺满与 max_parallel 拆波（=1 逐节点）、全局权重
  上限迫使精确 2 波（朴素贪心填满会错成 3 波）、分区权重独立拆波、同故障
  域每波至多一个（仅故障域冲突不产生 blocker）、平局优先单节点首波、不同
  基数的首波元组按位置而非子集掩码比较、提交顺序不影响结果、每波 ID 升序
  且波次间字典序有序、单节点超全局/分区上限（可同时命中两条）、任一
  blocker 即不出部分计划、blocker 稳定排序、权重恰好等于上限合法、综合
  约束交互与逐波分区权重、16 节点上限精确求解；非正并发/全局/分区上限、
  非正权重、重复节点 ID、未知分区限额、空限额映射、空节点列表、17 节点、
  空串字段、额外/缺失字段 422，16 节点边界与未使用限额 200；
- 铺开执行审计：按 sequence 乱序折叠、event_id 同内容去重、0 波即
  completed、up_to_date 预置累计完成、各波护栏独立累计（批次号各自从 1
  起）与 promote 闸门、全成功三批逐波 promote 至 completed、一波 hold 阻塞
  下一波、多指标全 pass 才 promote、单指标冻结后再送批次、失败批 rollback
  后任何事件（含越波启动）均违规、节点先于波启动/重复完成、指标早于全部
  节点完成、批次跳号、重复启动、越波启动、首个违规即停且返回事前状态；
  非 planned 计划、未知波次/节点/指标引用、节点串波、sequence 重复、
  event_id 内容冲突、护栏 ID 重复/参数非法、波次号不连续、节点跨波重复、
  计划计数不一致、事件形状错配（failures>total/多余字段）422；
- API：健康检查、404、坏配置/环/未知依赖返回 422、同用户结果稳定、不同盐值产生分流、
  发布窗口求解往返与非 UTC 拒绝。

## 5. 项目结构

```
app/
  models.py          # Pydantic 模型（开关/变体/依赖/规则/制品/护栏/审批/归因/发布窗口/最小上下文/权重重排/下线方案/分波铺开/日志核对/请求响应）
  validation.py      # 依赖图、引用、权重、环检测（迭代 DFS 三色标记，显式栈）
  evaluator.py       # 迭代依赖评估（显式帧栈 + 轨迹段树）、条件求值、SHA-256 分桶、权重选变体
  explanations.py    # 最小充分上下文：依赖闭包属性收集（迭代）、保留集合（属性数/字典序）枚举、域值与缺失态重评
  guardrails.py      # Wald SPRT：逐批累计 LLR、阈值判定、冻结、总决策汇总
  artifacts.py       # 规范化 JSON、SHA-256 摘要、HMAC-SHA256 签发/验签、密钥加载
  approvals.py       # 多人审批：事件折叠、否决判定、最大流分组配额、字典序最小分配
  attribution.py     # 实验归因：event_id 去重、最近曝光匹配、归因窗口、变体切换半开洗脱
  release_windows.py # 发布窗口：周期窗口逐日展开（跨午夜/DST gap 顺延/fold 早晚）、合并、扣黑窗、交集裁剪
  rebalance.py       # 权重重排：枚举目标顺序求全稳定桶最大布局（字典序破平局）、迁移矩阵与同向相邻区间合并
  retirement.py      # 开关下线方案：blocker 收集、终态依赖改写、候选分层删除（依赖方先于被依赖方）
  rollout.py         # 制品分波铺开：子集可行性剪枝枚举、最少波次 DP（下界短路）、字典序最小重建
  rollout_audit.py   # 铺开执行审计：事件按 sequence 折叠重放、逐波独立复用 Wald SPRT、违规即停
  reconcile.py       # 决策日志核对：快照摘要允许区间、±偏差时间分类、评估器重算比对、节点计数
  main.py            # FastAPI 路由 /validate /evaluate /explanations/minimize /guardrails/evaluate /approvals/evaluate /experiments/attribute /artifacts/sign /artifacts/verify /release-windows/resolve /rollouts/rebalance /flags/retirement-plan /sdk/rollout-plan /sdk/rollout-audit /sdk/reconcile /health
tests/               # pytest 套件
examples/config.json
Dockerfile
docker-compose.yml
requirements.txt
```
