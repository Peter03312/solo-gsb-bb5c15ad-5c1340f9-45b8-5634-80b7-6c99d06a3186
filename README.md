# 🌍 跨时区软件发布窗口编排

在多时区环境下编排软件发布：给定**服务依赖、候选发布时间窗、团队禁发时段、维护冻结、回滚耗时与共享资源**，
系统用 Google OR-Tools CP-SAT 求出满足全部约束的可执行排程；无解时返回**阻塞它的约束组合**而不是部分结果。

## 约束语义

| 约束 | 规则 |
|---|---|
| 候选时间窗 | 每个服务的**部署 + 回滚整个占用区间**必须完整落在其某个候选窗内；支持多窗 |
| 依赖先后 | `下游开始 ≥ 上游部署结束 + gap_minutes` |
| 团队禁发 | 仅**部署区间**不得与所属团队禁发时段重叠（回滚允许进入禁发期） |
| 维护冻结 | **占用区间（部署+回滚）**不得与任何冻结重叠 |
| 共享资源 | 同名资源在任意时刻的**占用数（含回滚窗口）**不得超过其**当时容量**。基准容量由请求顶层的 `resource_capacities` 声明（正整数）；**未声明的资源容量为 1**，即同一时间只能被一个服务占用。容量为 2 时两个服务可并行，第三个必须错峰 |
| 临时限流 | `resource_capacity_windows` 可按资源给出若干**容量时段**（开始、结束、时区、非负整数容量），区间左闭右开：时段内覆盖为时段容量，**时段外沿用 `resource_capacities`**（未声明仍为 1）；**容量 0 表示该时段完全不可占用**。降容时刻落在既有占用期间（含回滚窗口）也必须遵守，排程会自动把占用错峰到合法时段。同资源时段重叠、结束不晚于开始、容量非法、本地时间存在 DST 歧义均按 422 拒绝 |
| 夏令时 | 不带偏移量的本地墙上时间若在 DST 切换时**重复**（回拨）或**不存在**（前拨），请求被拒绝（422），并给出 gap 两侧 / 重复两次的绝对时间供修正；带偏移量的绝对时间（如 `...+01:00`、`...Z`）无歧义，直接通过 |
| 无解 | 求解器返回一个足以导致矛盾的约束子集（sufficient assumption core），即“阻塞组合”，放松其中任意一项后可重新求解；**不返回部分排程** |

目标函数：所有服务部署开始时刻之和最小（排程尽量早、结果确定可复现）。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- 操作页面：http://localhost:8000/
- 健康检查：http://localhost:8000/api/health
- 交互式 API 文档（Swagger）：http://localhost:8000/docs

停止：`docker compose down`。

## 仅用 Docker

```bash
docker build -t release-orchestrator:1.0 .
docker run --rm -p 8000:8000 release-orchestrator:1.0
```

## 本地直接运行（Python 3.11+）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

> 时区数据：镜像内同时安装了系统 `tzdata` 与 Python `tzdata` 包，保证 `zoneinfo` 可解析全部 IANA 时区
> （`Asia/Shanghai`、`Europe/Berlin`、`America/New_York` 等）。

## HTTP API

### `POST /api/schedule`

请求体：

```json
{
  "services": [
    {
      "id": "payments",
      "name": "支付服务",
      "timezone": "Europe/Berlin",
      "duration_minutes": 45,
      "rollback_minutes": 60,
      "resources": ["payment-db", "k8s-prod-east"],
      "windows": [
        {"start": "2026-11-13T22:00", "end": "2026-11-14T05:00", "timezone": "Europe/Berlin"}
      ]
    }
  ],
  "dependencies": [
    {"upstream": "payments", "downstream": "api-gateway", "gap_minutes": 30}
  ],
  "service_teams": [
    {"service": "payments", "team": "payment-team"}
  ],
  "team_blackouts": [
    {"team": "payment-team", "start": "2026-11-13T23:30", "end": "2026-11-14T00:30", "timezone": "Europe/Berlin"}
  ],
  "freezes": [
    {"label": "周末封网", "start": "2026-11-14T02:00", "end": "2026-11-14T03:00", "timezone": "UTC"}
  ],
  "resource_capacities": {
    "payment-db": 1,
    "k8s-prod-east": 2
  },
  "resource_capacity_windows": [
    {"resource": "k8s-prod-east",
     "start": "2026-11-13T23:00", "end": "2026-11-14T01:00",
     "timezone": "Europe/Berlin", "capacity": 1}
  ],
  "max_solve_seconds": 10
}
```

`resource_capacities` 为可选字段：

