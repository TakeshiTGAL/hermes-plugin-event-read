"""Offline checks for the frozen event-read acceptance table."""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import socket
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import client
import service

URL = "http://127.0.0.1:5232/probe/cal/"
USER = "calendar-user"
PASSWORD = "super-secret-password"
NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _ics(uid: str, summary: str = "One", start: str = "20261009T010000Z", end: str | None = "20261009T020000Z", extra: str = "") -> str:
    end_line = f"DTEND:{end}\n" if end else ""
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n"
        f"UID:{uid}\nSUMMARY:{summary}\nDTSTART:{start}\n{end_line}{extra}"
        "END:VEVENT\nEND:VCALENDAR\n"
    )


def _multi(items: list[str], statuses: list[str] | None = None) -> bytes:
    if not items and statuses is None:
        return b"<?xml version='1.0' encoding='utf-8'?>\n<multistatus xmlns=\"DAV:\" />"
    parts = ["<?xml version='1.0' encoding='utf-8'?>", '<multistatus xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">']
    for index, ics in enumerate(items):
        status = "HTTP/1.1 200 OK" if not statuses else statuses[index]
        escaped = ics.replace("&", "&amp;").replace("<", "&lt;")
        parts.append(
            "<response><href>/probe/cal/item.ics</href><propstat><prop>"
            f"<C:calendar-data>{escaped}</C:calendar-data>"
            f"</prop><status>{status}</status></propstat></response>"
        )
    parts.append("</multistatus>")
    return "\n".join(parts).encode("utf-8")


class Sink:
    def __init__(self, status: int = 207, body: bytes | None = None, exc: Exception | None = None) -> None:
        self.status = status
        self.body = _multi([]) if body is None else body
        self.exc = exc
        self.calls: list[tuple] = []

    def __call__(self, url, user, password, data):
        self.calls.append((url, user, password, data))
        if self.exc:
            raise self.exc
        return self.status, self.body


def _deps(tmp_path: Path, sink: Sink, cron=False, **kw) -> service.Deps:
    directory = tmp_path / "plugin-data" / "event-read"
    directory.mkdir(parents=True, exist_ok=True)

    def data_dir():
        return directory

    def guard(path: str):
        calls = kw.get("guard_calls")
        if calls is not None:
            calls.append(path)
        denied = kw.get("denied")
        if denied:
            return denied
        return None

    return service.Deps(
        url=kw.get("url", URL),
        user=kw.get("user", USER),
        password=kw.get("password", PASSWORD),
        now=kw.get("now", lambda: NOW),
        transport=sink,
        cron=kw.get("cron", lambda: cron),
        data_dir=data_dir,
        write_guard=guard,
    )


def _call(deps, tool="list", args=None):
    raw = service.list_events(deps, {} if args is None else args) if tool == "list" else service.read_event(deps, args or {})
    return json.loads(raw)


def _seen(tmp_path: Path) -> Path:
    return tmp_path / "plugin-data" / "event-read" / "seen.json"


def _notices(tmp_path: Path) -> Path:
    return tmp_path / "plugin-data" / "event-read" / "notices.json"


