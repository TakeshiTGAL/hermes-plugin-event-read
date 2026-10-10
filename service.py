"""list_events and read_event. A cron no-argument list is the only path that records uids."""
from __future__ import annotations

import importlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

if __package__:
    from .client import (
        MAX_BODY,
        EventError,
        exchange,
        list_body,
        parse_multistatus,
        read_body,
        valid_stamp,
        validate_url,
    )
else:
    from client import (
        MAX_BODY,
        EventError,
        exchange,
        list_body,
        parse_multistatus,
        read_body,
        valid_stamp,
        validate_url,
    )

TOOLSET = "event_read"
PLUGIN_NAME = "event-read"
HOST_PROCESS_ENV = "HERMES_PLUGIN_HOST_PROCESS"
CAP = 50
MAX_SEEN = 5_000
QUIET = timedelta(hours=23)
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
READ_ERROR = "The calendar could not be read. The seen uids were not changed."
CANNOT_MONITOR = "This process cannot monitor the calendar. The seen uids were not changed."
DID_NOT_UPDATE = "This call did not update the seen uids."
LIST_ARGS = "list_events takes a start and an end together, or no arguments. The seen uids were not changed."
READ_ARGS = "read_event takes one uid. The seen uids were not changed."
NEED_ENV = "CALDAV_URL, CALDAV_USER, and CALDAV_PASSWORD must be set."
NOT_FOUND = "No event with that uid."
FIRST_RECORD = "Recorded the current event uids. Nothing was announced."
NO_NEW = "No new event uid."
RECOVERED = "The calendar can be read again."
URL_CHANGED = (
    "The saved calendar URL does not match CALDAV_URL, so nothing was read and nothing was announced. "
    "The seen uids were not changed."
)
CORRUPT = "The saved state could not be read, so it was left in place. The seen uids were not changed."
NOT_SAVED = "The seen uids could not be saved, so the calendar was not read. The seen uids were not changed."
SAVE_FAILED = "The calendar was read, but the seen uids could not be saved. The seen uids were not changed."
TOO_MANY = (
    "More than 50 events are in the window, so none were recorded and this is not the whole list. "
    "The seen uids were not changed."
)
QUIET_NOTE = "The same cause stays quiet for 23 hours."


def read_error(kind: str) -> str:
    return f"The calendar could not be read ({kind}). The seen uids were not changed."

Transport = Callable[[str, str, str, bytes], tuple[int, bytes]]
Clock = Callable[[], datetime]
CronJudge = Callable[[], Optional[bool]]
DataDir = Callable[[], Path]
WriteGuard = Callable[[str], Optional[str]]


@dataclass
class Deps:
    url: str
    user: str
    password: str
    now: Clock
    transport: Transport | None = None
    cron: CronJudge | None = None
    data_dir: DataDir | None = None
    write_guard: WriteGuard | None = None


