import json

import pytest

from svc_panel.monitoring import BuiltinMonitoringProvider
from svc_panel.store import Store


def counter(total, status=200, category="business"):
    return {
        "endpoint": "/query",
        "method": "POST",
        "category": category,
        "status_code": status,
        "requests_total": total,
        "duration_seconds_sum": total * 0.01,
    }


def seed(store, instance, time, counters, boot="a"):
    with store.connection() as db:
        db.execute(
            "INSERT INTO samples VALUES(?,?,?,?)", (instance, time, boot, json.dumps(counters))
        )


def test_isolated_instance_deltas_and_excluded_probes(tmp_path, monkeypatch):
    store = Store(tmp_path)
    provider = BuiltinMonitoringProvider(store, 30)
    monkeypatch.setattr("svc_panel.monitoring.time.time", lambda: 1000)
    seed(store, "i1", 940, [counter(100), counter(4, 500), counter(20, category="probe")])
    seed(store, "i1", 970, [counter(110), counter(7, 500), counter(200, category="probe")])
    seed(store, "i2", 940, [counter(0)])
    seed(store, "i2", 970, [counter(1000)])
    result = provider.summary("i1", 15)
    assert result["requests"] == 13
    assert result["ok_200"] == 10 and result["non_200"] == 3
    assert result["mean_duration_ms"] == pytest.approx(10)
    assert provider.summary(None, 15)["requests"] == 1013
    assert provider.summary("i1", 15, "/other")["requests"] == 0


def test_resets_gaps_and_missing_are_not_fabricated_zero(tmp_path, monkeypatch):
    store = Store(tmp_path)
    provider = BuiltinMonitoringProvider(store, 30)
    monkeypatch.setattr("svc_panel.monitoring.time.time", lambda: 1000)
    assert not provider.summary("i1")["available"]
    seed(store, "i1", 800, [counter(100)])
    seed(store, "i1", 900, [counter(200)])  # Gap: do not interpolate across outage.
    seed(store, "i1", 930, [counter(10)], boot="b")  # Restart: no negative delta.
    result = provider.summary("i1")
    assert result["requests"] == 0 and not result["available"]
    seed(store, "i1", 960, [counter(12)], boot="b")
    assert provider.summary("i1")["requests"] == 2


def test_non_200_includes_other_success_codes_and_window_clipping(tmp_path, monkeypatch):
    store = Store(tmp_path)
    provider = BuiltinMonitoringProvider(store, 30)
    monkeypatch.setattr("svc_panel.monitoring.time.time", lambda: 1000)
    seed(store, "i1", 925, [counter(0, 201)])
    seed(store, "i1", 955, [counter(30, 201)])
    result = provider.summary("i1", 1)
    assert result["non_200"] == 15  # Window begins at 940.
    assert sum(b["requests"] for b in result["series"]) == 15


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), "12"])
def test_invalid_metrics_rejected(tmp_path, value):
    provider = BuiltinMonitoringProvider(Store(tmp_path))
    item = counter(0)
    item["requests_total"] = value
    with pytest.raises(ValueError):
        provider.record("i1", {"boot_id": "a", "counters": [item]})
