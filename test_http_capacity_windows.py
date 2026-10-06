"""HTTP 层（/api/schedule 与 /api/schedule/diff）临时容量时段集成验证。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def svc(sid, start="2026-12-01T00:00:00Z", end="2026-12-01T06:00:00Z",
        duration=60, rollback=60, resource="shared"):
    return {
        "id": sid, "name": sid, "timezone": "UTC",
        "duration_minutes": duration, "rollback_minutes": rollback,
        "resources": [resource] if resource else [],
        "windows": [{"start": start, "end": end, "timezone": "UTC"}],
    }


PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# ---- /api/schedule 基本路径 ----

payload = {
    "services": [svc("a"), svc("b"), svc("c")],
    "resource_capacities": {"shared": 3},
    "resource_capacity_windows": [
        {"resource": "shared", "start": "2026-12-01T00:00:00Z",
         "end": "2026-12-01T02:00:00Z", "timezone": "UTC", "capacity": 1},
    ],
}
resp = client.post("/api/schedule", json=payload)
check("schedule 200", resp.status_code == 200, resp.status_code)
data = resp.json()
check("schedule feasible=true", data["feasible"] is True, data)
starts = {item["service_id"]: item["deploy_start_utc"] for item in data["schedule"]}
check("schedule 一个 00:00 两个 02:00",
      sorted(starts.values()) == [
          "2026-12-01T00:00:00+00:00",
          "2026-12-01T02:00:00+00:00",
          "2026-12-01T02:00:00+00:00"], starts)

# 容量 0 -> feasible=false + 资源阻塞组合
payload0 = {
    "services": [svc("a", end="2026-12-01T03:00:00Z")],
    "resource_capacity_windows": [
        {"resource": "shared", "start": "2026-12-01T00:00:00Z",
         "end": "2026-12-01T02:00:00Z", "timezone": "UTC", "capacity": 0},
    ],
}
resp = client.post("/api/schedule", json=payload0)
check("封禁无解仍 200", resp.status_code == 200, resp.status_code)
data = resp.json()
check("feasible=false", data["feasible"] is False, data)
check("schedule=null（不返回部分排程）", data["schedule"] is None, data)
codes = [b["code"] for b in data["blockers"]]
check("blockers 含资源组合", "resource:shared" in codes, codes)

# 422 路径：重叠 / 负容量 / DST
resp = client.post("/api/schedule", json={
    "services": [svc("a")],
    "resource_capacity_windows": [
        {"resource": "shared", "start": "2026-12-01T00:00:00Z",
         "end": "2026-12-01T02:00:00Z", "timezone": "UTC", "capacity": 1},
        {"resource": "shared", "start": "2026-12-01T01:00:00Z",
         "end": "2026-12-01T03:00:00Z", "timezone": "UTC", "capacity": 2},
    ],
})
check("重叠 -> 422", resp.status_code == 422, resp.status_code)
check("错误码 overlapping_capacity_windows",
      any(e["code"] == "overlapping_capacity_windows" for e in resp.json()["errors"]),
      resp.json())

resp = client.post("/api/schedule", json={
    "services": [svc("a")],
    "resource_capacity_windows": [
        {"resource": "shared", "start": "2026-12-01T00:00:00Z",
         "end": "2026-12-01T02:00:00Z", "timezone": "UTC", "capacity": -2},
    ],
})
check("负容量 -> 422", resp.status_code == 422, resp.status_code)
check("错误码 bad_resource_capacity",
      any(e["code"] == "bad_resource_capacity" for e in resp.json()["errors"]),
      resp.json())

resp = client.post("/api/schedule", json={
    "services": [svc("a")],
    "resource_capacity_windows": [
        {"resource": "shared", "start": "2027-11-07T01:30",
         "end": "2027-11-07T04:00", "timezone": "America/New_York", "capacity": 0},
    ],
})
check("DST 歧义 -> 422", resp.status_code == 422, resp.status_code)
check("错误码 dst_ambiguous",
      any(e["code"] == "dst_ambiguous" for e in resp.json()["errors"]),
      resp.json())

# 不带容量时段的旧请求行为一致（基准 1 -> 两服务串行）
resp = client.post("/api/schedule", json={
    "services": [svc("a"), svc("b")],
})
check("无时段请求 200", resp.status_code == 200, resp.status_code)
vals = sorted(i["deploy_start_utc"] for i in resp.json()["schedule"])
check("无时段：基准容量1 两服务串行",
      vals == ["2026-12-01T00:00:00+00:00", "2026-12-01T02:00:00+00:00"], vals)

# ---- /api/schedule/diff：两侧各自使用自己的容量时段 ----

current = {
    "services": [svc("a"), svc("b"), svc("c")],
    "resource_capacities": {"shared": 3},
}
modified = {
    "services": [svc("a"), svc("b"), svc("c")],
    "resource_capacities": {"shared": 3},
    "resource_capacity_windows": [
        {"resource": "shared", "start": "2026-12-01T00:00:00Z",
         "end": "2026-12-01T02:00:00Z", "timezone": "UTC", "capacity": 1},
    ],
}
resp = client.post("/api/schedule/diff", json={"current": current, "modified": modified})
check("diff 200", resp.status_code == 200, resp.status_code)
d = resp.json()
check("diff feasible=true", d["feasible"] is True, d)
changed = {c["service_id"]: c for c in d["diff"]["changed"]}
check("diff 两个服务被推迟 120 分钟",
      len(changed) == 2
      and all(c["deploy_start"]["delta_minutes"] == 120.0 for c in changed.values()),
      changed)
check("diff 无 failures", d["failures"] is None, d)

# modified 侧容量 0 导致无解：只在该侧给阻塞组合，diff=null，HTTP 仍 200
modified_blocked = {
    "services": [svc("a", end="2026-12-01T03:00:00Z")],
    "resource_capacity_windows": [
        {"resource": "shared", "start": "2026-12-01T00:00:00Z",
         "end": "2026-12-01T02:00:00Z", "timezone": "UTC", "capacity": 0},
    ],
}
current_ok = {
    "services": [svc("a", end="2026-12-01T03:00:00Z")],
}
resp = client.post("/api/schedule/diff",
                   json={"current": current_ok, "modified": modified_blocked})
check("单侧无解仍 200", resp.status_code == 200, resp.status_code)
d = resp.json()
check("单侧无解 feasible=false", d["feasible"] is False, d)
check("diff=null", d["diff"] is None, d)
check("failures 仅 modified 侧",
      set(d["failures"]) == {"modified"}, d["failures"])
check("modified 为 infeasible + 资源阻塞",
      d["failures"]["modified"]["kind"] == "infeasible"
      and any(b["code"] == "resource:shared"
              for b in d["failures"]["modified"]["blockers"]),
      d["failures"])

# current 侧 422：diff 整体 422
bad_current = {
    "services": [svc("a")],
    "resource_capacity_windows": [
        {"resource": "shared", "start": "2026-12-01T02:00:00Z",
         "end": "2026-12-01T01:00:00Z", "timezone": "UTC", "capacity": 0},
    ],
}
resp = client.post("/api/schedule/diff",
                   json={"current": bad_current, "modified": current_ok})
check("单侧 422 -> 整体 422", resp.status_code == 422, resp.status_code)
d = resp.json()
check("422 failures 落在 current/validation",
      d["failures"]["current"]["kind"] == "validation"
      and any(e["code"] == "bad_interval"
              for e in d["failures"]["current"]["errors"]),
      d)

# 空差异：两侧完全相同（含相同容量时段）
same = {
    "services": [svc("a"), svc("b")],
    "resource_capacity_windows": [
        {"resource": "shared", "start": "2026-12-01T00:00:00Z",
         "end": "2026-12-01T02:00:00Z", "timezone": "UTC", "capacity": 2},
    ],
}
resp = client.post("/api/schedule/diff", json={"current": same, "modified": same})
d = resp.json()
check("相同请求空差异 200", resp.status_code == 200, d)
check("changed/added/removed 均空",
      d["diff"]["changed"] == [] and d["diff"]["added"] == []
      and d["diff"]["removed"] == [], d)

# 健康检查
check("health", client.get("/api/health").json() == {"status": "ok"}, "")

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