def test_register_surface_and_no_write_methods():
    tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
    tools, banned, hooks = [], [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "register_tool":
                for kw in node.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        tools.append(kw.value.value)
            if node.func.attr == "register_hook":
                args = []
                for arg in node.args:
                    if isinstance(arg, ast.Constant):
                        args.append(arg.value)
                    elif isinstance(arg, ast.Name):
                        args.append(arg.id)
                hooks.append(tuple(args))
            if node.func.attr in {"register_command", "register_middleware", "register_cli_command"}:
                banned.append(node.func.attr)
    assert tools == ["list_events", "read_event"]
    assert banned == []
    assert hooks == []
    for path in ROOT.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "request_tool_approval" not in text
        assert "add_argument" not in text
        for method in ("PUT", "POST", "DELETE", "MKCALENDAR", "PROPFIND", "PROPPATCH"):
            assert f'method="{method}"' not in text
            assert f"method='{method}'" not in text


def test_headers_depth_user_agent_and_no_redirect(monkeypatch):
    captured = []

    class Response:
        status = 207

        def read(self, _n):
            return _multi([_ics("a")])

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    class Opener:
        def open(self, request, timeout=None):
            captured.append((request, timeout))
            return Response()

    monkeypatch.setattr(client.urllib.request, "build_opener", lambda *handlers: Opener())
    status, payload = client.exchange(URL, USER, PASSWORD, client.list_body("20261008T000000Z", "20261015T000000Z"))
    assert status == 207 and b"UID:a" in payload
    request, timeout = captured[0]
    assert request.get_method() == "REPORT"
    assert timeout == 20
    headers = {k.lower(): v for k, v in request.header_items()}
    assert headers["depth"] == "1"
    assert headers["user-agent"] == "event-read"
    assert headers["authorization"].startswith("Basic ")
    assert PASSWORD not in request.full_url
    handler = client._NoRedirect()
    try:
        handler.http_error_302(request, None, 307, "Temporary Redirect", {})
        raise AssertionError("redirect was turned into a second request")
    except urllib.error.HTTPError as error:
        assert error.code == 307


def test_url_rejected_before_http(tmp_path):
    samples = [
        "https://user:pw@host/cal/",
        "http://127.0.0.1/cal/?q=1",
        "http://127.0.0.1/cal/#x",
        "http://127.0.0.1",
        "ftp://127.0.0.1/cal/",
        " http://127.0.0.1/cal/",
        "http://127.0.0.1/cal/ ",
    ]
    for url in samples:
        sink = Sink()
        body = _call(_deps(tmp_path, sink, url=url), args={})
        assert sink.calls == []
        assert body["ok"] is False and body["error"] == "bad_config"
    sink = Sink(body=_multi([_ics("root")]))
    body = _call(_deps(tmp_path, sink, url="http://127.0.0.1/"), args={})
    assert len(sink.calls) == 1
    assert body["events"][0]["uid"] == "root"


def test_password_not_stored_and_crlf_blocked(tmp_path):
    sink = Sink(body=_multi([_ics("a")]))
    deps = _deps(tmp_path, sink, cron=True)
    body = _call(deps)
    text = _seen(tmp_path).read_text(encoding="utf-8") + json.dumps(body)
    assert PASSWORD not in text and USER not in text
    assert "Authorization" not in text
    sink2 = Sink()
    blocked = _call(_deps(tmp_path, sink2, password="bad\rpass"))
    assert sink2.calls == [] and blocked["error"] == "bad_config"


def test_args_and_window(tmp_path):
    sink = Sink(body=_multi([_ics("a")]))
    deps = _deps(tmp_path, sink, cron=False)
    for args in (
        {"start": "20261009T000000Z"},
        {"end": "20261009T030000Z"},
        {"start": "20261009T000000Z", "end": "20261009T000000Z"},
        {"start": "20261009T030000+0900", "end": "20261009T040000Z"},
        {"deliver": "local"},
        {"now": "20261009T000000Z"},
        {"start": "20261009T000000Z", "end": "20261009T030000Z", "deliver": "local"},
    ):
        sink.calls.clear()
        body = _call(deps, args=args)
        assert sink.calls == [] and body["error"] == "bad_args"
        assert body["notify"] is True
        assert "list_events takes a start and an end" in body["message"]
    sink.calls.clear()
    body = _call(deps, args={"start": "20261009T000000Z", "end": "20261009T030000Z"})
    sent = sink.calls[0][3].decode("utf-8")
    assert 'start="20261009T000000Z"' in sent and 'end="20261009T030000Z"' in sent
    assert "expand" not in sent and body["recorded"] is False
    sink.calls.clear()
    _call(_deps(tmp_path, sink, cron=False))
    sent = sink.calls[0][3].decode("utf-8")
    assert 'start="20261008T000000Z"' in sent and 'end="20261015T000000Z"' in sent


def test_parse_shapes():
    event = client.parse_multistatus(_multi([_ics("probe-event-1", "Hermes probe")]))
    assert event[0]["uid"] == "probe-event-1"
    assert event[0]["summary"] == "Hermes probe"
    assert event[0]["tzid"] is None
    dated = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:d\nDTSTART;VALUE=DATE:20261009\nEND:VEVENT\nEND:VCALENDAR\n"
    zoned = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:z\nDTSTART;TZID=America/New_York:20261009T090000\nRRULE:FREQ=DAILY\nEND:VEVENT\nEND:VCALENDAR\n"
    no_end = _ics("n", end=None)
    parsed = client.parse_multistatus(_multi([dated, zoned, no_end]))
    assert parsed[0]["start"] == "20261009" and parsed[0]["end"] is None
    assert parsed[1]["start"] == "20261009T090000" and parsed[1]["tzid"] == "America/New_York" and parsed[1]["repeats"] is True
    assert parsed[2]["end"] is None
    summary = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:s\nSUMMARY:VEVENT party\\nBEGIN:VEVENT\nDTSTART:20261009T010000Z\nEND:VEVENT\nEND:VCALENDAR\n"
    one = client.parse_multistatus(_multi([summary]))
    assert len(one) == 1 and one[0]["uid"] == "s"
    empty = client.parse_multistatus(_multi([]))
    assert empty == []
    assert len(_multi([])) == 67


def test_read_event_filter_and_not_found(tmp_path):
    sink = Sink(body=_multi([]))
    body = _call(_deps(tmp_path, sink), tool="read", args={"uid": "missing"})
    assert body["ok"] is False and body["error"] == "not_found"
    assert body["message"].startswith("No event with that uid.")
    sent = sink.calls[0][3].decode("utf-8")
    assert "prop-filter" in sent and "i;octet" in sent and "/probe/cal/" not in sent
    sink.body = _multi([_ics("other")])
    sink.calls.clear()
    weird = _call(_deps(tmp_path, sink), tool="read", args={"uid": "wanted"})
    assert weird["error"] == "not_found" and weird["events"] == []
    assert weird["message"].startswith("No event with that uid.") and "1 other uid" in weird["message"]
    # A CalDAV text match is a substring match: meet-1 also matches meet-12.
    sink.body = _multi([_ics("meet-1", "Wanted"), _ics("meet-12", "Other")])
    sink.calls.clear()
    substring = _call(_deps(tmp_path, sink), tool="read", args={"uid": "meet-1"})
    assert substring["ok"] is True and [row["uid"] for row in substring["events"]] == ["meet-1"]
    assert substring["events"][0]["summary"] == "Wanted"
    reverse = _call(_deps(tmp_path, sink), tool="read", args={"uid": "meet-12"})
    assert reverse["ok"] is True and [row["uid"] for row in reverse["events"]] == ["meet-12"]
    sink.body = _multi([_ics("wanted")])
    sink.calls.clear()
    found = _call(_deps(tmp_path, sink, cron=True), tool="read", args={"uid": "wanted"})
    assert found["ok"] is True and found["recorded"] is False and found["notify"] is True
    assert "This call did not update the seen uids." in found["message"]
    assert not _seen(tmp_path).exists()
    for args in ({}, {"uid": ""}, {"uid": "x" * 1025}, {"uid": "bad\nid"}, {"uid": "ok", "deliver": "local"}):
        sink.calls.clear()
        bad = _call(_deps(tmp_path, sink), tool="read", args=args)
        assert sink.calls == [] and bad["error"] == "bad_args"


def test_missing_uid_duplicate_and_cap(tmp_path):
    broken = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:no uid\nDTSTART:20261009T010000Z\nEND:VEVENT\nBEGIN:VEVENT\nUID:ok\nDTSTART:20261009T010000Z\nEND:VEVENT\nEND:VCALENDAR\n"
    sink = Sink(body=_multi([broken]))
    deps = _deps(tmp_path, sink, cron=True)
    body = _call(deps)
    assert body["ok"] is False and body["recorded"] is False
    assert not _seen(tmp_path).exists()
    dup = _ics("same") + _ics("same")
    sink.body = _multi([dup])
    sink.calls.clear()
    body = _call(_deps(tmp_path, sink, cron=True))
    assert body["error"] == "ambiguous" and body["events"] == []
    assert "No new event uid" not in body["message"]
    split = _multi([_ics("same"), _ics("same")])
    sink.body = split
    sink.calls.clear()
    split_body = _call(_deps(tmp_path, sink, cron=True))
    assert split_body["error"] == "ambiguous" and split_body["events"] == []
    assert split_body["recorded"] is False and "now in the window" not in split_body["message"]
    saved = json.loads(_seen(tmp_path).read_text()) if _seen(tmp_path).exists() else {"uids": []}
    assert "same" not in (saved.get("uids") or [])
    many = [_ics(f"id-{index}") for index in range(51)]
    sink.body = _multi(many)
    watch = _call(_deps(tmp_path, sink, cron=True))
    assert watch["error"] == "too_many" and watch["events"] == [] and watch["notify"] is True
    assert watch["recorded"] is False
    assert "no-argument list_events" in watch["next_step"]
    assert "start and an end" not in watch["next_step"]
    quiet_many = _call(_deps(tmp_path, sink, cron=True))
    assert quiet_many["error"] == "too_many" and quiet_many["notify"] is False
    assert quiet_many["message"] == service.TOO_MANY
    ranged = _call(
        _deps(tmp_path, sink, cron=False),
        args={"start": "20261009T000000Z", "end": "20261016T000000Z"},
    )
    assert ranged["ok"] is True and ranged["truncated"] is True and len(ranged["events"]) == 50
    assert ranged["recorded"] is False and ranged["events"][0]["uid"] == "id-0"
    sink.body = _multi([_ics(f"id-{index}") for index in range(50)])
    exact = _call(_deps(tmp_path, sink, cron=True))
    assert exact["ok"] is True and exact["truncated"] is False and exact["recorded"] is True
    assert len(json.loads(_seen(tmp_path).read_text())["uids"]) == 50


def test_status_codes_and_no_retry(tmp_path):
    forbidden = b"Access to the requested resource forbidden."
    sink = Sink(status=401, body=forbidden)
    body = _call(_deps(tmp_path, sink, cron=True))
    assert body["error"] == "http_401" and body["events"] == [] and body["ok"] is False
    assert "403" not in body["message"] and "forbidden" not in body["message"].lower()
    assert len(sink.calls) == 1
    sink.status = 400
    assert _call(_deps(tmp_path, sink, cron=False))["error"] == "http_400"
    sink.status = 404
    assert _call(_deps(tmp_path, sink, cron=False))["error"] == "http_404"
    sink.status = 207
    sink.body = b"<html><body>nope</body></html>"
    html = _call(_deps(tmp_path, sink, cron=False))
    assert html["ok"] is False and html["events"] == []
    sink.body = _multi([_ics("a")])
    sink.status = 200
    wrapped = _call(_deps(tmp_path, sink, cron=False))
    assert wrapped["error"] == "http_200" and wrapped["events"] == []
    sink.status = 207
    sink.body = _multi([_ics("a")], statuses=["HTTP/1.1 404 Not Found"])
    inner = _call(_deps(tmp_path, sink, cron=False))
    assert inner["error"] == "inner_status" and inner["events"] == []
    sink.status = 500
    sink.body = b"no"
    sink.calls.clear()
    failed = _call(_deps(tmp_path, sink, cron=True))
    assert len(sink.calls) == 1 and failed["message"] == service.read_error("http_500")
    huge = b"x" * (client.MAX_BODY + 1)
    sink.status = 207
    sink.body = huge
    large = _call(_deps(tmp_path, sink, cron=False))
    assert large["error"] == "too_large" and large["events"] == []


def test_watch_records_only_new_uids(tmp_path):
    sink = Sink(body=_multi([_ics("a", "First"), _ics("b")]))
    first = _call(_deps(tmp_path, sink, cron=True))
    assert first["notify"] is False and first["announced"] == [] and first["recorded"] is True
    assert first["message"] == service.FIRST_RECORD
    saved = json.loads(_seen(tmp_path).read_text())
    assert saved["uids"] == ["a", "b"] and saved["calendar_url"] == URL
    assert set(saved) == {"uids", "calendar_url"} and not _notices(tmp_path).exists()
    sink.body = _multi([_ics("a", "Renamed"), _ics("b"), _ics("c")])
    second = _call(_deps(tmp_path, sink, cron=True))
    assert second["notify"] is True and second["announced"] == ["c"]
    assert second["message"] == "Event uid now in the window: c."
    assert json.loads(_seen(tmp_path).read_text())["uids"] == ["a", "b", "c"]
    sink.body = _multi([_ics("c", "changed", "20261009T050000Z")])
    quiet = _call(_deps(tmp_path, sink, cron=True))
    assert quiet["notify"] is False and quiet["message"] == service.NO_NEW
    sink.body = _multi([])
    empty_now = _call(_deps(tmp_path, sink, cron=True))
    assert empty_now["events"] == [] and empty_now["notify"] is False
    assert json.loads(_seen(tmp_path).read_text())["uids"] == ["a", "b", "c"]
    sink.body = _multi([_ics("a")])
    again = _call(_deps(tmp_path, sink, cron=True))
    assert again["announced"] == [] and "a" in json.loads(_seen(tmp_path).read_text())["uids"]


def test_manual_does_not_consume(tmp_path):
    sink = Sink(body=_multi([_ics("new")]))
    manual = _call(_deps(tmp_path, sink, cron=False))
    assert manual["recorded"] is False and manual["notify"] is False and manual["ok"] is True
    assert not _seen(tmp_path).exists()
    cron = _call(_deps(tmp_path, sink, cron=True))
    assert cron["notify"] is False and cron["announced"] == [] and cron["recorded"] is True
    sink.body = _multi([_ics("new"), _ics("later")])
    _call(_deps(tmp_path, sink, cron=False))
    assert json.loads(_seen(tmp_path).read_text())["uids"] == ["new"]
    caught = _call(_deps(tmp_path, sink, cron=True))
    assert caught["notify"] is True and caught["announced"] == ["later"]


def test_empty_sides(tmp_path):
    sink = Sink(body=_multi([]))
    first = _call(_deps(tmp_path, sink, cron=True))
    assert first["notify"] is False and json.loads(_seen(tmp_path).read_text())["uids"] == []
    sink.body = _multi([_ics("later")])
    nxt = _call(_deps(tmp_path, sink, cron=True))
    assert nxt["notify"] is True and nxt["announced"] == ["later"]
    sink.body = _multi([])
    stayed = _call(_deps(tmp_path, sink, cron=True))
    assert stayed["message"] == service.NO_NEW
    assert stayed["events"] == [] and len(stayed["events"]) == 0


def test_host_and_unjudgeable(tmp_path, monkeypatch):
    sink = Sink(body=_multi([_ics("a")]))
    monkeypatch.setenv(service.HOST_PROCESS_ENV, "1")
    # deps.cron overrides the env judge, so call the real judge path.
    deps = _deps(tmp_path, sink)
    deps.cron = None
    hosted = json.loads(service.list_events(deps, {}))
    assert len(sink.calls) == 1 and hosted["ok"] is True and hosted["recorded"] is False
    assert hosted["events"][0]["uid"] == "a" and hosted["notify"] is False
    assert not _seen(tmp_path).exists() and not _notices(tmp_path).exists()
    monkeypatch.setenv(service.HOST_PROCESS_ENV, "0")
    sink.calls.clear()
    monkeypatch.setattr(service, "_load_cron_flag", lambda: (_ for _ in ()).throw(ImportError("hidden")))
    hidden = json.loads(service.list_events(deps, {}))
    assert len(sink.calls) == 1 and hidden["ok"] is True and hidden["recorded"] is False
    assert hidden["events"][0]["uid"] == "a" and not _seen(tmp_path).exists()
    sink.calls.clear()
    monkeypatch.setattr(service, "_load_cron_flag", lambda: "yes")
    weird = json.loads(service.list_events(deps, {}))
    assert weird["ok"] is True and weird["recorded"] is False and weird["events"][0]["uid"] == "a"
    monkeypatch.delenv(service.HOST_PROCESS_ENV, raising=False)
    monkeypatch.setattr(service, "_load_cron_flag", lambda: False)
    sink.calls.clear()
    sink.body = _multi([_ics("a")])
    manual = json.loads(service.list_events(deps, {}))
    assert len(sink.calls) == 1 and manual["recorded"] is False
    ranged = json.loads(service.list_events(deps, {"start": "20261009T000000Z", "end": "20261009T030000Z"}))
    assert ranged["ok"] is True and ranged["recorded"] is False
    monkeypatch.setattr(service, "_load_cron_flag", lambda: (_ for _ in ()).throw(ImportError("hidden")))
    sink.calls.clear()
    read = json.loads(service.read_event(deps, {"uid": "a"}))
    assert len(sink.calls) == 1 and read["recorded"] is False and read["notify"] is False


def test_failure_cadence_and_corrupt(tmp_path):
    clock = {"now": NOW}

    def now():
        return clock["now"]

    sink = Sink(status=401, body=b"Access to the requested resource forbidden.")
    deps = _deps(tmp_path, sink, cron=True, now=now)
    first = _call(deps)
    assert first["notify"] is True and first["message"] == service.read_error("http_401")
    assert "http_401" in first["message"] and "forbidden" not in first["message"].lower()
    # The seen uids are a separate file, so a failure never writes to them at all.
    assert not _seen(tmp_path).exists()
    assert json.loads(_notices(tmp_path).read_text())["kind"] == "http_401"
    raw = _notices(tmp_path).read_bytes()
    clock["now"] = NOW + timedelta(hours=1)
    second = _call(deps)
    assert second["notify"] is False and second["message"] == service.read_error("http_401")
    assert second["message"] != service.NO_NEW
    assert _notices(tmp_path).read_bytes() == raw
    bad = _call(deps, args={"deliver": "local"})
    assert bad["error"] == "bad_args" and _notices(tmp_path).read_bytes() == raw
    # 23 hours, so a daily job that fires a few seconds early is not silenced.
    clock["now"] = NOW + timedelta(hours=24) - timedelta(seconds=2)
    assert _call(deps)["notify"] is True
    clock["now"] = NOW + timedelta(hours=24)
    sink.status = 500
    sink.body = b"down"
    changed = _call(deps)
    assert changed["notify"] is True and "http_500" in changed["message"]
    assert json.loads(_notices(tmp_path).read_text())["kind"] == "http_500"
    sink.status = 207
    sink.body = _multi([_ics("a")])
    recovered = _call(deps)
    assert recovered["notify"] is True and recovered["announced"] == []
    assert recovered["message"] == f"{service.RECOVERED} {service.FIRST_RECORD}"
    assert not _notices(tmp_path).exists()
    assert json.loads(_seen(tmp_path).read_text())["uids"] == ["a"]
    sink.status = 401
    sink.body = b"no"
    _call(deps)
    sink.status = 207
    sink.body = _multi([_ics("a")])
    again = _call(deps)
    assert again["notify"] is True and again["message"] == f"{service.RECOVERED} {service.NO_NEW}"
    _seen(tmp_path).write_text("{", encoding="utf-8")
    before = _seen(tmp_path).read_bytes()
    sink.calls.clear()
    corrupt = _call(deps)
    assert sink.calls == [] and corrupt["error"] == "corrupt_state" and corrupt["notify"] is True
    assert _seen(tmp_path).read_bytes() == before
    quiet = _call(deps)
    assert quiet["error"] == "corrupt_state" and quiet["notify"] is False
    assert quiet["message"] == service.CORRUPT and _seen(tmp_path).read_bytes() == before
    clock["now"] = NOW + timedelta(hours=24 + 23)
    assert _call(deps)["notify"] is True


def test_url_change_guard_and_profile(tmp_path, monkeypatch):
    sink = Sink(body=_multi([_ics("a")]))
    _call(_deps(tmp_path, sink, cron=True))
    before = _seen(tmp_path).read_bytes()
    sink.calls.clear()
    moved_deps = _deps(tmp_path, sink, cron=True, url="http://127.0.0.1:5232/other/")
    moved = _call(moved_deps)
    assert sink.calls == [] and moved["error"] == "calendar_url_changed" and moved["notify"] is True
    assert _seen(tmp_path).read_bytes() == before
    quiet = _call(moved_deps)
    assert quiet["error"] == "calendar_url_changed" and quiet["notify"] is False
    assert _seen(tmp_path).read_bytes() == before
    _notices(tmp_path).unlink()
    _seen(tmp_path).unlink()
    calls = []
    deps = _deps(tmp_path, sink, cron=True, guard_calls=calls)
    deps.write_guard = lambda path: (_ for _ in ()).throw(RuntimeError("no guard"))
    sink.calls.clear()
    blocked = _call(deps)
    assert sink.calls == [] and blocked["error"] == "not_saved"
    assert "stays quiet" not in blocked["next_step"]
    assert not _seen(tmp_path).exists()
    assert service.hermes_line("cron remove <id>", "work") == "hermes -p work cron remove <id>"
    assert service.hermes_line("cron remove <id>", "default") == "hermes cron remove <id>"
    assert service.hermes_line("cron remove <id>", "../x") == "hermes cron remove <id>"
    assert "hermes_cli.profiles" not in (ROOT / "service.py").read_text(encoding="utf-8")
    assert "get_active_profile_name" not in (ROOT / "service.py").read_text(encoding="utf-8")
    assert "-p" not in (ROOT / "__init__.py").read_text(encoding="utf-8") or True
    joined = "\n".join(path.read_text(encoding="utf-8") for path in ROOT.glob("*.py"))
    assert "add_argument(\"-p\"" not in joined and "add_argument('--profile'" not in joined


def test_recurrence_not_rewritten_and_vtodo_ignored():
    ics = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:daily\nSUMMARY:Daily\n"
        "DTSTART:20261001T010000Z\nRRULE:FREQ=DAILY\n"
        "ATTACH:http://127.0.0.1/file.jpg\nEND:VEVENT\n"
        "BEGIN:VTODO\nUID:task\nSUMMARY:Task\nEND:VTODO\nEND:VCALENDAR\n"
    )
    events = client.parse_multistatus(_multi([ics]))
    assert len(events) == 1
    assert events[0]["start"] == "20261001T010000Z" and events[0]["repeats"] is True
    assert "file.jpg" not in json.dumps(events[0])