- 键是资源名，值是该资源的**并行容量（正整数）**。服务 `resources` 中每列出一个资源，
  就在自己的部署 + 回滚占用区间内占用该资源 **1 个名额**；任意时刻同资源占用数不得超过容量。
  容量为 2 时两项发布可并行，第三项必须错峰。
- **未在 `resource_capacities` 中出现的资源按容量 1 处理**；省略整个字段或传 `{}` 时，
  行为与旧版本完全一致（所有共享资源串行）。
- 容量必须为正整数（`0`、负数按 422 拒绝，错误码 `bad_resource_capacity`）；
  同一服务在 `resources` 中重复列出同一资源也按 422 拒绝（`duplicate_resource`）。
- 容量不满足时（如三个服务只能挤在容量 2 的同一时段）仍返回 **200、`feasible=false`**，
  `blockers` 中给出含资源容量约束的阻塞组合，不返回部分排程。

**临时容量时段 `resource_capacity_windows`（可选）**：发布前对共享资源做临时限流时使用，
每个时段形如 `{"resource", "start", "end", "timezone", "capacity"}`：

- 时间写法与候选窗一致：墙上时间 + `timezone`（参与 DST 检查）或带偏移量的绝对时间；
  区间一律按**左闭右开** `[start, end)` 处理。
- 时段内资源容量**覆盖**为该时段的 `capacity`（**非负整数**；`0` = 该时段完全不可占用）；
  时段外（含时段开始之前与结束之后）沿用 `resource_capacities`，未声明基准容量仍为 `1`。
- 占用计数覆盖每个服务的**部署 + 回滚整个占用区间**：降容时刻正好落在某次发布的回滚窗口内时，
  该占用同样计入降容后容量，求解器会把开始时刻整体错峰到合法位置，不会出现“先抢到就放行”。
- 同一资源的多个时段**不得重叠**（首尾相接允许，例如 `00:00–01:00` 与 `01:00–02:00`）；
  不同资源的时段互不影响。引用了任何服务都不使用的资源也是合法的（不产生约束）。
- 以下情况按 **422** 拒绝，沿用现有错误格式：时段重叠（`overlapping_capacity_windows`）、
  结束不晚于开始（`bad_interval`）、容量为负（`bad_resource_capacity`）、
  未知时区（`bad_timezone`）、墙上时间在 DST 前拨/回拨时不存在或重复
  （`dst_nonexistent` / `dst_ambiguous`，错误中附 gap 两侧/重复两次的绝对时间）。
- 临时容量导致排不下时，仍返回 **200、`feasible=false`**，`blockers` 中给出形如
  `{"code": "resource:<资源名>", ...}` 的资源阻塞组合（消息中列出基准容量与各临时时段），
  不返回部分排程。
- 省略该字段或传 `[]` 时行为与旧版本完全一致。

时间串支持两种写法：

- **墙上时间 + 时区字段**：`"2026-11-13T22:00"` + `"timezone": "Europe/Berlin"`，按该时区解释，参与 DST 检查；
- **绝对时间**：`"2026-11-13T21:00:00Z"` 或 `"2026-11-13T22:00:00+01:00"`，无 DST 歧义。

**可行（200）**：

```json
{
  "feasible": true,
  "status": "optimal",
  "schedule": [
    {
      "service_id": "payments",
      "name": "支付服务",
      "deploy_start_utc": "2026-11-13T21:00:00+00:00",
      "deploy_end_utc": "2026-11-13T21:45:00+00:00",
      "occupied_until_utc": "2026-11-13T22:45:00+00:00",
      "local_time": "2026-11-13T22:00:00+01:00",
      "timezone": "Europe/Berlin",
      "window_used": "2026-11-13T21:00:00+00:00 ~ 2026-11-14T04:00:00+00:00 UTC（候选窗1）",
      "resources": ["payment-db"]
    }
  ],
  "blockers": null
}
```

**不可行（200，`feasible=false`）**：`schedule` 为 `null`，`blockers` 为联合导致无解的约束组合：

```json
{
  "feasible": false,
  "status": "infeasible",
  "schedule": null,
  "blockers": [
    {"code": "freeze:周末封网:a", "message": "周末封网（…）期间服务 A 不得部署或处于回滚窗口内"},
    {"code": "dependency:a->b", "message": "依赖顺序：B 必须在 A 部署完成之后才能开始"},
    {"code": "resource:shared-db", "message": "共享资源 shared-db 同时只能被一个服务占用…"}
  ]
}
```

