"""输入校验：DST 检查、引用完整性、DAG 环检测、时间区间合法性。

产出内部规整结构（全部时间为 UTC epoch 秒）供调度器使用。
任何校验失败都会收集全部错误后一次性返回（HTTP 422）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .models import ScheduleRequest
from .times import (
    DstIssue,
    UnknownTimezoneError,
    get_zone,
    parse_wall_time,
)


@dataclass
class Window:
    start: int
    end: int
    label: str


@dataclass
class Period:
    start: int
    end: int
    label: str


@dataclass
class CapacityWindow:
    start: int  # UTC epoch 秒
    end: int
    capacity: int  # 非负整数；0 表示该时段不可占用


@dataclass
class Service:
    id: str
    name: str
    timezone: str
    duration: int  # 秒
    rollback: int  # 秒
    resources: list[str]
    windows: list[Window]


@dataclass
class Dependency:
    upstream: str
    downstream: str
    gap: int  # 秒


@dataclass
class Normalized:
    services: dict[str, Service]
    dependencies: list[Dependency]
    team_members: dict[str, list[str]]  # service_id -> teams
    blackouts: dict[str, list[Period]]  # team -> periods
    freezes: list[Period]
    resource_capacities: dict[str, int]  # 声明容量的资源 -> 正整数容量（未声明者按 1 处理）
    resource_capacity_windows: dict[str, list[CapacityWindow]]  # 资源 -> 临时容量时段（已按时开始排序）
    max_solve_seconds: float


def _issue_error(issue: DstIssue) -> dict:
    if issue.kind == "ambiguous":
        msg = (
            f"{issue.field}: 本地时间 {issue.raw!r}（时区 {issue.tz}）在夏令时回拨时重复发生，"
            "无法确定是哪一次。请改用带偏移量的绝对时间二选一："
        )
    else:
        msg = (
            f"{issue.field}: 本地时间 {issue.raw!r}（时区 {issue.tz}）在夏令时前拨时不存在"
            "（该时刻被跳过）。请改用 gap 两侧的合法时间："
        )
    return {
        "code": f"dst_{issue.kind}",
        "field": issue.field,
        "message": msg,
        "suggestions": issue.suggestions,
    }


def validate_and_normalize(req: ScheduleRequest) -> tuple[Normalized | None, list[dict]]:
    errors: list[dict] = []
    dst_issues: list[dict] = []

    # 1) 时区字段本身是否合法（服务展示时区）。
    service_tz: dict[str, str] = {}
    for s in req.services:
        try:
            get_zone(s.timezone)
            service_tz[s.id] = s.timezone
        except UnknownTimezoneError:
            errors.append({"code": "bad_timezone", "field": f"services[{s.id}].timezone",
                           "message": f"服务 {s.id}: 未知时区 {s.timezone!r}"})

    def parse_period(raw_start: str, raw_end: str, tzname: str, field: str) -> tuple[int, int] | None:
        try:
            get_zone(tzname)
        except UnknownTimezoneError:
            errors.append({"code": "bad_timezone", "field": f"{field}.timezone",
                           "message": f"{field}: 未知时区 {tzname!r}"})
            return None
        st, is1 = parse_wall_time(raw_start, tzname, f"{field}.start")
        en, is2 = parse_wall_time(raw_end, tzname, f"{field}.end")
        for iss in (is1, is2):
            if iss:
                dst_issues.append(_issue_error(iss))
        a, b = int(st.timestamp()), int(en.timestamp())
        if a >= b:
            errors.append({"code": "bad_interval", "field": field,
                           "message": f"{field}: 开始必须早于结束（{raw_start} >= {raw_end}）"})
            return None
        return a, b

    # 2) 服务与候选窗。
    seen: set[str] = set()
    services: dict[str, Service] = {}
    for idx, s in enumerate(req.services):
        if not s.id:
            errors.append({"code": "empty_id", "field": f"services[{idx}].id",
                           "message": "服务 id 不能为空"})
            continue
        if s.id in seen:
            errors.append({"code": "duplicate_service", "field": f"services[{s.id}]",
                           "message": f"服务 id 重复: {s.id}"})
            continue
        seen.add(s.id)

        # 同一服务重复列出同一资源没有“占多个名额”的语义，按输入错误拒绝。
        seen_resources: set[str] = set()
        for r in s.resources:
            if r in seen_resources:
                errors.append({"code": "duplicate_resource",
                               "field": f"services[{s.id}].resources",
                               "message": f"服务 {s.id} 重复列出资源: {r}（同一资源在一个服务中只能出现一次）"})
            seen_resources.add(r)

        windows: list[Window] = []
        for j, w in enumerate(s.windows):
            field = f"services[{s.id}].windows[{j}]"
            parsed = parse_period(w.start, w.end, w.timezone, field)
            if parsed:
                windows.append(Window(parsed[0], parsed[1], f"候选窗{j + 1}"))
        win_span = s.duration_minutes * 60 + s.rollback_minutes * 60
        for w in windows:
            if w.end - w.start < win_span:
                errors.append({
                    "code": "window_too_short",
                    "field": f"services[{s.id}].windows",
                    "message": (
                        f"服务 {s.id} 的候选窗 [{w.label}] 长度 {w.end - w.start}s 小于"
                        f" 部署+回滚占用 {win_span}s，单独使用该窗必然不可行"
                    ),
                })
        services[s.id] = Service(
            id=s.id,
            name=s.name or s.id,
            timezone=service_tz.get(s.id, "UTC"),
            duration=s.duration_minutes * 60,
            rollback=s.rollback_minutes * 60,
            resources=list(dict.fromkeys(s.resources)),
            windows=windows,
        )

    # 3) 资源容量：必须为正整数（未声明的资源仍按容量 1 处理）。
    resource_capacities: dict[str, int] = {}
    for r, cap in req.resource_capacities.items():
        if cap is None or cap <= 0:
            errors.append({"code": "bad_resource_capacity",
                           "field": f"resource_capacities[{r}]",
                           "message": f"资源 {r} 的容量必须为正整数（实际: {cap}）；"
                                      "未声明的资源按容量 1 处理"})
            continue
        resource_capacities[r] = cap

    # 3b) 临时容量时段：逐段解析（含 DST 与区间合法性），容量必须为非负整数；
    # 同一资源的时段在 UTC 轴上不得重叠（首尾相接允许，区间按左闭右开处理）。
    capacity_windows_by_resource: dict[str, list[CapacityWindow]] = {}
    for k, w in enumerate(req.resource_capacity_windows):
        field = f"resource_capacity_windows[{k}]"
        if not w.resource:
            errors.append({"code": "empty_resource", "field": f"{field}.resource",
                           "message": f"{field}: resource 不能为空"})
            continue
        parsed = parse_period(w.start, w.end, w.timezone, field)
        if w.capacity is None or w.capacity < 0:
            errors.append({"code": "bad_resource_capacity",
                           "field": f"{field}.capacity",
                           "message": f"资源 {w.resource} 的临时容量必须为非负整数"
                                      f"（实际: {w.capacity}）；容量 0 表示该时段不可占用，"
                                      "时段外沿用 resource_capacities（未声明按 1）"})
        if parsed:
            capacity_windows_by_resource.setdefault(w.resource, []).append(
                CapacityWindow(parsed[0], parsed[1],
                               w.capacity if w.capacity is not None else -1))

    for r, windows in capacity_windows_by_resource.items():
        windows.sort(key=lambda cw: cw.start)
        for prev, cur in zip(windows, windows[1:]):
            if cur.start < prev.end:
                errors.append({
                    "code": "overlapping_capacity_windows",
                    "field": "resource_capacity_windows",
                    "message": (
                        f"资源 {r} 的临时容量时段重叠："
                        f"[{datetime.fromtimestamp(prev.start, tz=timezone.utc).isoformat()} ~ "
                        f"{datetime.fromtimestamp(prev.end, tz=timezone.utc).isoformat()} UTC, 容量 {prev.capacity}] 与 "
                        f"[{datetime.fromtimestamp(cur.start, tz=timezone.utc).isoformat()} ~ "
                        f"{datetime.fromtimestamp(cur.end, tz=timezone.utc).isoformat()} UTC, 容量 {cur.capacity}]；"
                        "同一资源的容量时段不得重叠（首尾相接允许）"
                    ),
                })

    # 4) 依赖引用与环。
    deps: list[Dependency] = []
    for k, d in enumerate(req.dependencies):
        field = f"dependencies[{k}]"
        if d.upstream not in services or d.downstream not in services:
            errors.append({"code": "unknown_service", "field": field,
                           "message": f"依赖引用了不存在的服务: {d.upstream} -> {d.downstream}"})
            continue
        if d.upstream == d.downstream:
            errors.append({"code": "self_dependency", "field": field,
                           "message": f"服务 {d.upstream} 不能依赖自身"})
            continue
        deps.append(Dependency(d.upstream, d.downstream, d.gap_minutes * 60))

    if deps:
        cycle = _find_cycle(services, deps)
        if cycle:
            errors.append({"code": "dependency_cycle", "field": "dependencies",
                           "message": "服务依赖存在环，无法定义先后顺序: " + " -> ".join(cycle)})

    # 5) 团队归属与禁发。
    team_members: dict[str, list[str]] = {}
    for m in req.service_teams:
        if m.service not in services:
            errors.append({"code": "unknown_service", "field": "service_teams",
                           "message": f"团队归属引用了不存在的服务: {m.service}"})
            continue
        team_members.setdefault(m.service, []).append(m.team)

    blackouts: dict[str, list[Period]] = {}
    for b in req.team_blackouts:
        parsed = parse_period(b.start, b.end, b.timezone, f"team_blackouts[{b.team}]")
        if parsed:
            blackouts.setdefault(b.team, []).append(
                Period(parsed[0], parsed[1], f"团队 {b.team} 禁发")
            )
    # 团队没有禁发记录是合法的（表示该团队全程可发布），不报错。

    # 6) 全局维护冻结。
    freezes: list[Period] = []
    for f in req.freezes:
        parsed = parse_period(f.start, f.end, f.timezone, f"freezes[{f.label}]")
        if parsed:
            freezes.append(Period(parsed[0], parsed[1], f"维护冻结：{f.label}"))

    if dst_issues or errors:
        return None, dst_issues + errors

    return Normalized(
        services=services,
        dependencies=deps,
        team_members=team_members,
        blackouts=blackouts,
        freezes=freezes,
        resource_capacities=resource_capacities,
        resource_capacity_windows=capacity_windows_by_resource,
        max_solve_seconds=req.max_solve_seconds,
    ), []


def _find_cycle(services: dict[str, Service], deps: list[Dependency]) -> list[str] | None:
    adj: dict[str, list[str]] = {sid: [] for sid in services}
    for d in deps:
        adj[d.upstream].append(d.downstream)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in services}
    stack: list[str] = []

    def dfs(u: str) -> list[str] | None:
        color[u] = GRAY
        stack.append(u)
        for v in adj[u]:
            if color[v] == GRAY:
                i = stack.index(v)
                return stack[i:] + [v]
            if color[v] == WHITE:
                found = dfs(v)
                if found:
                    return found
        stack.pop()
        color[u] = BLACK
        return None

    for sid in services:
        if color[sid] == WHITE:
            found = dfs(sid)
            if found:
                return found
    return None