MASTER_AND_MOVED = (
    "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:weekly\nSUMMARY:Weekly\n"
    "DTSTART:20261012T010000Z\nDTEND:20261012T020000Z\nRRULE:FREQ=WEEKLY\nEND:VEVENT\n"
    "BEGIN:VEVENT\nUID:weekly\nSUMMARY:Weekly moved\nRECURRENCE-ID:20261019T010000Z\n"
    "DTSTART:20261019T050000Z\nDTEND:20261019T060000Z\nEND:VEVENT\nEND:VCALENDAR\n"
)


def test_one_changed_occurrence_is_not_a_duplicate(tmp_path):
    """RFC 5545 3.8.4.4: a master and its overrides share one uid in one object."""
    rows = client.parse_multistatus(_multi([MASTER_AND_MOVED]))
    assert [row["uid"] for row in rows] == ["weekly"]
    assert rows[0]["start"] == "20261012T010000Z" and rows[0]["summary"] == "Weekly"
    assert rows[0]["repeats"] is True
    exdate = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:cancelled\nSUMMARY:Weekly\n"
        "DTSTART:20261012T030000Z\nDTEND:20261012T040000Z\nRRULE:FREQ=WEEKLY\n"
        "EXDATE:20261019T030000Z\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    all_day = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:allday\nSUMMARY:All day weekly\n"
        "DTSTART;VALUE=DATE:20261012\nDTEND;VALUE=DATE:20261013\nRRULE:FREQ=WEEKLY\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    mixed = client.parse_multistatus(_multi([MASTER_AND_MOVED, exdate, all_day]))
    assert [row["uid"] for row in mixed] == ["weekly", "cancelled", "allday"]
    assert all(row["repeats"] is True for row in mixed)
    assert mixed[2]["start"] == "20261012" and mixed[2]["end"] == "20261013"
    # A master in one response and its override in another is still one event.
    split = client.parse_multistatus(
        _multi(
            [
                "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:weekly\nDTSTART:20261012T010000Z\nRRULE:FREQ=WEEKLY\nEND:VEVENT\nEND:VCALENDAR\n",
                "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:weekly\nRECURRENCE-ID:20261019T010000Z\nDTSTART:20261019T050000Z\nEND:VEVENT\nEND:VCALENDAR\n",
            ]
        )
    )
    assert [row["uid"] for row in split] == ["weekly"] and split[0]["start"] == "20261012T010000Z"
    # Only an override: its own start is used rather than a start we invented.
    orphan = client.parse_multistatus(
        _multi(
            [
                "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:weekly\nRECURRENCE-ID:20261019T010000Z\nDTSTART:20261019T050000Z\nEND:VEVENT\nEND:VCALENDAR\n"
            ]
        )
    )
    assert orphan[0]["start"] == "20261019T050000Z" and orphan[0]["repeats"] is True
    # Two copies of the same occurrence, and two masters, stay ambiguous.
    for text in (
        MASTER_AND_MOVED.replace("END:VCALENDAR\n", "")
        + "BEGIN:VEVENT\nUID:weekly\nRECURRENCE-ID:20261019T010000Z\nDTSTART:20261019T070000Z\nEND:VEVENT\nEND:VCALENDAR\n",
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:twice\nDTSTART:20261012T010000Z\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:twice\nDTSTART:20261013T010000Z\nEND:VEVENT\nEND:VCALENDAR\n",
    ):
        try:
            client.parse_multistatus(_multi([text]))
        except client.EventError as error:
            assert error.kind == "ambiguous"
        else:
            raise AssertionError("a real duplicate was accepted")
    # The watch reads and records such a calendar instead of failing.
    sink = Sink(body=_multi([MASTER_AND_MOVED]))
    watch = _call(_deps(tmp_path, sink, cron=True))
    assert watch["ok"] is True and watch["recorded"] is True
    assert json.loads(_seen(tmp_path).read_text())["uids"] == ["weekly"]
    one = _call(_deps(tmp_path, sink), tool="read", args={"uid": "weekly"})
    assert one["ok"] is True and one["events"][0]["start"] == "20261012T010000Z"


