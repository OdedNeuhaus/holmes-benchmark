import pytest


@pytest.mark.parametrize("value, expected", [
    ("2026-12-31T22:15:00+00:00", "2027-01-01T01:15:00.000+03:00"),
    ("2026-07-01T10:15:00+00:00", "2026-07-01T13:15:00.000+03:00"),
    ("2026-01-01T10:15:00-05:00", "2026-01-01T18:15:00.000+03:00"),
])
def test_fixed_offset_in_winter_summer_and_date_rollover(value, expected):
    from holmes_benchmark.timestamps import format_timestamp

    assert format_timestamp(value) == expected


def test_report_converts_own_timestamps_but_preserves_model_data():
    from holmes_benchmark.config import Config
    from holmes_benchmark.report import build_report
    from holmes_benchmark.runner import Batch, RequestResult

    original = "2026-12-31T22:15:00+00:00"
    result = RequestResult(1, "A", started_at=original, finished_at=original,
                           raw_events=[{"received_at": original, "data": {"server_time": original}}])
    report = build_report(Config("http://host/api/chat", ("A",)), Batch([result], original, original, 1))
    converted = "2027-01-01T01:15:00.000+03:00"
    assert report["started_at"] == report["finished_at"] == converted
    assert report["requests"][0]["started_at"] == converted
    assert report["requests"][0]["finished_at"] == converted
    assert report["requests"][0]["raw_events"][0]["received_at"] == converted
    assert report["requests"][0]["raw_events"][0]["data"]["server_time"] == original
    assert result.started_at == original
