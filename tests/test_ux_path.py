"""First request, a read with no approval, and one retry from the reply."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import service

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
URL = "http://127.0.0.1:5232/probe/cal/"
PHRASES = ("来週の予定", "今日の予定", "予定を教えて", "この予定の中身", "meet-1 の中身")


def _ics(uid: str) -> str:
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n"
        f"UID:{uid}\nSUMMARY:Standup\nDTSTART:20261009T010000Z\nDTEND:20261009T020000Z\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )


def _multi(items: list[str]) -> bytes:
    parts = [
        "<?xml version='1.0' encoding='utf-8'?>",
        '<multistatus xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">',
    ]
    for ics in items:
        escaped = ics.replace("&", "&amp;").replace("<", "&lt;")
        parts.append(
            "<response><href>/probe/cal/item.ics</href><propstat><prop>"
            f"<C:calendar-data>{escaped}</C:calendar-data>"
            "</prop><status>HTTP/1.1 200 OK</status></propstat></response>"
        )
    parts.append("</multistatus>")
    return "\n".join(parts).encode("utf-8")


class _Sink:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls = 0

    def __call__(self, url, user, password, data):
        self.calls += 1
        return 207, self.body


def _deps(tmp_path: Path, sink: _Sink, **kw) -> service.Deps:
    directory = tmp_path / "plugin-data" / "event-read"
    directory.mkdir(parents=True, exist_ok=True)
    return service.Deps(
        url=kw.get("url", URL),
        user=kw.get("user", "calendar-user"),
        password=kw.get("password", "super-secret-password"),
        now=lambda: NOW,
        transport=sink,
        cron=lambda: False,
        data_dir=lambda: directory,
        write_guard=lambda _path: None,
    )


def test_skill_puts_each_japanese_request_on_an_english_search_line():
    text = (ROOT / "skills" / "caldav-calendar" / "SKILL.md").read_text(encoding="utf-8")
    assert "tool_search" in text and "English" in text
    for phrase in PHRASES:
        line = next(row for row in text.splitlines() if phrase in row)
        assert "tool_search" in line and "calendar" in line


def test_readme_first_request_has_six_steps():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = text.split("## First request", 1)[1].split("\n## ", 1)[0]
    assert len(re.findall(r"(?m)^\d+\. ", section)) == 6
    assert "list_events" in section and "no arguments" in section


def test_a_read_asks_nothing_and_writes_nothing(tmp_path):
    assert "approver" not in service.Deps.__dataclass_fields__
    sink = _Sink(_multi([_ics("meet-1")]))
    body = json.loads(service.list_events(_deps(tmp_path, sink), {}))
    assert body["ok"] is True
    assert sink.calls == 1
    assert not (tmp_path / "plugin-data" / "event-read" / "seen.json").exists()


def test_a_missing_uid_names_one_list_and_that_list_works(tmp_path):
    sink = _Sink(_multi([_ics("meet-1")]))
    missing = json.loads(service.read_event(_deps(tmp_path, sink), {"uid": "missing-uid"}))
    assert missing["ok"] is False and missing["error"] == "not_found"
    step = missing["next_step"]
    assert step.index("Call list_events with no arguments") < step.index("日本語")
    listed = json.loads(service.list_events(_deps(tmp_path, sink), {}))
    assert listed["ok"] is True
    assert [row["uid"] for row in listed["events"]] == ["meet-1"]


def test_missing_settings_name_a_bare_list(tmp_path):
    def refuse(*_args):
        raise AssertionError("sent")

    class _Blocked(_Sink):
        def __call__(self, url, user, password, data):
            return refuse()

    empty = json.loads(service.list_events(_deps(tmp_path, _Blocked(b""), url="", user="", password=""), {}))
    assert empty["ok"] is False
    step = empty["next_step"]
    assert "CALDAV_URL" in step and "no arguments" in step
    assert step.index("Set CALDAV_URL") < step.index("日本語")
    sink = _Sink(_multi([_ics("meet-1")]))
    ready = json.loads(service.list_events(_deps(tmp_path, sink), {}))
    assert ready["ok"] is True and sink.calls == 1