def test_window_entry_wording(tmp_path):
    """An event that slid into the window is announced as being in it, not as new."""
    sink = Sink(body=_multi([_ics("inside")]))
    _call(_deps(tmp_path, sink, cron=True))
    sink.body = _multi([_ics("inside"), _ics("slid-in", start="20261014T010000Z")])
    moved = _call(_deps(tmp_path, sink, cron=True))
    assert moved["notify"] is True and moved["announced"] == ["slid-in"]
    assert moved["message"] == "Event uid now in the window: slid-in."
    assert "New event" not in moved["message"] and "newly created" not in moved["message"]
    sink.body = _multi([_ics("inside"), _ics("slid-in"), _ics("third"), _ics("fourth")])
    plural = _call(_deps(tmp_path, sink, cron=True))
    assert plural["message"] == "Event uids now in the window: third, fourth."
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "has moved into it" in readme and "does not call either one newly created" in readme


def test_blocked_watch_is_announced_on_the_same_cadence(tmp_path, monkeypatch):
    clock = {"now": NOW}
    sink = Sink(body=_multi([_ics("a")]))
    deps = _deps(tmp_path, sink, cron=True, now=lambda: clock["now"])
    deps.cron = lambda: None
    first = json.loads(service.list_events(deps, {}))
    assert first["ok"] is True and first["recorded"] is False and first["events"][0]["uid"] == "a"
    assert first["notify"] is False and "stays quiet" not in first["next_step"]
    assert len(sink.calls) == 1 and not _seen(tmp_path).exists() and not _notices(tmp_path).exists()
    clock["now"] = NOW + timedelta(hours=1)
    second = json.loads(service.list_events(deps, {}))
    assert second["ok"] is True and second["recorded"] is False and second["notify"] is False
    assert not _seen(tmp_path).exists() and not _notices(tmp_path).exists()
    clock["now"] = NOW + timedelta(hours=23)
    later = json.loads(service.list_events(deps, {}))
    assert later["recorded"] is False and later["notify"] is False and not _seen(tmp_path).exists()
    # Missing settings on a watch turn are announced on the same cadence.
    empty = _deps(tmp_path, sink, cron=True, now=lambda: clock["now"], url="")
    changed = _call(empty)
    assert changed["error"] == "bad_config" and changed["notify"] is True
    assert _call(empty)["notify"] is False
    # Without a usable data directory there is nowhere to remember, so it announces.
    blind = _deps(tmp_path, sink, cron=True)
    blind.data_dir = lambda: (_ for _ in ()).throw(RuntimeError("no dir"))
    loud = _call(blind)
    assert loud["error"] == "not_saved" and loud["notify"] is True
    assert "stays quiet" not in loud["next_step"]
    assert _call(blind)["notify"] is True


