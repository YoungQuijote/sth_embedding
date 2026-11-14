from __future__ import annotations

import json
import math
import time
from typing import Protocol


class MonitoringProvider(Protocol):
    def summary(
        self,
        instance_id: str | None,
        minutes: int,
        endpoint: str | None = None,
        method: str | None = None,
    ) -> dict: ...


class BuiltinMonitoringProvider:
    def __init__(self, store, interval=30):
        self.store = store
        self.interval = interval

    def record(self, instance, metrics):
        if not isinstance(metrics.get("boot_id"), str) or not metrics["boot_id"]:
            raise ValueError("metrics 缺少 boot_id")
        counters = metrics.get("counters")
        if not isinstance(counters, list) or len(counters) > 10000:
            raise ValueError("metrics counters 格式或数量错误")
        keys = set()
        for counter in counters:
            key = (
                counter["endpoint"],
                counter["method"],
                counter["category"],
                counter["status_code"],
            )
            if key in keys:
                raise ValueError("重复的指标维度")
            keys.add(key)
            for name in ("requests_total", "duration_seconds_sum"):
                value = counter[name]
                if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError("无效的指标计数")
        now = time.time()
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO samples VALUES(?,?,?,?)",
                (instance, now, metrics["boot_id"], json.dumps(counters)),
            )
            db.execute("DELETE FROM samples WHERE timestamp < ?", (now - 7 * 86400,))

    def summary(self, instance_id=None, minutes=60, endpoint=None, method=None):
        now = time.time()
        start = now - minutes * 60
        bucket_width = max(60, math.ceil(minutes * 60 / 30))
        buckets = [
            {
                "timestamp": start + i * bucket_width,
                "requests": 0.0,
                "non_200": 0.0,
                "available": False,
            }
            for i in range(math.ceil(minutes * 60 / bucket_width))
        ]
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT * FROM samples WHERE timestamp>=? "
                + ("AND instance_id=? " if instance_id else "")
                + "ORDER BY instance_id,timestamp",
                (start - self.interval * 3, instance_id)
                if instance_id
                else (start - self.interval * 3,),
            ).fetchall()
        previous = {}
        observed_seconds = {}
        observed_requests = {}
        totals = {"requests": 0.0, "ok_200": 0.0, "non_200": 0.0, "duration_seconds": 0.0}
        routes = {}
        intervals = 0
        for row in rows:
            current = {
                tuple(c[k] for k in ("endpoint", "method", "category", "status_code")): c
                for c in json.loads(row["value"])
            }
            old = previous.get(row["instance_id"])
            previous[row["instance_id"]] = (row["timestamp"], row["boot_id"], current)
            if not old:
                continue
            elapsed = row["timestamp"] - old[0]
            if (
                old[1] != row["boot_id"]
                or elapsed <= 0
                or elapsed > self.interval * 2.5
                or row["timestamp"] <= start
            ):
                continue
            intervals += 1
            lo, hi = max(start, old[0]), min(now, row["timestamp"])
            identifier = row["instance_id"]
            observed_seconds[identifier] = observed_seconds.get(identifier, 0) + hi - lo
            for bucket in buckets:
                if min(hi, bucket["timestamp"] + bucket_width) > max(lo, bucket["timestamp"]):
                    bucket["available"] = True
            for key, value in current.items():
                route, verb, category, status = key
                if (
                    category != "business"
                    or (endpoint and endpoint != route)
                    or (method and method != verb)
                ):
                    continue
                prev = old[2].get(key, {"requests_total": 0, "duration_seconds_sum": 0})
                count = value["requests_total"] - prev["requests_total"]
                duration = value["duration_seconds_sum"] - prev["duration_seconds_sum"]
                if count < 0 or duration < 0:
                    continue
                fraction = (hi - lo) / elapsed
                count_in_window = count * fraction
                observed_requests[identifier] = (
                    observed_requests.get(identifier, 0) + count_in_window
                )
                totals["requests"] += count_in_window
                totals["ok_200" if status == 200 else "non_200"] += count_in_window
                totals["duration_seconds"] += duration * fraction
                route_total = routes.setdefault(
                    (route, verb),
                    {"endpoint": route, "method": verb, "ok_200": 0.0, "non_200": 0.0},
                )
                route_total["ok_200" if status == 200 else "non_200"] += count_in_window
                for bucket in buckets:
                    overlap = max(
                        0,
                        min(hi, bucket["timestamp"] + bucket_width) - max(lo, bucket["timestamp"]),
                    )
                    bucket["requests"] += count * overlap / elapsed
                    if status != 200:
                        bucket["non_200"] += count * overlap / elapsed
        return {
            **totals,
            "qps": sum(
                observed_requests.get(identifier, 0) / seconds
                for identifier, seconds in observed_seconds.items()
                if seconds > 0
            ),
            "observed_seconds": observed_seconds,
            "mean_duration_ms": totals["duration_seconds"] * 1000 / totals["requests"]
            if totals["requests"]
            else 0,
            "non_200_ratio": totals["non_200"] / totals["requests"] if totals["requests"] else 0,
            "series": buckets,
            "endpoints": list(routes.values()),
            "available": intervals > 0,
            "minutes": minutes,
            "approximate": True,
            "source": "builtin",
            "retention_days": 7,
        }
