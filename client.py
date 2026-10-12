"""One CalDAV calendar-query. Standard library only. Redirects are not followed."""
from __future__ import annotations

import base64
import http.client
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.parsers.expat
from typing import Any

MAX_BODY = 2_000_000
MAX_DEPTH = 100
TIMEOUT_SECONDS = 20
USER_AGENT = "event-read"
_STAMP = re.compile(r"^\d{8}T\d{6}Z\Z")
_STATUS = re.compile(r"HTTP/\d+\.\d+\s+(\d{3})")


class EventError(Exception):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(kind)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop urllib from sending Authorization to a second URL."""

    def http_error_302(self, req, fp, code, msg, headers):  # noqa: ANN001
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def validate_url(url: str) -> bool:
    """True when url is http(s) with a host and a non-empty path, and nothing else."""
    if not isinstance(url, str) or url != url.strip() or any(ch.isspace() for ch in url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"}:
        return False
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return False
    if parts.query or parts.fragment or not parts.hostname or parts.path == "":
        return False
    return port is None or isinstance(port, int)


def valid_stamp(value: Any) -> bool:
    if not isinstance(value, str) or not _STAMP.fullmatch(value):
        return False
    month = int(value[4:6])
    day = int(value[6:8])
    hour = int(value[9:11])
    minute = int(value[11:13])
    second = int(value[13:15])
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour <= 23 and minute <= 59 and second <= 59):
        return False
    # Reject impossible calendar days without inventing a timezone conversion.
    thirty = {4, 6, 9, 11}
    if month in thirty and day > 30:
        return False
    if month == 2:
        year = int(value[0:4])
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        if day > (29 if leap else 28):
            return False
    return True


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def list_body(start: str, end: str) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        "<D:prop><C:calendar-data/></D:prop>"
        '<C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT">'
        f'<C:time-range start="{start}" end="{end}"/>'
        "</C:comp-filter></C:comp-filter></C:filter>"
        "</C:calendar-query>"
    )
    return xml.encode("utf-8")


def read_body(uid: str) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        "<D:prop><C:calendar-data/></D:prop>"
        '<C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT">'
        '<C:prop-filter name="UID">'
        f'<C:text-match collation="i;octet">{_xml(uid)}</C:text-match>'
        "</C:prop-filter>"
        "</C:comp-filter></C:comp-filter></C:filter>"
        "</C:calendar-query>"
    )
    return xml.encode("utf-8")


def _headers(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {
        "Authorization": f"Basic {token}",
        "Depth": "1",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/xml; charset=utf-8",
    }


def _chunked(response: Any) -> bool:
    """True when the body is framed by chunked transfer, not Content-Length.

    urllib still leaves Content-Length on the headers. A sized read then stops
    after that many decoded bytes and leaves the rest of the chunks unread.
    """
    if getattr(response, "chunked", False) is True:
        return True
    headers = getattr(response, "headers", None)
    if headers is None:
        return False
    transfer = headers.get("Transfer-Encoding")
    return isinstance(transfer, str) and transfer.lower() == "chunked"


def _read_limited(response: Any, limit: int) -> bytes:
    """Read one body. A declared length that is not fully received is a failure.

    urllib's sized read returns a short buffer when the connection closes early.
    A short but well-formed multistatus must not be treated as the whole calendar.
    Chunked framing ignores Content-Length. Stopping there would keep a prefix.
    """
    headers = getattr(response, "headers", None)
    declared = None if _chunked(response) else (
        headers.get("Content-Length") if headers is not None else None
    )
    try:
        if declared is not None:
            try:
                need = int(declared)
            except ValueError:
                raise EventError("network")
            if need < 0 or need > limit:
                raise EventError("too_large" if need > limit else "network")
            payload = response.read(need)
            if len(payload) != need:
                raise EventError("network")
            return payload
        payload = response.read(limit + 1)
    except EventError:
        raise
    except http.client.IncompleteRead as error:
        raise EventError("network") from error
    except TimeoutError as error:
        raise EventError("timeout") from error
    if len(payload) > limit:
        raise EventError("too_large")
    return payload


def exchange(url: str, user: str, password: str, body: bytes, timeout: float = TIMEOUT_SECONDS) -> tuple[int, bytes]:
    """Send one REPORT. A 3xx raises HTTPError from _NoRedirect and is not retried here."""
    request = urllib.request.Request(url, data=body, headers=_headers(user, password), method="REPORT")
    # ProxyHandler({}) replaces urllib's default, so an environment proxy does not
    # receive the Basic Authorization header. _NoRedirect replaces the redirect handler.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), _read_limited(response, MAX_BODY)
    except urllib.error.HTTPError as error:
        try:
            payload = _read_limited(error, MAX_BODY) if error.fp is not None else b""
        except EventError:
            payload = b""
        return int(error.code), payload
    except EventError:
        raise
    except TimeoutError as error:
        raise EventError("timeout") from error
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, TimeoutError):
            raise EventError("timeout") from error
        raise EventError("network") from error
    except OSError as error:
        raise EventError("network") from error


def _unescape(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            out.append({"n": "\n", "N": "\n", "\\": "\\", ";": ";", ",": ","}.get(nxt, nxt))
            index += 2
        else:
            out.append(value[index])
            index += 1
    return "".join(out)


def _unfold(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for line in text.split("\n"):
        if lines and line.startswith((" ", "\t")):
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _param_map(params: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for param in params:
        if "=" in param:
            key, value = param.split("=", 1)
            found[key.upper()] = value
    return found


def _same(values: list[Any]) -> bool:
    return all(item == values[0] for item in values)


def _components_from_ics(text: str) -> list[dict[str, Any]]:
    """Return one entry per VEVENT component, in document order.

    Count VEVENT at BEGIN lines. Do not search the word inside a summary.
    A recurring event with one changed occurrence is one calendar object that
    holds a master component and one RECURRENCE-ID component per changed
    occurrence (RFC 5545 section 3.8.4.4). They share one UID, so the UID of a
    component is not unique on its own. _group turns them into one row.
    """
    stack: list[str] = []
    components: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in _unfold(text):
        if not line or ":" not in line:
            continue
        left, raw_value = line.split(":", 1)
        pieces = left.split(";")
        name = pieces[0].upper()
        params = pieces[1:]
        if name == "BEGIN":
            comp = raw_value.strip().upper()
            stack.append(comp)
            if comp == "VEVENT":
                current = {
                    "uids": [],
                    "summaries": [],
                    "starts": [],
                    "ends": [],
                    "recurrences": [],
                    "repeats": False,
                }
                components.append(current)
            continue
        if name == "END":
            if stack:
                stack.pop()
            if raw_value.strip().upper() == "VEVENT":
                current = None
            continue
        if current is None or not stack or stack[-1] != "VEVENT":
            continue
        if name == "UID":
            current["uids"].append(_unescape(raw_value))
        elif name == "SUMMARY":
            current["summaries"].append(_unescape(raw_value))
        elif name == "DTSTART":
            current["starts"].append((params, _unescape(raw_value)))
        elif name == "DTEND":
            current["ends"].append((params, _unescape(raw_value)))
        elif name == "RECURRENCE-ID":
            current["recurrences"].append((params, _unescape(raw_value)))
        elif name in {"RRULE", "RDATE"}:
            current["repeats"] = True
    built: list[dict[str, Any]] = []
    for item in components:
        uids = item["uids"]
        if not uids or uids[0] == "":
            raise EventError("missing_uid")
        if len(uids) > 1:
            raise EventError("ambiguous")
        starts = item["starts"]
        if not starts:
            raise EventError("missing_start")
        if len(starts) > 1:
            raise EventError("ambiguous")
        start_params, start = starts[0]
        summaries = item["summaries"]
        if summaries and not _same(summaries):
            raise EventError("ambiguous")
        ends = item["ends"]
        if ends and not _same(ends):
            raise EventError("ambiguous")
        recurrences = item["recurrences"]
        if recurrences and not _same(recurrences):
            raise EventError("ambiguous")
        occurrence = None
        if recurrences:
            rec_params, rec_value = recurrences[0]
            occurrence = (_param_map(rec_params).get("TZID"), rec_value)
        tzid = _param_map(start_params).get("TZID")
        if start.endswith("Z"):
            tzid = None
        end = None
        if ends:
            end = ends[0][1]
        built.append(
            {
                "uid": uids[0],
                "summary": summaries[0] if summaries else None,
                "start": start,
                "end": end,
                "tzid": tzid,
                "occurrence": occurrence,
                "repeats": bool(item["repeats"]),
            }
        )
    return built


def _group(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per uid. A RECURRENCE-ID component belongs to the master of its uid.

    Two components are the same event only when the uid and the RECURRENCE-ID
    are both the same. Two masters of one uid, or two copies of one changed
    occurrence, stay ambiguous.
    """
    order: list[str] = []
    groups: dict[str, dict[str, Any]] = {}
    for component in components:
        uid = component["uid"]
        group = groups.get(uid)
        if group is None:
            group = {"master": None, "first": component, "keys": set(), "changed": 0}
            groups[uid] = group
            order.append(uid)
        key = component["occurrence"]
        if key in group["keys"]:
            raise EventError("ambiguous")
        group["keys"].add(key)
        if key is None:
            group["master"] = component
        else:
            group["changed"] += 1
    rows: list[dict[str, Any]] = []
    for uid in order:
        group = groups[uid]
        base = group["master"] or group["first"]
        rows.append(
            {
                "uid": uid,
                "summary": base["summary"],
                "start": base["start"],
                "end": base["end"],
                "tzid": base["tzid"],
                "repeats": bool(base["repeats"] or group["changed"] or base["occurrence"]),
            }
        )
    return rows