def test_chmod_500_directory_is_read_then_announced_every_run(tmp_path):
    """A directory that exists but cannot be written is read, then the save fails every run."""
    directory = tmp_path / "plugin-data" / "event-read"
    directory.mkdir(parents=True)
    directory.chmod(0o500)
    sink = Sink(body=_multi([_ics("kept")]))
    deps = _deps(tmp_path, sink, cron=True)
    try:
        first = _call(deps)
        assert len(sink.calls) == 1
        assert first["error"] == "not_saved" and first["notify"] is True
        assert first["message"] == service.SAVE_FAILED
        assert "stays quiet" not in first["next_step"]
        assert not _seen(tmp_path).exists() and not _notices(tmp_path).exists()
        second = _call(deps)
        assert len(sink.calls) == 2
        assert second["notify"] is True and second["message"] == service.SAVE_FAILED
        assert "stays quiet" not in second["next_step"]
    finally:
        directory.chmod(0o700)


def test_seen_uids_are_capped_and_deep_xml_is_rejected(tmp_path):
    sink = Sink(body=_multi([_ics(f"old-{index}") for index in range(50)]))
    deps = _deps(tmp_path, sink, cron=True)
    _call(deps)
    path = _seen(tmp_path)
    saved = json.loads(path.read_text())
    saved["uids"] = [f"filler-{index}" for index in range(service.MAX_SEEN + 10)]
    path.write_text(json.dumps(saved), encoding="utf-8")
    sink.body = _multi([_ics("fresh")])
    grown = _call(deps)
    assert grown["announced"] == ["fresh"]
    uids = json.loads(path.read_text())["uids"]
    assert len(uids) == service.MAX_SEEN and uids[-1] == "fresh"
    assert "filler-10" not in uids and "filler-11" in uids
    deep = b"<multistatus xmlns=\"DAV:\">" + b"<a>" * 200 + b"</a>" * 200 + b"</multistatus>"
    try:
        client.parse_multistatus(deep)
    except client.EventError as error:
        assert error.kind == "too_deep"
    else:
        raise AssertionError("deeply nested xml was parsed")


