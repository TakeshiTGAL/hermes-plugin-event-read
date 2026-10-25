"""Five argument shapes a caller may send, and how each one is accepted or refused."""
import json

from test_event_read import Sink, _call, _deps, _ics, _multi


def _deps_for(tmp_path):
    sink = Sink(body=_multi([_ics("meet-1")]))
    return sink, _deps(tmp_path, sink, cron=False)


def test_slash_dot_and_hyphen_dates_are_sent_compact(tmp_path):
    sink, deps = _deps_for(tmp_path)
    pairs = (
        ("2026/10/09T00:00:00Z", "2026/10/09T03:00:00Z"),
        ("2026.10.09T00:00:00Z", "2026.10.09T03:00:00Z"),
        ("2026-10-09T00:00:00Z", "2026-10-09 03:00:00Z"),
    )
    for start, end in pairs:
        sink.calls.clear()
        body = _call(deps, args={"start": start, "end": end})
        sent = sink.calls[0][3].decode("utf-8")
        assert body["ok"] is True
        assert 'start="20261009T000000Z"' in sent
        assert 'end="20261009T030000Z"' in sent


def test_fullwidth_and_lower_case_stamps_are_sent_compact(tmp_path):
    sink, deps = _deps_for(tmp_path)
    body = _call(deps, args={
        "start": "２０２６１００９Ｔ００００００Ｚ",
        "end": "２０２６１００９Ｔ０３００００Ｚ",
    })
    sent = sink.calls[-1][3].decode("utf-8")
    assert body["ok"] is True
    assert 'start="20261009T000000Z"' in sent
    sink.calls.clear()
    body = _call(deps, args={"start": "20261009t000000z", "end": "20261009t030000z"})
    sent = sink.calls[-1][3].decode("utf-8")
    assert body["ok"] is True
    assert 'end="20261009T030000Z"' in sent


def test_extra_argument_names_the_fix_without_the_key(tmp_path):
    sink, deps = _deps_for(tmp_path)
    body = _call(deps, args={"deliver": "local"})
    text = json.dumps(body)
    assert body["ok"] is False
    assert body["error_type"] == "bad_args"
    assert "20261009T000000Z" in body["message"]
    assert "deliver" not in text
    assert "local" not in text
    assert sink.calls == []


def test_offset_is_refused_without_echoing_it(tmp_path):
    sink, deps = _deps_for(tmp_path)
    body = _call(deps, args={
        "start": "2026-10-09T03:00:00+0900",
        "end": "2026-10-09T04:00:00Z",
    })
    text = json.dumps(body)
    assert body["ok"] is False
    assert body["error_type"] == "bad_args"
    assert "20261009T000000Z" in body["message"]
    assert "+0900" not in text
    assert "03:00:00" not in text
    assert sink.calls == []


def test_impossible_day_is_not_rolled_forward(tmp_path):
    sink, deps = _deps_for(tmp_path)
    body = _call(deps, args={
        "start": "2026/02/31T00:00:00Z",
        "end": "2026/03/01T00:00:00Z",
    })
    text = json.dumps(body)
    assert body["ok"] is False
    assert body["error_type"] == "bad_args"
    assert "20261009T000000Z" in body["message"]
    assert "02/31" not in text
    assert "20260231" not in text
    assert "20260303" not in text
    assert sink.calls == []


def test_date_without_time_or_z_is_refused(tmp_path):
    sink, deps = _deps_for(tmp_path)
    for start, end in (
        ("2026/10/09", "2026/10/10"),
        ("2026-10-09T00:00:00", "2026-10-09T03:00:00"),
        ("2026-10-09T00:00Z", "2026-10-09T03:00Z"),
    ):
        body = _call(deps, args={"start": start, "end": end})
        assert body["ok"] is False
        assert body["error_type"] == "bad_args"
    assert sink.calls == []