def _local(name: str) -> str:
    return name.split(" ")[-1]


def parse_multistatus(payload: bytes) -> list[dict[str, Any]]:
    """Return VEVENTs in document order. The caller applies the 50-event cap."""
    try:
        return _parse_multistatus(payload)
    except RecursionError as error:
        raise EventError("too_deep") from error


def _parse_multistatus(payload: bytes) -> list[dict[str, Any]]:
    if len(payload) > MAX_BODY:
        raise EventError("too_large")
    budget = 0
    root: dict[str, Any] | None = None
    stack: list[dict[str, Any]] = []

    def add(size: int) -> None:
        nonlocal budget
        budget += size
        if budget > MAX_BODY:
            raise EventError("too_large")

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal root
        add(len(name.encode("utf-8")))
        for key, value in attrs.items():
            add(len(str(key).encode("utf-8")) + len(str(value).encode("utf-8")))
        if len(stack) >= MAX_DEPTH:
            # _collect walks this tree with recursion. Stop before the stack does.
            raise EventError("too_deep")
        node: dict[str, Any] = {"local": _local(name), "text": [], "children": []}
        if stack:
            stack[-1]["children"].append(node)
        else:
            root = node
        stack.append(node)

    def end(name: str) -> None:
        if stack:
            stack.pop()

    def chars(data: str) -> None:
        add(len(data.encode("utf-8")))
        if stack:
            stack[-1]["text"].append(data)

    def reject_doctype(*_args: object) -> None:
        raise EventError("not_xml")

    parser = xml.parsers.expat.ParserCreate(namespace_separator=" ")
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.StartDoctypeDeclHandler = reject_doctype
    parser.ExternalEntityRefHandler = lambda *_args: 0
    try:
        parser.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)
    except Exception:
        pass
    try:
        parser.Parse(payload, True)
    except EventError:
        raise
    except xml.parsers.expat.ExpatError as error:
        raise EventError("not_xml") from error
    if root is None or root["local"] != "multistatus":
        raise EventError("not_multistatus")
    components: list[dict[str, Any]] = []
    for response in root["children"]:
        if response["local"] != "response":
            continue
        statuses = _collect(response, "status")
        if not statuses:
            raise EventError("inner_status")
        for status in statuses:
            match = _STATUS.search(status.strip())
            if not match or int(match.group(1)) != 200:
                raise EventError("inner_status")
        for calendar in _collect(response, "calendar-data"):
            components.extend(_components_from_ics(calendar))
    # Components are grouped across the whole multistatus, so a master and a
    # changed occurrence stay one event even when a server splits them.
    return _group(components)


def _collect(node: dict[str, Any], local: str) -> list[str]:
    found: list[str] = []
    if node["local"] == local:
        found.append("".join(node["text"]))
    for child in node["children"]:
        found.extend(_collect(child, local))
    return found