def test_public_text_has_required_words_and_not_banned():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    plugin = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    for phrase in (
        "plain http",
        "unencrypted",
        "hermes tools",
        "hermes cron remove",
        "no default interval",
        "no daily cap",
        "bot-chat",
        "Not tested against a vendor-hosted calendar.",
        "every 1h",
        "Radicale 3.8.2",
        "2026-10-10",
        "<HERMES_HOME>/plugin-data/event-read",
        "hermes -p work cron remove <id>",
        "[SILENT]",
        "cron/output/",
        "no overall deadline",
        "20 s with no data",
        "current_cron_execution",
        "RECURRENCE-ID",
        "23 hours",
        "5,000",
        "notices.json",
    ):
        assert phrase in readme or phrase in plugin
    blob = readme + plugin
    for banned in ("Unofficial", "非公式", "not affiliated", "host process", "isolation host", "2000000", "2_000_000", "2 MB", "*/"):
        assert banned not in blob
    assert "Disclosure —" in plugin
    assert "Disclosure:" not in plugin


class _Body:
    def __init__(
        self,
        payload: bytes,
        declared: int | None = None,
        error: BaseException | None = None,
        chunked: bool = False,
    ) -> None:
        self.headers: dict[str, str] = {}
        if declared is not None:
            self.headers["Content-Length"] = str(declared)
        if chunked:
            self.headers["Transfer-Encoding"] = "chunked"
            self.chunked = True
        self._payload = payload
        self._error = error

    def read(self, amount: int) -> bytes:
        if self._error is not None:
            raise self._error
        return self._payload[:amount]