**输入错误（422）**：DST 重复/不存在、未知时区、依赖环、区间非法、引用不存在的服务、候选窗短于部署+回滚占用、资源容量非正（`bad_resource_capacity`）、临时容量为负（`bad_resource_capacity`）、同一资源的临时容量时段重叠（`overlapping_capacity_windows`）、同一服务重复列出资源（`duplicate_resource`）等：

```json
{
  "errors": [
    {
      "code": "dst_nonexistent",
      "field": "services[x].windows[0].start",
      "message": "…本地时间在夏令时前拨时不存在（该时刻被跳过）…",
      "suggestions": [
        "2027-03-14T06:30:00+00:00  （gap 前，偏移 UTC-05:00）",
        "2027-03-14T07:30:00+00:00  （gap 后，偏移 UTC-04:00）"
      ]
    }
  ]
}
```

`dst_ambiguous`（重复时间）的建议分别给出回拨前后的两个绝对时刻（第一次/第二次）。
这些值可直接粘贴回输入框重新提交。

#### 共享资源并行容量示例（HTTP）

三个服务都要占用 `canary-pool`，声明容量 2：前两项并行，第三项自动错峰一小时。

```bash
curl -X POST http://localhost:8000/api/schedule \
  -H 'Content-Type: application/json' \
  -d '{
    "services": [
      {"id":"canary-a","name":"灰度 A","timezone":"UTC",
       "duration_minutes":30,"rollback_minutes":30,"resources":["canary-pool"],
       "windows":[{"start":"2026-12-01T00:00:00Z","end":"2026-12-01T03:00:00Z","timezone":"UTC"}]},
      {"id":"canary-b","name":"灰度 B","timezone":"UTC",
       "duration_minutes":30,"rollback_minutes":30,"resources":["canary-pool"],
       "windows":[{"start":"2026-12-01T00:00:00Z","end":"2026-12-01T03:00:00Z","timezone":"UTC"}]},
      {"id":"canary-c","name":"灰度 C","timezone":"UTC",
       "duration_minutes":30,"rollback_minutes":30,"resources":["canary-pool"],
       "windows":[{"start":"2026-12-01T00:00:00Z","end":"2026-12-01T03:00:00Z","timezone":"UTC"}]}
    ],
    "resource_capacities": {"canary-pool": 2}
  }'
```

返回（200，节选）：`canary-a` 与 `canary-b` 同在 `00:00` 开始、占用至 `01:00`，
`canary-c` 错峰到 `01:00` 开始、占用至 `02:00` —— 任意时刻池内占用数均为 2，未超容量。
把容量改为 1（或直接省略该字段）时三者串行到 `00:00 / 01:00 / 02:00` 开始；
窗口只够容纳两个占用时则返回 `feasible=false`，`blockers` 含
`{"code": "resource:canary-pool", ...}`。容量写成 `0` 或负数返回 422：

```json
{
  "errors": [
    {
      "code": "bad_resource_capacity",
      "field": "resource_capacities[canary-pool]",
      "message": "资源 canary-pool 的容量必须为正整数（实际: 0）；未声明的资源按容量 1 处理",
      "suggestions": null
    }
  ]
}
```

#### 临时限流（容量时段）示例（HTTP）

`canary-pool` 基准容量 3，但发布当晚 `01:00–02:00 UTC` 临时降容到 1（例如值班缩编）：
三个服务的部署+回滚占用均为 1 小时，求解器让一个服务 `00:00` 开始（`01:00` 恰好释放，
左闭右开不与降容时段相交），另两个错峰到 `02:00` 降容结束后并行：

```bash
curl -X POST http://localhost:8000/api/schedule \
  -H 'Content-Type: application/json' \
  -d '{
    "services": [
      {"id":"canary-a","name":"灰度 A","timezone":"UTC",
       "duration_minutes":30,"rollback_minutes":30,"resources":["canary-pool"],
       "windows":[{"start":"2026-12-01T00:00:00Z","end":"2026-12-01T04:00:00Z","timezone":"UTC"}]},
      {"id":"canary-b","name":"灰度 B","timezone":"UTC",
       "duration_minutes":30,"rollback_minutes":30,"resources":["canary-pool"],
       "windows":[{"start":"2026-12-01T00:00:00Z","end":"2026-12-01T04:00:00Z","timezone":"UTC"}]},
      {"id":"canary-c","name":"灰度 C","timezone":"UTC",
       "duration_minutes":30,"rollback_minutes":30,"resources":["canary-pool"],
       "windows":[{"start":"2026-12-01T00:00:00Z","end":"2026-12-01T04:00:00Z","timezone":"UTC"}]}
    ],
    "resource_capacities": {"canary-pool": 3},
    "resource_capacity_windows": [
      {"resource":"canary-pool",
       "start":"2026-12-01T01:00:00Z","end":"2026-12-01T02:00:00Z",
       "timezone":"UTC","capacity":1}
    ]
  }'
```