def dumps(body: dict[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=True, sort_keys=True)


def active_profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        name = get_active_profile_name()
    except Exception:
        return "default"
    if not isinstance(name, str) or not _PROFILE_NAME.fullmatch(name.strip()):
        return "default"
    return name.strip() or "default"


def hermes_line(command: str) -> str:
    """Printed hermes command. -p goes before the subcommand. This plugin does not define -p."""
    name = active_profile_name()
    if name != "default":
        return f"hermes -p {name} {command}"
    return f"hermes {command}"


def _scrub(text: str, password: str) -> str:
    if password and len(password) >= 8 and password in text:
        return text.replace(password, "[redacted]")
    return text


def _result(
    deps: Deps,
    *,
    ok: bool,
    error: str | None,
    events: list[dict[str, Any]] | None = None,
    truncated: bool = False,
    recorded: bool = False,
    announced: list[str] | None = None,
    notify: bool = False,
    message: str,
    next_step: str,
) -> str:
    body = {
        "ok": ok,
        "error": error,
        "events": events or [],
        "truncated": truncated,
        "recorded": recorded,
        "announced": announced or [],
        "notify": notify,
        "message": _scrub(message, deps.password),
        "next_step": _scrub(next_step, deps.password),
    }
    return dumps(body)


def _load_cron_flag() -> bool:
    module = importlib.import_module("tools.approval_context")
    fn = getattr(module, "_is_cron_approval_context", None)
    if not callable(fn):
        raise LookupError("cron check is missing")
    value = fn()
    if not isinstance(value, bool):
        raise LookupError("cron check did not return a bool")
    return value


def judge_cron() -> bool | None:
    """True on a confirmed cron turn, False on a confirmed manual turn, None when it cannot be judged.

    None is not a manual turn. A host value other than unset, empty, or 0 is None.
    Import failure, a renamed helper, an exception, or a non-bool is None.
    A helper that returns False is manual.
    """
    if os.environ.get(HOST_PROCESS_ENV, "").strip() not in {"", "0"}:
        return None
    try:
        value = _load_cron_flag()
    except Exception:
        return None
    if isinstance(value, bool):
        return value
    return None


def default_data_dir() -> Path:
    module = importlib.import_module("plugins.plugin_storage")
    fn = getattr(module, "plugin_data_dir", None)
    if not callable(fn):
        raise RuntimeError("plugin_data_dir is missing")
    path = fn(PLUGIN_NAME)
    return path if isinstance(path, Path) else Path(path)


def default_write_guard(path: str) -> str | None:
    """Call get_write_denied_error with the path only. v2026.9.21 has no entry argument."""
    try:
        module = importlib.import_module("agent.file_safety")
        fn = getattr(module, "get_write_denied_error", None)
    except Exception:
        return "unavailable"
    if not callable(fn):
        return "unavailable"
    try:
        denied = fn(path)
    except Exception:
        return "unavailable"
    if denied:
        return str(denied)
    return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clock(deps: Deps) -> datetime:
    now = deps.now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return now.replace(microsecond=0)


def _window(deps: Deps) -> tuple[str, str]:
    start = _clock(deps)
    end = start + timedelta(days=7)
    return start.strftime("%Y%m%dT%H%M%SZ"), end.strftime("%Y%m%dT%H%M%SZ")


def _config_problem(deps: Deps) -> str | None:
    if not deps.url or not deps.user or not deps.password:
        return NEED_ENV
    if "\r" in deps.user or "\n" in deps.user or "\r" in deps.password or "\n" in deps.password:
        return "CALDAV_USER or CALDAV_PASSWORD contains a line break, so nothing was requested."
    if not validate_url(deps.url):
        return (
            "CALDAV_URL must be http or https with a host and a non-empty path, and without a username, "
            "query, fragment, or surrounding spaces. Nothing was requested."
        )
    return None


def _bad_config(deps: Deps, message: str) -> str:
    return _result(
        deps,
        ok=False,
        error="bad_config",
        notify=True,
        message=message,
        next_step="Set CALDAV_URL, CALDAV_USER, and CALDAV_PASSWORD. Nothing was requested.",
    )


def _bad_args(deps: Deps, message: str) -> str:
    return _result(
        deps,
        ok=False,
        error="bad_args",
        notify=True,
        message=message,
        next_step="Fix the arguments. Nothing was requested and the saved uids were not changed.",
    )


_NOT_SAVED_STEP = (
    "Check that the Hermes profile data directory can be written. Nothing was requested."
)


def _send(deps: Deps, body: bytes) -> tuple[int, bytes]:
    transport = deps.transport or exchange
    return transport(deps.url, deps.user, deps.password, body)


def _read_report(deps: Deps, body: bytes) -> tuple[list[dict[str, Any]] | None, str | None]:
    try:
        status, payload = _send(deps, body)
    except EventError as error:
        return None, error.kind
    if len(payload) > MAX_BODY:
        return None, "too_large"
    if status != 207:
        return None, f"http_{status}"
    try:
        return parse_multistatus(payload), None
    except EventError as error:
        return None, error.kind


def _seen_path(directory: Path) -> Path:
    return directory / "seen.json"


def _notice_path(directory: Path) -> Path:
    return directory / "notices.json"


def _load_state(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read the seen uids. Only uids live here, so a problem never rewrites them."""
    if not path.exists():
        return None, None
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return None, "corrupt"
    if not isinstance(data, dict):
        return None, "corrupt"
    url = data.get("calendar_url")
    uids = data.get("uids")
    if not isinstance(url, str) or not isinstance(uids, list):
        return None, "corrupt"
    if not all(isinstance(item, str) for item in uids):
        return None, "corrupt"
    return {"calendar_url": url, "uids": uids}, None


def _write_json(path: Path, payload: dict[str, Any], guard: WriteGuard) -> None:
    try:
        denied = guard(str(path))
    except Exception as error:  # a guard that raises is a guard that is not there
        raise OSError("the Hermes write check could not be used") from error
    if denied:
        raise OSError(denied)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    tmp = path.parent / f".{path.stem}-{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(blob, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _watch_dir(deps: Deps) -> Path | None:
    try:
        directory = (deps.data_dir or default_data_dir)()
    except Exception:
        return None
    return directory if isinstance(directory, Path) else None


def _load_notice(path: Path) -> dict[str, str] | None:
    """Read when the last watch problem was announced. No uid is kept here."""
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("kind")
    when = data.get("notified_at")
    if not isinstance(kind, str) or not isinstance(when, str):
        return None
    try:
        datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return {"kind": kind, "notified_at": when}


def _notice_due(notice: dict[str, str] | None, kind: str, now: datetime) -> bool:
    """First time, a changed cause, or 23 hours after the last announcement.

    The margin is 23 hours so a daily job that fires a few seconds early still
    announces instead of staying quiet for another day.
    """
    if notice is None or notice["kind"] != kind:
        return True
    previous = datetime.strptime(notice["notified_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return now - previous >= QUIET


def _watch_problem(
    deps: Deps,
    directory: Path | None,
    kind: str,
    message: str,
    next_step: str,
    *,
    truncated: bool = False,
    remember: bool = True,
) -> str:
    """Announce a watch problem the first time, on a change, and once a day.

    Without a usable data directory there is no memory, so it announces.
    An unjudgeable run does not write that memory either, so it announces every time.
    A manual call and a cron call look the same under plugins.isolation: host, and
    one shared notice would hide the watch from its owner.
    """
    notify = True
    if remember and directory is not None:
        path = _notice_path(directory)
        now = _clock(deps)
        notify = _notice_due(_load_notice(path), kind, now)
        if notify:
            stamp = {"kind": kind, "notified_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
            try:
                _write_json(path, stamp, deps.write_guard or default_write_guard)
            except OSError:
                pass
    return _result(
        deps,
        ok=False,
        error=kind,
        truncated=truncated,
        notify=notify,
        message=message,
        next_step=next_step,
    )


def _clear_notice(directory: Path) -> None:
    path = _notice_path(directory)
    try:
        path.unlink()
    except OSError:
        pass


def _entered_message(uids: list[str]) -> str:
    if len(uids) == 1:
        return f"Event uid now in the window: {uids[0]}."
    return "Event uids now in the window: " + ", ".join(uids) + "."


def _finish_watch(
    deps: Deps,
    directory: Path,
    state: dict[str, Any] | None,
    events: list[dict[str, Any]],
) -> str:
    current = [event["uid"] for event in events]
    previous = None if state is None else state["uids"]
    recovering = _load_notice(_notice_path(directory)) is not None
    if previous is None:
        announced: list[str] = []
        merged = list(current)
    else:
        have = set(previous)
        announced = [uid for uid in current if uid not in have]
        merged = list(previous) + announced
    # The file keeps the newest uids only, so one call stays bounded in time and size.
    merged = merged[-MAX_SEEN:]
    if recovering and previous is None:
        message = f"{RECOVERED} {FIRST_RECORD}"
        notify = True
        announced_out: list[str] = []
    elif recovering and announced:
        message = f"{RECOVERED} {_entered_message(announced)}"
        notify = True
        announced_out = announced
    elif recovering:
        message = f"{RECOVERED} {NO_NEW}"
        notify = True
        announced_out = []
    elif previous is None:
        message = FIRST_RECORD
        notify = False
        announced_out = []
    elif announced:
        message = _entered_message(announced)
        notify = True
        announced_out = announced
    else:
        message = NO_NEW
        notify = False
        announced_out = []
    updated = {"calendar_url": deps.url, "uids": merged}
    try:
        _write_json(_seen_path(directory), updated, deps.write_guard or default_write_guard)
    except OSError:
        return _watch_problem(
            deps,
            directory,
            "not_saved",
            SAVE_FAILED,
            "Check that the Hermes profile data directory can be written. Nothing was recorded.",
        )
    if recovering:
        _clear_notice(directory)
    return _result(
        deps,
        ok=True,
        error=None,
        events=events,
        recorded=True,
        announced=announced_out,
        notify=notify,
        message=message,
        next_step=f"The job stays until you run {hermes_line('cron remove <id>')}.",
    )


def _read_message(deps: Deps, events: list[dict[str, Any]], *, cron: bool, truncated: bool) -> str:
    if truncated:
        text = "Returned the first 50 events. This is not the whole list. The seen uids were not changed."
    else:
        count = len(events)
        noun = "event" if count == 1 else "events"
        text = f"Returned {count} {noun}. The seen uids were not changed."
    if cron:
        return f"{text} {DID_NOT_UPDATE}"
    return text


def list_events(deps: Deps, args: dict[str, Any] | None) -> str:
    if not isinstance(args, dict):
        return _bad_args(deps, LIST_ARGS)
    if set(args) - {"start", "end"}:
        return _bad_args(deps, LIST_ARGS)
    ranged = "start" in args or "end" in args
    if ranged and ("start" not in args or "end" not in args):
        return _bad_args(deps, LIST_ARGS)
    if ranged and (
        not valid_stamp(args.get("start"))
        or not valid_stamp(args.get("end"))
        or args["end"] <= args["start"]
    ):
        return _bad_args(deps, LIST_ARGS)
    cron_flag: bool | None = False
    directory: Path | None = None
    if not ranged:
        cron_flag = (deps.cron or judge_cron)()
        directory = _watch_dir(deps)
        if cron_flag is None:
            return _watch_problem(
                deps,
                directory,
                "cannot_monitor",
                CANNOT_MONITOR,
                "Run the watch from a cron turn in this process. The saved uids were not changed.",
                remember=False,
            )
    watching = (not ranged) and cron_flag is True
    problem = _config_problem(deps)
    if problem:
        if watching:
            return _watch_problem(
                deps,
                directory,
                "bad_config",
                problem,
                f"Set CALDAV_URL, CALDAV_USER, and CALDAV_PASSWORD. Nothing was requested. {QUIET_NOTE}",
            )
        return _bad_config(deps, problem)
    state: dict[str, Any] | None = None
    if watching:
        if directory is None:
            return _watch_problem(deps, None, "not_saved", NOT_SAVED, _NOT_SAVED_STEP)
        guard = deps.write_guard or default_write_guard
        try:
            denied = guard(str(_seen_path(directory)))
        except Exception:
            denied = "unavailable"
        if denied:
            return _watch_problem(deps, directory, "not_saved", NOT_SAVED, _NOT_SAVED_STEP)
        state, trouble = _load_state(_seen_path(directory))
        if trouble:
            return _watch_problem(
                deps,
                directory,
                "corrupt_state",
                CORRUPT,
                f"Delete that file yourself to start again. The saved uids were not changed. {QUIET_NOTE}",
            )
        if state is not None and state["calendar_url"] != deps.url:
            return _watch_problem(
                deps,
                directory,
                "calendar_url_changed",
                URL_CHANGED,
                f"Point CALDAV_URL back, or delete the saved file to start again. {QUIET_NOTE}",
            )
    if ranged:
        body = list_body(str(args["start"]), str(args["end"]))
    else:
        start, end = _window(deps)
        body = list_body(start, end)
    events, kind = _read_report(deps, body)
    if kind:
        if watching and directory is not None:
            return _watch_problem(
                deps,
                directory,
                kind,
                read_error(kind),
                f"The saved uids were left as they were. {QUIET_NOTE}",
            )
        cron_turn = False
        if ranged:
            cron_turn = (deps.cron or judge_cron)() is True
        message = read_error(kind)
        if cron_turn:
            message = f"{message} {DID_NOT_UPDATE}"
        return _result(
            deps,
            ok=False,
            error=kind,
            notify=cron_turn,
            message=message,
            next_step="Nothing was recorded.",
        )
    assert events is not None
    if len(events) > CAP:
        if watching and directory is not None:
            return _watch_problem(
                deps,
                directory,
                "too_many",
                TOO_MANY,
                "Shrink the collection until the next seven days hold 50 events or fewer. "
                f"The next no-argument list_events records them. {QUIET_NOTE}",
                truncated=True,
            )
        cron_turn = (deps.cron or judge_cron)() is True if ranged else False
        return _result(
            deps,
            ok=True,
            error=None,
            events=events[:CAP],
            truncated=True,
            notify=cron_turn,
            message=_read_message(deps, events[:CAP], cron=cron_turn, truncated=True),
            next_step="This is not the whole list. The saved uids were not changed.",
        )
    if watching and directory is not None:
        return _finish_watch(deps, directory, state, events)
    cron_turn = False
    if ranged:
        cron_turn = (deps.cron or judge_cron)() is True
    return _result(
        deps,
        ok=True,
        error=None,
        events=events,
        notify=cron_turn,
        message=_read_message(deps, events, cron=cron_turn, truncated=False),
        next_step="The saved uids were not changed.",
    )


def read_event(deps: Deps, args: dict[str, Any] | None) -> str:
    if not isinstance(args, dict) or set(args) - {"uid"}:
        return _bad_args(deps, READ_ARGS)
    uid = args.get("uid")
    if not isinstance(uid, str) or uid == "" or len(uid) > 1024 or any(ord(ch) < 32 or ord(ch) == 127 for ch in uid):
        return _bad_args(deps, READ_ARGS)
    problem = _config_problem(deps)
    if problem:
        return _bad_config(deps, problem)
    cron_turn = (deps.cron or judge_cron)() is True
    events, kind = _read_report(deps, read_body(uid))
    if kind:
        message = read_error(kind)
        if cron_turn:
            message = f"{message} {DID_NOT_UPDATE}"
        return _result(
            deps,
            ok=False,
            error=kind,
            notify=cron_turn,
            message=message,
            next_step="Nothing was recorded.",
        )
    assert events is not None
    # A CalDAV text-match is a substring match, so asking for meet-1 can return
    # meet-12 as well. Keep only an exact uid out of what came back.
    matched = [event for event in events if event["uid"] == uid]
    if not matched:
        message = NOT_FOUND
        if events:
            message = f"{NOT_FOUND} The collection returned {len(events)} other uid(s), and none was chosen."
        if cron_turn:
            message = f"{message} {DID_NOT_UPDATE}"
        return _result(
            deps,
            ok=False,
            error="not_found",
            notify=cron_turn,
            message=message,
            next_step="Nothing was recorded.",
        )
    if len(matched) > 1:
        message = "More than one event has that uid, so none was chosen. The seen uids were not changed."
        if cron_turn:
            message = f"{message} {DID_NOT_UPDATE}"
        return _result(
            deps,
            ok=False,
            error="ambiguous",
            notify=cron_turn,
            message=message,
            next_step="Nothing was recorded.",
        )
    return _result(
        deps,
        ok=True,
        error=None,
        events=matched,
        notify=cron_turn,
        message=_read_message(deps, matched, cron=cron_turn, truncated=False),
        next_step="The saved uids were not changed.",
    )