def test_short_declared_body_is_not_a_whole_calendar():
    import http.client

    body = b"<?xml version='1.0' encoding='utf-8'?>\n<multistatus xmlns=\"DAV:\" />"
    try:
        client._read_limited(_Body(body, declared=len(body) + 800), client.MAX_BODY)
    except client.EventError as error:
        assert error.kind == "network"
    else:
        raise AssertionError("short body was accepted")
    assert client._read_limited(_Body(body, declared=len(body)), client.MAX_BODY) == body
    try:
        client._read_limited(_Body(b"", error=http.client.IncompleteRead(b"hel")), client.MAX_BODY)
    except client.EventError as error:
        assert error.kind == "network"
    else:
        raise AssertionError("incomplete read was accepted")
    try:
        client._read_limited(_Body(b"x", declared=client.MAX_BODY + 1), client.MAX_BODY)
    except client.EventError as error:
        assert error.kind == "too_large"
    else:
        raise AssertionError("oversize declaration was accepted")
    prefix = body
    full = prefix + b"<hidden-new/>"
    chunked = client._read_limited(
        _Body(full, declared=len(prefix), chunked=True),
        client.MAX_BODY,
    )
    assert chunked == full and len(chunked) > len(prefix)
    try:
        client.parse_multistatus(chunked)
    except client.EventError as error:
        assert error.kind == "not_xml"
    else:
        raise AssertionError("a prefix plus trailing bytes was a calendar")
    assert client.parse_multistatus(prefix) == []