返回（200，节选）：开始时刻为 `00:00 / 02:00 / 02:00` —— 降容时段
`[01:00, 02:00)` 内至多一个占用，时段外恢复 3 个并行名额。

**容量 0 = 时段封禁**。下例把 `01:00–02:00 UTC` 容量声明为 0，1 小时的占用区间
（部署 30 分钟 + 回滚 30 分钟）无法完整避开封禁段，全部错峰到 `02:00` 之后：

```bash
curl -X POST http://localhost:8000/api/schedule \
  -H 'Content-Type: application/json' \
  -d '{
    "services": [
      {"id":"job-a","name":"批处理 A","timezone":"UTC",
       "duration_minutes":30,"rollback_minutes":30,"resources":["batch-cluster"],
       "windows":[{"start":"2026-12-01T00:00:00Z","end":"2026-12-01T04:00:00Z","timezone":"UTC"}]},
      {"id":"job-b","name":"批处理 B","timezone":"UTC",
       "duration_minutes":30,"rollback_minutes":30,"resources":["batch-cluster"],
       "windows":[{"start":"2026-12-01T00:00:00Z","end":"2026-12-01T04:00:00Z","timezone":"UTC"}]}
    ],
    "resource_capacity_windows": [
      {"resource":"batch-cluster",
       "start":"2026-12-01T01:00:00Z","end":"2026-12-01T02:00:00Z",
       "timezone":"UTC","capacity":0}
    ]
  }'
```

时段也可用**墙上时间 + 时区**声明（与候选窗相同的 DST 规则），例如
`"start":"2026-12-01T02:00","end":"2026-12-01T03:00","timezone":"Europe/Berlin"`
等价于 `01:00–02:00 UTC`。若封禁段把所有候选位置占满（例如候选窗本身只到 `01:30`），
返回 **200、`feasible=false`** 且 `schedule` 为 `null`：

```json
{
  "feasible": false,
  "status": "infeasible",
  "schedule": null,
  "blockers": [
    {"code": "resource:batch-cluster",
     "message": "共享资源 batch-cluster 的容量随时段变化（基准 1，未声明时段按基准）："
                "2026-12-01T01:00:00+00:00 ~ 2026-12-01T02:00:00+00:00 UTC 容量 0"
                "（该时段不可占用）；部署+回滚窗口内任意时刻的占用数不得超过当时容量…"}
  ]
}
```

非法输入（时段重叠、结束不晚于开始、负容量、坏时区、DST 歧义）返回 422，例如重叠：

```json
{
  "errors": [
    {
      "code": "overlapping_capacity_windows",
      "field": "resource_capacity_windows",
      "message": "资源 canary-pool 的临时容量时段重叠：[2026-12-01T01:00:00+00:00 ~ 2026-12-01T02:00:00+00:00 UTC, 容量 1] 与 [2026-12-01T01:30:00+00:00 ~ 2026-12-01T02:30:00+00:00 UTC, 容量 0]；同一资源的容量时段不得重叠（首尾相接允许）",
      "suggestions": null
    }
  ]
}
```

### `POST /api/schedule/diff` — 变更影响预演

变更评审时，提交一份**当前排程请求**和一份**修改后的请求**，系统分别独立求解，
再按**服务 ID** 对齐两侧结果，预演变更会让哪些服务的发布时刻发生移动。

请求体：

```json
{
  "current": { ...一份完整的 /api/schedule 请求体... },
  "modified": { ...一份完整的 /api/schedule 请求体... }
}
```

比较规则：

- `changed`：两侧都存在、且**部署开始**或**占用结束**（部署结束 + 回滚窗口）的 UTC 时刻发生变化的服务，
  给出变化前后的 UTC 时刻与变化分钟数（`delta_minutes` 正为推迟、负为提前；未变化的字段为 `null`）；
- `added` / `removed`：仅在 `modified` 中出现（新增）/ 仅在 `current` 中出现（被移除）的服务；
- 两份请求结果完全一致时，三个列表均为空（空差异）。
- 两侧请求**各自携带自己的 `resource_capacities` 与 `resource_capacity_windows` 并独立求解**：
  例如 current 未声明容量（资源串行）、modified 把同一资源声明为容量 2（两项并行），
  或 modified 新增一个容量 0 的临时封禁时段，差异会体现在被提前/推迟的服务上；
  某一侧容量非法（422）、临时时段非法或容量导致无解时，同样只在 `failures` 的该侧给出
  完整错误 / 阻塞组合（含资源约束），`diff` 为 `null`，不给部分排程。
