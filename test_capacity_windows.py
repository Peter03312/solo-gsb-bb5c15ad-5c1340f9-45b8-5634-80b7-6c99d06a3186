"""临时容量时段（resource_capacity_windows）端到端行为验证。"""

from datetime import datetime, timezone

from app.models import ScheduleRequest
from app.diff import run_request

H0 = int(datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp())


def svc(sid, start="2026-12-01T00:00:00Z", end="2026-12-01T06:00:00Z",
        duration=60, rollback=60, resource="shared"):
    return {
        "id": sid, "name": sid, "timezone": "UTC",
        "duration_minutes": duration, "rollback_minutes": rollback,
        "resources": [resource] if resource else [],
        "windows": [{"start": start, "end": end, "timezone": "UTC"}],
    }


def run(payload):
    req = ScheduleRequest.model_validate(payload)
    out = run_request(req)
    if out.validation_errors is not None:
        return {"validation_errors": out.validation_errors}
    assert out.norm is not None and out.result is not None
    r = out.result
    return {
        "feasible": r.feasible,
        "status": r.status,
        "starts": r.starts,
        "blockers": r.blockers,
    }


def starts_minutes(res):
    assert res["feasible"], res
    return {sid: (st - H0) // 60 for sid, st in res["starts"].items()}


PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def capacity_respected(sm, windows, base_cap, occ=120, h1=360):
    """sm: sid -> 相对分钟；windows: (起,止,容量) 相对分钟；在所有边界点采样计数。"""
    bounds = {0}
    for ws, we, _ in windows:
        bounds.add(ws)
        bounds.add(we)
    bounds.add(h1)
    for t in sorted(bounds):
        cap = base_cap
        for ws, we, c in windows:
            if ws <= t < we:
                cap = c
        n = sum(1 for s in sm.values() if s <= t < s + occ)
        if n > cap:
            return False, (t, n, cap)
    return True, None


base = {
    "services": [svc("a"), svc("b"), svc("c")],
    "resource_capacities": {"shared": 3},
}

# 1) 不带容量时段：基准容量 3，三个服务并行（回归一致性）。
r = run(base)
sm = starts_minutes(r)
check("无窗口：基准容量3三者并行", sm == {"a": 0, "b": 0, "c": 0}, sm)

# 2) 00:00-02:00 降容到 1：容量 1 段必须串行；02:00 后容量 3 可并行。
p = dict(base)
p["resource_capacity_windows"] = [
    {"resource": "shared", "start": "2026-12-01T00:00:00Z",
     "end": "2026-12-01T02:00:00Z", "timezone": "UTC", "capacity": 1},
]
r = run(p)
sm = starts_minutes(r)
print("   降容1 排程:", sm)
ok, bad = capacity_respected(sm, [(0, 120, 1)], 3)
check("降容1：分段容量约束被遵守", ok, bad)
check("降容1：结果可行", r["feasible"], r)
# 一个占用 [0,120) 覆盖整段；另两个 s=120 起，与段交集仅点 120（空），允许。
check("降容1：最优排程 c=0,a=b=120", sm == {"a": 120, "b": 120, "c": 0}, sm)

# 3) 容量 0 时段封禁：所有占用必须完全避开 [60,120)。
p = dict(base)
p["resource_capacity_windows"] = [
    {"resource": "shared", "start": "2026-12-01T01:00:00Z",
     "end": "2026-12-01T02:00:00Z", "timezone": "UTC", "capacity": 0},
]
r = run(p)
sm = starts_minutes(r)
print("   封禁 排程:", sm)
check("封禁：可行", r["feasible"], r)
ok, bad = capacity_respected(sm, [(60, 120, 0)], 3)
check("封禁：无占用进入 [60,120)", ok, bad)
# 2h 占用不能覆盖点 60..119；最早合法起点 120，三者并行。
check("封禁：三者错到 120 并行", sm == {"a": 120, "b": 120, "c": 120}, sm)

# 4) 降容时刻落在既有占用期间：[0,60) cap3，[60,360) cap2，三个 2h 占用。
p = dict(base)
p["resource_capacity_windows"] = [
    {"resource": "shared", "start": "2026-12-01T01:00:00Z",
     "end": "2026-12-01T06:00:00Z", "timezone": "UTC", "capacity": 2},
]
r = run(p)
sm = starts_minutes(r)
print("   降容2@60 排程:", sm)
ok, bad = capacity_respected(sm, [(60, 360, 2)], 3)
check("降容2@60：分段容量被遵守（含跨越降容时刻的占用）", ok, bad)
# a,b=0 [0,120) 在 t=60 仍占名额（回滚期），c 最早 120（服务对称，只校验时刻集合）。
check("降容2@60：两个 0 一个 120", sorted(sm.values()) == [0, 0, 120], sm)

# 5) 容量约束无解：窗口 [0,180)，cap0 段 [0,120)，2h 占用无处安放。
p = {
    "services": [svc("a", end="2026-12-01T03:00:00Z")],
    "resource_capacities": {"shared": 1},
    "resource_capacity_windows": [
        {"resource": "shared", "start": "2026-12-01T00:00:00Z",
         "end": "2026-12-01T02:00:00Z", "timezone": "UTC", "capacity": 0},
    ],
}
r = run(p)
check("封禁导致无解：feasible=false", not r["feasible"], r)
codes = [b["code"] for b in (r.get("blockers") or [])]
check("无解 blockers 含 resource:shared", "resource:shared" in codes, codes)
check("无解不返回部分排程（无 starts）", r.get("starts") is None, r)

# 6) 时段外沿用基准、未声明基准仍为 1：cap2 段 [0,60)，其余 cap1。
p = {
    "services": [svc("a"), svc("b"), svc("c")],
    "resource_capacity_windows": [
        {"resource": "shared", "start": "2026-12-01T00:00:00Z",
         "end": "2026-12-01T01:00:00Z", "timezone": "UTC", "capacity": 2},
    ],
}
r = run(p)
sm = starts_minutes(r)
print("   基准1+临时2 排程:", sm)
ok, bad = capacity_respected(sm, [(0, 60, 2)], 1)
check("基准1：分段容量被遵守", ok, bad)

# 7) 输入错误：时段重叠 -> 422
p = dict(base)
p["resource_capacity_windows"] = [
    {"resource": "shared", "start": "2026-12-01T00:00:00Z",
     "end": "2026-12-01T02:00:00Z", "timezone": "UTC", "capacity": 1},
    {"resource": "shared", "start": "2026-12-01T01:00:00Z",
     "end": "2026-12-01T03:00:00Z", "timezone": "UTC", "capacity": 2},
]
r = run(p)
errs = r.get("validation_errors")
check("重叠时段被拒", bool(errs) and any(
    e["code"] == "overlapping_capacity_windows" for e in errs), errs)
# 首尾相接允许
p2 = dict(base)
p2["resource_capacity_windows"] = [
    {"resource": "shared", "start": "2026-12-01T00:00:00Z",
     "end": "2026-12-01T02:00:00Z", "timezone": "UTC", "capacity": 1},
    {"resource": "shared", "start": "2026-12-01T02:00:00Z",
     "end": "2026-12-01T03:00:00Z", "timezone": "UTC", "capacity": 2},
]
r2 = run(p2)
check("首尾相接允许", r2["feasible"], r2)
# 不同资源的“重叠”允许
p3 = dict(base)
p3["services"] = [svc("a", resource="r1"), svc("b", resource="r2")]
p3["resource_capacities"] = {"r1": 1, "r2": 1}
p3["resource_capacity_windows"] = [
    {"resource": "r1", "start": "2026-12-01T00:00:00Z",
     "end": "2026-12-01T02:00:00Z", "timezone": "UTC", "capacity": 0},
    {"resource": "r2", "start": "2026-12-01T00:00:00Z",
     "end": "2026-12-01T02:00:00Z", "timezone": "UTC", "capacity": 0},
]
r3 = run(p3)
check("不同资源同时段各自生效", r3["feasible"], r3)

# 8) 输入错误：结束不晚于开始；容量负数；DST 歧义
p = dict(base)
p["resource_capacity_windows"] = [
    {"resource": "shared", "start": "2026-12-01T02:00:00Z",
     "end": "2026-12-01T02:00:00Z", "timezone": "UTC", "capacity": 1},
]
r = run(p)
errs = r.get("validation_errors") or []
check("零长度时段被拒(bad_interval)",
      any(e["code"] == "bad_interval" for e in errs), errs)

p = dict(base)
p["resource_capacity_windows"] = [
    {"resource": "shared", "start": "2026-12-01T00:00:00Z",
     "end": "2026-12-01T02:00:00Z", "timezone": "UTC", "capacity": -1},
]
r = run(p)
errs = r.get("validation_errors") or []
check("负容量被拒(bad_resource_capacity)",
      any(e["code"] == "bad_resource_capacity" for e in errs), errs)

# DST：America/New_York 2027-03-14 02:30 不存在（前拨）
p = dict(base)
p["resource_capacity_windows"] = [
    {"resource": "shared", "start": "2027-03-14T02:30",
     "end": "2027-03-14T06:00", "timezone": "America/New_York", "capacity": 0},
]
r = run(p)
errs = r.get("validation_errors") or []
check("DST 不存在时间被拒(dst_nonexistent)",
      any(e["code"] == "dst_nonexistent" for e in errs), errs)
# DST 重复：2027-11-07 01:30 New_York
p = dict(base)
p["resource_capacity_windows"] = [
    {"resource": "shared", "start": "2027-11-07T01:30",
     "end": "2027-11-07T04:00", "timezone": "America/New_York", "capacity": 0},
]
r = run(p)
errs = r.get("validation_errors") or []
check("DST 重复时间被拒(dst_ambiguous)",
      any(e["code"] == "dst_ambiguous" for e in errs), errs)
# 带偏移量的绝对时间即使落在 gap 也无歧义
p = dict(base)
p["resource_capacity_windows"] = [
    {"resource": "shared", "start": "2027-03-14T07:30:00Z",
     "end": "2027-03-14T09:00:00Z", "timezone": "America/New_York", "capacity": 0},
]
r = run(p)
check("绝对时间无 DST 歧义", r["feasible"], r)

# 坏时区
p = dict(base)
p["resource_capacity_windows"] = [
    {"resource": "shared", "start": "2026-12-01T00:00:00Z",
     "end": "2026-12-01T02:00:00Z", "timezone": "Mars/Olympus", "capacity": 0},
]
r = run(p)
errs = r.get("validation_errors") or []
check("坏时区被拒(bad_timezone)",
      any(e["code"] == "bad_timezone" for e in errs), errs)

# 9) 多段组合 + 升容：基准1，[0,60) 升容 3，[120,180) 封禁。
#    3 个 2h 占用：[0,120) 起点违规（覆盖封禁），可行起点需满足 [s,s+120) 避开 [120,180)
#    且基准段（cap1）串行 —— 视界拉到 8h：s ∈ {60? 覆盖120..180 违规}，
#    合法：s=180 起（[180,300)）三者在 cap1 下串行到 180/300/420。
p = {
    "services": [svc("a", end="2026-12-01T08:00:00Z"),
                 svc("b", end="2026-12-01T08:00:00Z"),
                 svc("c", end="2026-12-01T08:00:00Z")],
    "resource_capacity_windows": [
        {"resource": "shared", "start": "2026-12-01T00:00:00Z",
         "end": "2026-12-01T01:00:00Z", "timezone": "UTC", "capacity": 3},
        {"resource": "shared", "start": "2026-12-01T02:00:00Z",
         "end": "2026-12-01T03:00:00Z", "timezone": "UTC", "capacity": 0},
    ],
}
r = run(p)
sm = starts_minutes(r)
print("   升容3+封禁 排程:", sm)
ok, bad = capacity_respected(sm, [(0, 60, 3), (120, 180, 0)], 1, h1=480)
check("升容+封禁：分段容量被遵守", r["feasible"] and ok, (r, bad))

# 9b) 同上但视界仅 6h：封禁+基准串行确实无解（feasible=false，含资源阻塞组合）。
p9b = {
    "services": [svc("a"), svc("b"), svc("c")],
    "resource_capacity_windows": [
        {"resource": "shared", "start": "2026-12-01T00:00:00Z",
         "end": "2026-12-01T01:00:00Z", "timezone": "UTC", "capacity": 3},
        {"resource": "shared", "start": "2026-12-01T02:00:00Z",
         "end": "2026-12-01T03:00:00Z", "timezone": "UTC", "capacity": 0},
    ],
}
r = run(p9b)
check("升容+封禁(6h视界)：无解 feasible=false", not r["feasible"], r)
codes = [b["code"] for b in (r.get("blockers") or [])]
check("升容+封禁无解 blockers 含 resource:shared",
      "resource:shared" in codes, codes)

# 10) cap>=2 段的裁剪正确性：基准 3，[120,180) 段 cap2，4 个用户。
p = {
    "services": [svc("a"), svc("b"), svc("c"), svc("d")],
    "resource_capacities": {"shared": 3},
    "resource_capacity_windows": [
        {"resource": "shared", "start": "2026-12-01T02:00:00Z",
         "end": "2026-12-01T03:00:00Z", "timezone": "UTC", "capacity": 2},
    ],
}
r = run(p)
sm = starts_minutes(r)
print("   cap3/cap2@[120,180) 排程:", sm)
ok, bad = capacity_respected(sm, [(120, 180, 2)], 3)
check("cap≥2 裁剪：段内≤2、段外≤3", r["feasible"] and ok, (r, bad))

# 10b) 段边界与段外更高并发：a/b 为长占用（6h，覆盖整段），c 用候选窗锁定
#      最早 03:00 开始（30 分钟短占用）。段 [120,180) 容量 2（仅 a/b），
#      t∈[180,210) 段外 a/b/c 三者并行（基准容量 3）合法。
p = {
    "services": [
        svc("a", start="2026-12-01T00:00:00Z", end="2026-12-01T07:00:00Z",
            duration=60, rollback=300),
        svc("b", start="2026-12-01T00:00:00Z", end="2026-12-01T07:00:00Z",
            duration=60, rollback=300),
        svc("c", start="2026-12-01T03:00:00Z", end="2026-12-01T07:00:00Z",
            duration=30, rollback=0),
    ],
    "resource_capacities": {"shared": 3},
    "resource_capacity_windows": [
        {"resource": "shared", "start": "2026-12-01T02:00:00Z",
         "end": "2026-12-01T03:00:00Z", "timezone": "UTC", "capacity": 2},
    ],
}
r = run(p)
sm = starts_minutes(r)
print("   裁剪特化 排程:", sm)
# 显式校验：段内 [120,180) 任意点 ≤2；t=180 段外允许 3。
def count_at(sm, t, occs):
    return sum(1 for sid, s in sm.items() if s <= t < s + occs[sid])
occs = {"a": 360, "b": 360, "c": 30}
check("裁剪：a=b=0,c=180 可行",
      r["feasible"] and sm == {"a": 0, "b": 0, "c": 180}, sm)
check("裁剪：段内 t=150 计数 2", count_at(sm, 150, occs) == 2, count_at(sm, 150, occs))
check("裁剪：段外 t=180 计数 3（基准容量内）", count_at(sm, 180, occs) == 3,
      count_at(sm, 180, occs))

# 10c) 段内完全包含型裁剪：基准 3，[0,120) 段容量 2，三个 30 分钟占用。
#      最优：两个 0 开始、第三个 30 开始（段内任意点 ≤2）。
p = {
    "services": [
        svc("a", duration=30, rollback=0),
        svc("b", duration=30, rollback=0),
        svc("c", duration=30, rollback=0),
    ],
    "resource_capacities": {"shared": 3},
    "resource_capacity_windows": [
        {"resource": "shared", "start": "2026-12-01T00:00:00Z",
         "end": "2026-12-01T02:00:00Z", "timezone": "UTC", "capacity": 2},
    ],
}
r = run(p)
sm = starts_minutes(r)
print("   段内包含 排程:", sm)
ok, bad = capacity_respected(sm, [(0, 120, 2)], 3, occ=30)
check("段内包含裁剪：时刻集合 [0,0,30] 且容量被遵守",
      r["feasible"] and sorted(sm.values()) == [0, 0, 30] and ok, (sm, bad))

# 11) 窗口以墙上时间+非 UTC 时区声明（Europe/Berlin，2026-12 为 UTC+1）。
p = {
    "services": [svc("a"), svc("b")],
    "resource_capacities": {"shared": 2},
    "resource_capacity_windows": [
        {"resource": "shared", "start": "2026-12-01T02:00",
         "end": "2026-12-01T03:00", "timezone": "Europe/Berlin", "capacity": 0},
    ],
}
r = run(p)
sm = starts_minutes(r)
print("   Berlin 封禁 排程:", sm)
ok, bad = capacity_respected(sm, [(60, 120, 0)], 2)
check("时区换算正确（Berlin 02:00 = UTC 01:00）", r["feasible"] and ok, (r, bad))

# 12) 容量时段只引用未被任何服务使用的资源：合法且不影响结果。
p = dict(base)
p["resource_capacity_windows"] = [
    {"resource": "nobody-uses", "start": "2026-12-01T00:00:00Z",
     "end": "2026-12-01T02:00:00Z", "timezone": "UTC", "capacity": 0},
]
r = run(p)
check("未使用资源的时段被忽略",
      r["feasible"] and starts_minutes(r) == {"a": 0, "b": 0, "c": 0}, r)

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
