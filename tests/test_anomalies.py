"""The anomaly file: each anomaly once, grouped by day."""
import datetime as dt

from indexer import anomalies

UTC = dt.timezone.utc


def test_each_anomaly_is_written_once_and_grouped_by_day(tmp_path):
    f = tmp_path / "a" / "ANOMALIES.txt"
    d1 = dt.datetime(2026, 11, 20, 10, 5, tzinfo=UTC)
    d2 = dt.datetime(2026, 11, 21, 3, 0, tzinfo=UTC)
    assert anomalies.append(f, [("k1", "ccip / mint above", "m1"), ("k2", "supply / base", "s1"), ("k1", "dup", "x")], d1) == 2
    assert anomalies.append(f, [("k1", "ccip / mint above", "again"), ("k3", "crash / bsc", "c1")], d1) == 1
    assert anomalies.append(f, [("k3", "crash / bsc", "c1 again"), ("k4", "balances / bsc", "b1")], d2) == 1
    text = f.read_text()
    assert text.startswith("# EXPLORIZE ANOMALIES")
    assert text.count("[key=k1]") == 1 and text.count("[key=k3]") == 1
    assert text.index("## 2026-11-20") < text.index("[key=k3]") < text.index("## 2026-11-21") < text.index("[key=k4]")
    assert anomalies.known_keys(f) == {"k1", "k2", "k3", "k4"}


def test_nothing_new_leaves_the_file_untouched(tmp_path):
    f = tmp_path / "ANOMALIES.txt"
    assert anomalies.append(f, []) == 0 and not f.exists()
    anomalies.append(f, [("k", "x", "y")])
    before = f.read_text()
    assert anomalies.append(f, [("k", "x", "z")]) == 0 and f.read_text() == before