- **任一请求输入错误或无解时，不输出差异结果**：在 `failures` 中按 `current` / `modified`
  分别给出该侧的完整输入错误（HTTP 422）或阻塞约束组合（HTTP 200、`feasible=false`），
  与直接调用 `/api/schedule` 得到的内容一致。

调用示例：

```bash
curl -X POST http://localhost:8000/api/schedule/diff \
  -H 'Content-Type: application/json' \
  -d '{
    "current": {
      "services": [
        {"id": "payments", "name": "支付服务", "timezone": "UTC",
         "duration_minutes": 60, "rollback_minutes": 30,
         "windows": [{"start": "2026-11-13T00:00:00Z", "end": "2026-11-13T06:00:00Z", "timezone": "UTC"}]}
      ],
      "max_solve_seconds": 10
    },
    "modified": {
      "services": [
        {"id": "payments", "name": "支付服务", "timezone": "UTC",
         "duration_minutes": 60, "rollback_minutes": 30,
         "windows": [{"start": "2026-11-13T00:00:00Z", "end": "2026-11-13T06:00:00Z", "timezone": "UTC"}]}
      ],
      "freezes": [
        {"label": "临时冻结", "start": "2026-11-13T00:00:00Z", "end": "2026-11-13T01:00:00Z", "timezone": "UTC"}
      ],
      "max_solve_seconds": 10
    }
  }'
```

**有变化（200）**：

```json
{
  "feasible": true,
  "diff": {
    "changed": [
      {
        "service_id": "payments",
        "name": "支付服务",
        "deploy_start": {
          "before_utc": "2026-11-13T00:00:00+00:00",
          "after_utc": "2026-11-13T01:00:00+00:00",
          "delta_minutes": 60.0
        },
        "occupied_until": {
          "before_utc": "2026-11-13T01:30:00+00:00",
          "after_utc": "2026-11-13T02:30:00+00:00",
          "delta_minutes": 60.0
        }
      }
    ],
    "added": [],
    "removed": []
  },
  "failures": null
}
```

只调整回滚时长、开始时刻不变时，`deploy_start` 为 `null`，仅 `occupied_until` 给出变化。
两份请求无差异时返回 `"changed": [], "added": [], "removed": []`。

**某一侧输入错误（422）**：不输出 `diff`，在 `failures` 中指明出错的一侧及完整错误列表：

```json
{
  "feasible": false,
  "diff": null,
  "failures": {
    "modified": {
      "kind": "validation",
      "status": null,
      "errors": [
        {"code": "bad_timezone", "field": "services[payments].timezone",
         "message": "服务 payments: 未知时区 'Mars/Olympus'", "suggestions": null}
      ],
      "blockers": null
    }
  }
}
```

**某一侧无解（200，`feasible=false`）**：`kind` 为 `infeasible`（求解器超时未判定时为 `unknown`），
`blockers` 为该侧的阻塞约束组合：

```json
{
  "feasible": false,
  "diff": null,
  "failures": {
    "current": {
      "kind": "infeasible",
      "status": "infeasible",
      "errors": null,
      "blockers": [
        {"code": "freeze:维护冻结：全天封网:payments", "message": "…期间服务 支付服务（payments）不得部署或处于回滚窗口内"}
      ]
    }
  }
}
```

两侧同时失败时 `failures` 中会同时包含 `current` 与 `modified` 两个键。

## 项目结构

```
app/
  main.py        FastAPI 入口（/api/schedule、/api/schedule/diff、/api/health、静态页面）
  models.py      请求/响应 Pydantic 模型
  times.py       时区解析与 DST 重复/不存在分类（zoneinfo fold 规则）
  validator.py   输入校验：DST、引用完整性、依赖环检测、区间合法性、容量时段重叠检测
  scheduler.py   CP-SAT 建模、求解、不可行假设核（blocking core）提取
  diff.py        变更影响预演：双请求执行与按服务 ID 的时刻对齐求差
  web/index.html 操作页面（原生 JS，内置 3 个示例场景）
Dockerfile
docker-compose.yml
requirements.txt
```

## 不可行核的工作原理

每条业务约束都挂在一个布尔“强制字面量”上（通过可选区间实现：字面量为假时区间在
`NoOverlap` 中缺席），求解时把全部字面量作为 assumptions 断言为真。
CP-SAT 在判定 INFEASIBLE 时返回 `SufficientAssumptionsForInfeasibility`：
其中任意一个字面量放松（对应约束被修改/移除），其余约束才有可能成立。
因此返回的不是“所有相关约束”，而是一个最小化的、可直接据此谈判的阻塞组合。