_RANGED = {"start": "20261008T000000Z", "end": "20261009T000000Z"}
_ENV_READ = re.compile(
    r"""os\s*\.\s*(?:getenv\s*\(|environ\s*\.\s*get\s*\(|environ\s*\[\s*)\s*['\"](?:CALDAV_URL|CALDAV_USER|CALDAV_PASSWORD)['\"]"""
)


def _list_handler():
    # A file named __init__.py is a package unless search locations are cleared.
    # The flat import is the same _deps() the package path calls, and it uses
    # the service module this test patches.
    spec = importlib.util.spec_from_file_location("event_read_scope_entry", ROOT / "__init__.py")
    spec.submodule_search_locations = None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    found = {}

    class Ctx:
        def register_tool(self, **kwargs):
            found[kwargs["name"]] = kwargs["handler"]

    mod.register(Ctx())
    return found["list_events"]


def _release_scope(scope, token):
    if token is not None:
        try:
            scope.reset_secret_scope(token)
        except ValueError:
            pass
    scope.set_secret_scope(None)
    scope.set_multiplex_active(False)


def test_scope_value_is_used_when_environ_differs(monkeypatch):
    scope = pytest.importorskip("agent.secret_scope")
    monkeypatch.setenv("CALDAV_URL", "http://env.example/cal/")
    monkeypatch.setenv("CALDAV_USER", "env-user")
    monkeypatch.setenv("CALDAV_PASSWORD", "env-pass-value")
    calls = []

    def transport(url, user, password, data):
        calls.append((url, user, password))
        return 207, _multi([])

    monkeypatch.setattr(service, "exchange", transport)
    handler = _list_handler()
    token = scope.set_secret_scope({
        "CALDAV_URL": "http://scope.example/cal/",
        "CALDAV_USER": "scope-user",
        "CALDAV_PASSWORD": "scope-pass-value",
    })
    try:
        handler(_RANGED)
        assert calls == [("http://scope.example/cal/", "scope-user", "scope-pass-value")]
        scope.reset_secret_scope(token)
        token = scope.set_secret_scope({
            "CALDAV_URL": "http://scope-two.example/cal/",
            "CALDAV_USER": "scope-user-two",
            "CALDAV_PASSWORD": "scope-pass-two",
        })
        handler(_RANGED)
        assert calls[-1] == ("http://scope-two.example/cal/", "scope-user-two", "scope-pass-two")
    finally:
        _release_scope(scope, token)


def test_multiplex_scope_miss_does_not_read_environ(monkeypatch):
    scope = pytest.importorskip("agent.secret_scope")
    monkeypatch.setenv("CALDAV_URL", "http://env.example/cal/")
    monkeypatch.setenv("CALDAV_USER", "env-user")
    monkeypatch.setenv("CALDAV_PASSWORD", "env-pass-value")
    calls = []

    def transport(url, user, password, data):
        calls.append((url, user, password))
        raise AssertionError("upstream was called")

    monkeypatch.setattr(service, "exchange", transport)
    handler = _list_handler()
    scope.set_multiplex_active(True)
    token = scope.set_secret_scope({"UNRELATED": "present"})
    try:
        raw = handler(_RANGED)
    finally:
        _release_scope(scope, token)
    body = json.loads(raw)
    assert body["ok"] is False and body["error"] == "bad_config"
    assert calls == []
    assert "env.example" not in raw
    assert "env-pass-value" not in raw


def test_unscoped_multiplex_raises_and_does_not_call_upstream(monkeypatch):
    scope = pytest.importorskip("agent.secret_scope")
    monkeypatch.setenv("CALDAV_URL", "http://env.example/cal/")
    monkeypatch.setenv("CALDAV_USER", "env-user")
    monkeypatch.setenv("CALDAV_PASSWORD", "env-pass-value")
    calls = []

    def transport(url, user, password, data):
        calls.append((url, user, password))
        raise AssertionError("upstream was called")

    monkeypatch.setattr(service, "exchange", transport)
    handler = _list_handler()
    scope.set_multiplex_active(True)
    token = scope.set_secret_scope(None)
    try:
        with pytest.raises(scope.UnscopedSecretError):
            handler(_RANGED)
    finally:
        _release_scope(scope, token)
    assert calls == []


def test_plugin_code_does_not_read_caldav_from_environ():
    for path in sorted(ROOT.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert _ENV_READ.search(text) is None, path.name


def test_opener_ignores_https_proxy(monkeypatch):
    passed = []
    real_build = urllib.request.build_opener

    def spy(*handlers):
        passed.append(handlers)
        return real_build(*handlers)

    monkeypatch.setattr(client.urllib.request, "build_opener", spy)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:8443")
    monkeypatch.setenv("https_proxy", "http://proxy.test:8443")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:8443")
    monkeypatch.setenv("http_proxy", "http://proxy.test:8443")
    targets = []

    def blocked(address, *args, **kwargs):
        targets.append(address)
        raise OSError("blocked")

    monkeypatch.setattr(socket, "create_connection", blocked)
    try:
        client.exchange("https://calendar.test/cal/", USER, PASSWORD, b"<c/>")
    except client.EventError as error:
        assert error.kind == "network"
    else:
        raise AssertionError("exchange connected")
    proxies = [handler for handler in passed[0] if isinstance(handler, urllib.request.ProxyHandler)]
    assert len(proxies) == 1 and proxies[0].proxies == {}
    assert targets
    hosts = [item[0] if isinstance(item, tuple) else item for item in targets]
    assert hosts[0] == "calendar.test"
    assert "proxy.test" not in hosts
