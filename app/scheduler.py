"""CP-SAT 调度引擎。

建模（全部时间为 epoch 秒）：
- 每个服务一个部署起点 start；部署区间 [start, start+deploy)，
  占用区间 [start, start+deploy+rollback)。
- 起点必须落在候选时间窗的并集内：对并集在总时间轴上的每个“补集缺口”建立
  NoOverlap（占用区间 vs 缺口）。
- 维护冻结：占用区间不得与任何冻结区间相交（部署与回滚窗口都要避开）。
- 团队禁发：仅部署区间不得与所属团队禁发时段相交（回滚允许进入禁发期）。
- 共享资源：同一资源任意时刻的占用数不得超过其**当时容量**。基准容量取
  resource_capacities（默认 1，即两两不重叠；更大的正整数允许至多容量个服务并行）；
  resource_capacity_windows 可按资源给出若干左闭右开的临时容量时段，时段内覆盖为
  时段容量（非负整数，0 = 该时段完全不可占用），时段外沿用基准容量。视界按时段边界
  切成容量恒定的小段：容量 0 段用 NoOverlap（占用 vs 固定段区间）逐用户封禁；
  容量 1 段用“与该段相交”gate 的完整镜像做两两 NoOverlap；容量 ≥2 段把占用裁剪到
  段内后做 AddCumulative，避免段外重叠被误计。降容时刻落在既有占用期间也须遵守。
- 依赖：downstream.start >= upstream.start + deploy_up + gap。

CP-SAT 的 NoOverlap 不接受约束级 enforcement literal，因此采用“可选镜像区间”：
每个受假设控制的约束都有一个布尔字面量，把参与区间建成以该字面量为 presence 的
可选区间（固定时段则建常量轴上的可选固定区间）。字面量为假时区间缺席，
NoOverlap 自动忽略；求解时把所有字面量加入假设（assumptions）。
不可行时 CP-SAT 返回足以导致矛盾的字面量子集（sufficient assumption core），
即“阻塞它的约束组合”，而不会给出任何部分排程。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ortools.sat.python import cp_model

from .validator import Normalized


@dataclass
class SolveResult:
    feasible: bool
    status: str  # optimal | feasible | infeasible | unknown
    starts: dict[str, int] | None
    blockers: list[dict] | None


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    out = [list(ordered[0])]
    for a, b in ordered[1:]:
        if a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _complement(union: list[tuple[int, int]], h0: int, h1: int) -> list[tuple[int, int]]:
    gaps: list[tuple[int, int]] = []
    cur = h0
    for a, b in union:
        if a > cur:
            gaps.append((cur, a))
        cur = max(cur, b)
    if cur < h1:
        gaps.append((cur, h1))
    return gaps


def solve(norm: Normalized) -> SolveResult:
    m = cp_model.CpModel()
    services = norm.services

    h0 = min(w.start for s in services.values() for w in s.windows)
    h1 = max(w.end for s in services.values() for w in s.windows)

    # 假设字面量 index -> (code, message)，用于不可行核解释。
    assumption_map: dict[int, tuple[str, str]] = {}

    starts: dict[str, cp_model.IntVar] = {}
    deploy_iv: dict[str, cp_model.IntervalVar] = {}
    occupy_iv: dict[str, cp_model.IntervalVar] = {}

    for sid, s in services.items():
        start = m.new_int_var(h0, h1, f"start_{sid}")
        starts[sid] = start
        deploy_iv[sid] = m.new_fixed_size_interval_var(start, s.duration, f"deploy_{sid}")
        occupy_iv[sid] = m.new_fixed_size_interval_var(
            start, s.duration + s.rollback, f"occupy_{sid}")
        # 窗口之外的缺口仅在 h0~h1 内部建模，而最后的窗口终点即 h1；因此还须显式保证
        # 占用区间（含回滚）不越过 h1，否则起点取在 h1 时整段占用会漏到所有窗口之外。
        m.add(start + s.duration + s.rollback <= h1)

    def new_assumption(code: str, message: str) -> cp_model.IntVar:
        b = m.new_bool_var(f"assm_{code.replace(':', '_')}")
        m.add_assumptions([b])
        assumption_map[b.index] = (code, message)
        return b

    def opt_mirror(base_iv: cp_model.IntervalVar, lit, name: str) -> cp_model.IntervalVar:
        """以同一 start/size/end + presence=lit 建立可选镜像区间。

        注意 CP-SAT 的参数顺序是 (start, size, end, is_present, name)。
        """
        return m.new_optional_interval_var(
            base_iv.start_expr(), base_iv.size_expr(), base_iv.end_expr(), lit, name)

    def opt_constant(a: int, size: int, lit, name: str) -> cp_model.IntervalVar:
        """常量轴 [a, a+size) 上的可选固定区间。"""
        return m.new_optional_fixed_size_interval_var(m.new_constant(a), size, lit, name)

    def overlap_lit(start_expr, size_expr, a: int, b: int, name: str) -> cp_model.IntVar:
        """返回布尔变量：占用区间 [start, start+size) 与常量段 [a, b) 是否相交。

        左闭右开轴上相交等价于 start < b 且 start + size > a。
        """
        g = m.new_bool_var(f"{name}_g")  # start < b
        l = m.new_bool_var(f"{name}_l")  # start + size > a
        m.add(start_expr < b).only_enforce_if(g)
        m.add(start_expr >= b).only_enforce_if(g.negated())
        m.add(start_expr + size_expr > a).only_enforce_if(l)
        m.add(start_expr + size_expr <= a).only_enforce_if(l.negated())
        ov = m.new_bool_var(f"{name}_ov")
        m.add_bool_and(g, l).only_enforce_if(ov)
        m.add_bool_or(g.negated(), l.negated()).only_enforce_if(ov.negated())
        return ov

    def gated_presence(lit, ov, name: str) -> cp_model.IntVar:
        """p = lit AND ov：资源假设放松或占用与该段不相交时，镜像区间缺席。"""
        p = m.new_bool_var(f"{name}_p")
        m.add(p == 1).only_enforce_if([lit, ov])
        m.add(p == 0).only_enforce_if([lit.negated(), ov])
        m.add(p == 0).only_enforce_if([lit, ov.negated()])
        m.add(p == 0).only_enforce_if([lit.negated(), ov.negated()])
        return p

    # ---- 候选时间窗：占用区间必须整体落在窗口并集内 ----
    for sid, s in services.items():
        union = _merge_intervals([(w.start, w.end) for w in s.windows])
        gaps = _complement(union, h0, h1)
        for g_start, g_end in gaps:
            lit = new_assumption(
                f"window:{sid}:{g_start}",
                f"服务 {s.name}（{sid}）的部署与回滚必须落在其候选时间窗内；"
                f"窗口外时段 {_iso(g_start)} ~ {_iso(g_end)} UTC 不可发布",
            )
            m.add_no_overlap([
                opt_mirror(occupy_iv[sid], lit, f"win_occ_{sid}_{g_start}"),
                opt_constant(g_start, g_end - g_start, lit, f"win_gap_{sid}_{g_start}"),
            ])

    # ---- 全局维护冻结：占用区间（含回滚窗口）避让 ----
    for f in norm.freezes:
        for sid, s in services.items():
            lit = new_assumption(
                f"freeze:{f.label}:{sid}",
                f"{f.label}（{_iso(f.start)} ~ {_iso(f.end)} UTC）期间"
                f"服务 {s.name}（{sid}）不得部署或处于回滚窗口内",
            )
            m.add_no_overlap([
                opt_mirror(occupy_iv[sid], lit, f"fz_occ_{sid}_{f.start}"),
                opt_constant(f.start, f.end - f.start, lit, f"fz_f_{sid}_{f.start}"),
            ])

    # ---- 团队禁发：仅部署区间避让（回滚允许进入） ----
    for sid, teams in norm.team_members.items():
        for team in teams:
            for p in norm.blackouts.get(team, []):
                lit = new_assumption(
                    f"blackout:{team}:{sid}:{p.start}",
                    f"团队 {team} 禁发（{_iso(p.start)} ~ {_iso(p.end)} UTC）期间"
                    f"服务 {services[sid].name}（{sid}）不得开始部署",
                )
                m.add_no_overlap([
                    opt_mirror(deploy_iv[sid], lit, f"bo_dep_{sid}_{p.start}"),
                    opt_constant(p.start, p.end - p.start, lit, f"bo_p_{sid}_{p.start}"),
                ])

    # ---- 共享资源占用：同资源的并行占用数不得超过“当时”的容量 ----
    # 基准容量取 resource_capacities（未声明为 1）；resource_capacity_windows 给出
    # 若干左闭右开的临时容量时段，时段内覆盖为时段容量（允许 0 = 不可占用），
    # 时段外沿用基准容量。把 [h0,h1) 按时段边界切成容量恒定的小段，逐段建立约束。
    resource_users: dict[str, list[str]] = {}
    for sid, s in services.items():
        for r in s.resources:
            resource_users.setdefault(r, []).append(sid)

    for resource, users in resource_users.items():
        base_cap = norm.resource_capacities.get(resource, 1)

        def cap_segments() -> list[tuple[int, int, int]]:
            """返回与 [h0,h1) 相交的 (seg_start, seg_end, capacity) 序列。

            相邻且容量相同的段合并为一段（首尾相接的两个同容量临时时段、
            或临时时段容量恰好等于基准时会出现这种情况）。
            """
            clipped = sorted(
                (max(h0, w.start), min(h1, w.end), w.capacity)
                for w in norm.resource_capacity_windows.get(resource, [])
                if w.start < h1 and w.end > h0
            )
            raw: list[tuple[int, int, int]] = []
            cur, cap = h0, base_cap
            for a, b, c in clipped:
                if a > cur:
                    raw.append((cur, a, cap))
                raw.append((a, b, c))
                cur = b
            if cur < h1:
                raw.append((cur, h1, cap))
            merged: list[tuple[int, int, int]] = []
            for a, b, c in raw:
                if merged and merged[-1][1] == a and merged[-1][2] == c:
                    merged[-1] = (merged[-1][0], b, c)
                else:
                    merged.append((a, b, c))
            return merged

        segments = cap_segments()
        # 每一段的占用数都不超过该段容量：用户数本就不超过容量的段无需建模。
        effective = [(a, b, c) for a, b, c in segments if c < len(users)]
        if not effective:
            continue

        changed = [w for w in norm.resource_capacity_windows.get(resource, [])
                   if w.capacity != base_cap and w.start < h1 and w.end > h0]
        if not changed:
            # 临时时段容量都与基准相同（例如基准 2、时段也写 2）：沿用基准容量措辞。
            if base_cap == 1:
                message = (
                    f"共享资源 {resource} 同时只能被一个服务占用（部署+回滚窗口）；"
                    f"涉及: {', '.join(services[u].name for u in users)}"
                )
            else:
                message = (
                    f"共享资源 {resource} 的并行容量为 {base_cap}（部署+回滚窗口内任意时刻"
                    f"至多 {base_cap} 个服务占用），但共有 {len(users)} 个服务需要占用: "
                    f"{', '.join(services[u].name for u in users)}；超出容量的服务必须错峰"
                )
        else:
            detail = "；".join(
                f"{_iso(max(h0, w.start))} ~ {_iso(min(h1, w.end))} UTC "
                f"容量 {w.capacity}" + ("（该时段不可占用）" if w.capacity == 0 else "")
                for w in sorted(changed, key=lambda cw: cw.start)
            )
            message = (
                f"共享资源 {resource} 的容量随时段变化（基准 {base_cap}，未声明时段按基准）：{detail}；"
                f"部署+回滚窗口内任意时刻的占用数不得超过当时容量，共有 {len(users)} 个服务需要占用: "
                f"{', '.join(services[u].name for u in users)}；超出容量（含容量 0 时段）时必须错峰"
            )
        lit = new_assumption(f"resource:{resource}", message)

        for a, b, cap in effective:
            if cap == 0:
                # 该时段完全不可占用：每个占用区间（gate：与该段相交时才出席）都与
                # 固定段区间互斥。逐用户独立约束即可（NoOverlap 内含固定区间），
                # 不会额外限制两个用户彼此。
                for u in users:
                    ov = overlap_lit(starts[u], occupy_iv[u].size_expr(),
                                     a, b, f"r0_{resource}_{u}_{a}")
                    p = gated_presence(lit, ov, f"r0_{resource}_{u}_{a}")
                    m.add_no_overlap([
                        opt_mirror(occupy_iv[u], p, f"res0_{resource}_{u}_{a}"),
                        opt_constant(a, b - a, p, f"res0seg_{resource}_{u}_{a}"),
                    ])
            elif cap == 1:
                # 段内两两不得重叠。用完整占用区间 + “与该段相交”gate 的镜像：
                # 两个都出席（都与该段相交）的区间若在任何位置重叠就被禁止；
                # 只在段外重叠时至少有一方缺席，不会误杀。
                mirrors = []
                for u in users:
                    ov = overlap_lit(starts[u], occupy_iv[u].size_expr(),
                                     a, b, f"r1_{resource}_{u}_{a}")
                    p = gated_presence(lit, ov, f"r1_{resource}_{u}_{a}")
                    mirrors.append(
                        opt_mirror(occupy_iv[u], p, f"res1_{resource}_{u}_{a}"))
                m.add_no_overlap(mirrors)
            else:
                # cap >= 2：累计容量，必须把每个占用严格裁剪到 [a,b) 内再计数，
                # 否则段外（基准容量可能更大）的重叠会被误算。
                clips = []
                for u in users:
                    cs = m.new_int_var(a, b, f"cs_{resource}_{u}_{a}")
                    ce = m.new_int_var(a, b, f"ce_{resource}_{u}_{a}")
                    csz = m.new_int_var(0, b - a, f"csz_{resource}_{u}_{a}")
                    st = starts[u]
                    en = st + occupy_iv[u].size_expr()
                    ov = overlap_lit(st, occupy_iv[u].size_expr(),
                                     a, b, f"rc_{resource}_{u}_{a}")
                    p = gated_presence(lit, ov, f"rc_{resource}_{u}_{a}")
                    # cs = max(st, a)、ce = min(en, b)。MaxEquality/MinEquality
                    # 不支持 enforcement literal，改用两个选择器布尔量 + 线性等式。
                    sg = m.new_bool_var(f"csg_{resource}_{u}_{a}")  # st >= a
                    eb = m.new_bool_var(f"ceb_{resource}_{u}_{a}")  # en <= b
                    m.add(st >= a).only_enforce_if(sg)
                    m.add(st < a).only_enforce_if(sg.negated())
                    m.add(en <= b).only_enforce_if(eb)
                    m.add(en > b).only_enforce_if(eb.negated())
                    m.add(cs == st).only_enforce_if([ov, sg])
                    m.add(cs == a).only_enforce_if([ov, sg.negated()])
                    m.add(ce == en).only_enforce_if([ov, eb])
                    m.add(ce == b).only_enforce_if([ov, eb.negated()])
                    # 不相交时退化为点区间 [a,a)，size 0、presence 必为假。
                    m.add(cs == a).only_enforce_if(ov.negated())
                    m.add(ce == a).only_enforce_if(ov.negated())
                    m.add(cs + csz == ce)
                    clips.append(m.new_optional_interval_var(
                        cs, csz, ce, p, f"resc_{resource}_{u}_{a}"))
                m.add_cumulative(clips, [1] * len(clips), cap)

    # ---- 依赖先后 + 最小间隔（线性不等式支持直接挂 literal） ----
    for d in norm.dependencies:
        up, dn = services[d.upstream], services[d.downstream]
        lit = new_assumption(
            f"dependency:{d.upstream}->{d.downstream}",
            f"依赖顺序：{dn.name}（{d.downstream}）必须在 {up.name}（{d.upstream}）"
            f"部署完成" + (f"并再等待 {d.gap // 60} 分钟" if d.gap else "") + "之后才能开始",
        )
        m.add(
            starts[d.downstream] >= starts[d.upstream] + up.duration + d.gap
        ).only_enforce_if(lit)

    # ---- 目标：总发布开始时刻尽量早（输出稳定、可复现） ----
    m.minimize(sum(starts.values()))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = norm.max_solve_seconds
    solver.parameters.num_search_workers = 8
    status = solver.solve(m)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return SolveResult(
            feasible=True,
            status="optimal" if status == cp_model.OPTIMAL else "feasible",
            starts={sid: solver.value(v) for sid, v in starts.items()},
            blockers=None,
        )

    if status == cp_model.INFEASIBLE:
        core_literals = solver.sufficient_assumptions_for_infeasibility()
        blockers = []
        if core_literals:
            for lit in core_literals:
                code, message = assumption_map[lit]
                blockers.append({"code": code, "message": message})
        else:
            blockers = [{"code": code, "message": msg}
                        for code, msg in assumption_map.values()]
        return SolveResult(False, "infeasible", None, blockers)

    return SolveResult(
        False, "unknown", None,
        [{"code": "solver_unknown",
          "message": f"求解器在时限内未能判定可行性（状态 {solver.status_name(status)}），"
                     "可增大 max_solve_seconds 后重试"}],
    )
