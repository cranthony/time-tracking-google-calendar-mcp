"""An in-memory fake of the parts of Google Calendar v3 and Google
Sheets v4 this server uses, served over HTTP -- so the server under test
talks to it exactly as it talks to Google, through its own HTTP client.

It's faithful where the server depends on Google's behaviour, as the
probes (rust/src/bin/probe.rs) found it: recurring series expanded into
instances (with singleEvents), instances edited on their own as
exceptions, what a patch to a series' master does to them, a series
ended early by UNTIL, series split by importing
"<id>_R<time>@google.com", extendedProperties.private merged by a patch
with null removing a key, calendar etags checked by If-Match, deleted
events answering 410, inserting an event with a label the calendar hasn't
got answering 400, and Sheets' grid that values.update grows but
batchUpdateByDataFilter doesn't, and Calendar's batch endpoint answering
each request in a batch as if it came on its own.

Every request is logged (`FakeGoogle.requests`), so a scenario can check
that the server sent Google the requests its golden record has: then
Google would do the same as before, whatever this fake gets wrong
about it.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl, unquote
from zoneinfo import ZoneInfo

from dateutil import rrule
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

UTC = timezone.utc

# Google never lists more than it's asked for, but may list fewer; listing
# at most this many per page makes every scenario follow nextPageToken.
DEFAULT_PAGE_CAP = 7

# How far past a listing's end an endless series is expanded, at most.
_EXPANSION_HORIZON = timedelta(days=800)

_DROPPED_QUERY = {"alt", "prettyPrint", "key"}
"""Query parameters a client library adds that say nothing to Google about
what's asked (googleapiclient sends alt=json)."""


class GoogleError(Exception):
    def __init__(self, status: int, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.reason = reason or {400: "badRequest", 404: "notFound", 409: "duplicate", 410: "deleted", 412: "conditionNotMet", 429: "rateLimitExceeded"}.get(status, "")

    def payload(self) -> dict:
        return {"error": {"code": self.status, "message": self.message, "errors": [{"message": self.message, "reason": self.reason}]}}

    def response(self) -> JSONResponse:
        return JSONResponse(self.payload(), status_code=self.status)


@dataclass
class LoggedRequest:
    method: str
    path: str
    query: dict[str, list[str]]
    if_match: str | None
    body: Any
    batched: bool = False
    """Whether it came inside a batch, rather than as a request of its own."""

    def as_dict(self) -> dict:
        logged = {"method": self.method, "path": self.path, "query": self.query, "if_match": self.if_match, "body": self.body}
        if self.batched:
            logged["batched"] = True
        return logged


# --- times -----------------------------------------------------------------


def _parse_moment(value: dict, zone: ZoneInfo) -> datetime | date:
    """An event time ({"dateTime"...} or {"date"...}) as an aware datetime,
    or a date for an all-day one."""
    if "date" in value and "dateTime" not in value:
        return date.fromisoformat(value["date"])
    raw = value["dateTime"].replace("Z", "+00:00")
    moment = datetime.fromisoformat(raw)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(value["timeZone"]) if value.get("timeZone") else zone)
    return moment


def _render_moment(moment: datetime | date, zone: ZoneInfo, time_zone: str | None) -> dict:
    """An event time as Google returns it: in the event's own zone if it
    has one, else the calendar's, with "Z" for UTC."""
    if isinstance(moment, datetime):
        local = moment.astimezone(ZoneInfo(time_zone) if time_zone else zone)
        text = local.isoformat()
        if text.endswith("+00:00"):
            text = text[:-6] + "Z"
        rendered = {"dateTime": text}
        if time_zone:
            rendered["timeZone"] = time_zone
        return rendered
    return {"date": moment.isoformat()}


def _sort_key(moment: datetime | date, zone: ZoneInfo) -> datetime:
    if isinstance(moment, datetime):
        return moment.astimezone(UTC)
    return datetime.combine(moment, time(), zone).astimezone(UTC)


def _overlaps(start, end, zone, time_min: datetime | None, time_max: datetime | None) -> bool:
    s, e = _sort_key(start, zone), _sort_key(end, zone)
    if time_min is not None and e <= time_min:
        return False
    if time_max is not None and s >= time_max:
        return False
    return True


def _instance_suffix(moment: datetime | date) -> str:
    if isinstance(moment, datetime):
        return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return moment.strftime("%Y%m%d")


def _parse_rfc3339(text: str) -> datetime:
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise GoogleError(400, f"Bad Request: time {text!r} has no offset")
    return moment


# --- calendar --------------------------------------------------------------


@dataclass
class FakeCalendar:
    id: str
    resource: dict
    events: dict[str, dict] = field(default_factory=dict)
    """Every stored event by id: single events, series masters, and
    instances edited (or cancelled) on their own -- exceptions, stored
    under their instance id with recurringEventId set."""

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.resource.get("timeZone") or "UTC")


class FakeGoogle:
    def __init__(self, *, page_cap: int = DEFAULT_PAGE_CAP) -> None:
        self.page_cap = page_cap
        self.calendars: dict[str, FakeCalendar] = {}
        self.spreadsheets: dict[str, dict] = {}
        self.requests: list[LoggedRequest] = []
        self.rate_limit_next: int = 0
        """Answer this many of the next Sheets requests with 429."""
        self.fail_next: list[tuple] = []
        """(method, a piece of the path, status, message[, skip]): answer
        the next request that matches each with that error, once -- a write
        that fails partway through applying something. With `skip`, that
        many matching requests go through first."""
        self.token_requests = 0
        self.authkit_requests: list[dict] = []
        self._ids = itertools.count(1)
        self._lock = threading.RLock()

    # -- ids and etags --

    def _next_id(self, prefix: str = "") -> str:
        n = next(self._ids)
        digest = hashlib.sha256(f"{prefix}{n}".encode()).hexdigest()
        # Event ids are base32hex (a-v, 0-9); hex digits are a subset.
        return digest[:26]

    def _etag(self) -> str:
        return f'"{next(self._ids):016d}"'

    # -- seeding, for scenarios --

    def add_calendar(self, summary: str = "Time Tracking", time_zone: str | None = "America/New_York", description: str | None = None) -> str:
        with self._lock:
            calendar_id = f"{self._next_id('cal')}@group.calendar.google.com"
            resource = {"kind": "calendar#calendar", "id": calendar_id, "summary": summary, "etag": self._etag()}
            if time_zone:
                resource["timeZone"] = time_zone
            if description is not None:
                resource["description"] = description
            self.calendars[calendar_id] = FakeCalendar(calendar_id, resource)
            return calendar_id

    def add_event(self, calendar_id: str, body: dict) -> str:
        """Put an event on the calendar as if made in Google Calendar
        itself (a recurring series, say, which no tool makes)."""
        with self._lock:
            calendar = self.calendars[calendar_id]
            event_id = body.get("id") or self._next_id("seed")
            calendar.events[event_id] = self._new_event(calendar, body, event_id)
            return event_id

    def calendar(self, calendar_id: str) -> FakeCalendar:
        try:
            return self.calendars[unquote(calendar_id)]
        except KeyError:
            raise GoogleError(404, "Not Found") from None

    # -- calendars --

    def insert_calendar(self, body: dict) -> dict:
        calendar_id = self.add_calendar(body.get("summary", ""), body.get("timeZone"), body.get("description"))
        return copy.deepcopy(self.calendars[calendar_id].resource)

    def get_calendar(self, calendar_id: str) -> dict:
        return copy.deepcopy(self.calendar(calendar_id).resource)

    def patch_calendar(self, calendar_id: str, body: dict, if_match: str | None) -> dict:
        calendar = self.calendar(calendar_id)
        if if_match is not None and if_match != calendar.resource["etag"]:
            raise GoogleError(412, "Precondition Failed")
        for key, value in body.items():
            if key in ("id", "etag", "kind"):
                continue
            if key == "labelProperties":
                labels = []
                for label in (value or {}).get("eventLabels", []):
                    label = dict(label)
                    label.setdefault("id", self._next_id("label"))
                    labels.append(label)
                calendar.resource["labelProperties"] = {"eventLabels": labels}
            elif value is None:
                calendar.resource.pop(key, None)
            else:
                calendar.resource[key] = value
        calendar.resource["etag"] = self._etag()
        return copy.deepcopy(calendar.resource)

    # -- events: series expansion --

    def _rules(self, master: dict) -> tuple[list[str], dict[str, str] | None]:
        lines = master.get("recurrence") or []
        rrule_line = next((line for line in lines if line.startswith("RRULE:")), None)
        if rrule_line is None:
            return lines, None
        parts = dict(part.split("=", 1) for part in rrule_line[len("RRULE:"):].split(";") if part)
        return lines, parts

    def _occurrences(self, calendar: FakeCalendar, master: dict, until: datetime | None) -> list[datetime | date]:
        """The original starts of `master`'s instances, in order, up to
        `until` (exclusive, for endless series)."""
        zone = calendar.zone
        start = _parse_moment(master["start"], zone)
        lines, parts = self._rules(master)
        if parts is None:
            return [start]
        all_day = not isinstance(start, datetime)
        local_zone = ZoneInfo(master["start"].get("timeZone")) if master["start"].get("timeZone") else zone
        naive_start = datetime.combine(start, time()) if all_day else start.astimezone(local_zone).replace(tzinfo=None)
        rule_until = None
        rule_text = ";".join(f"{k}={v}" for k, v in parts.items() if k != "UNTIL")
        if "UNTIL" in parts:
            raw = parts["UNTIL"]
            if raw.endswith("Z"):
                rule_until = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            elif "T" in raw:
                rule_until = datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=local_zone)
            else:
                rule_until = datetime.combine(datetime.strptime(raw, "%Y%m%d").date(), time(23, 59, 59), local_zone)
        ruleset = rrule.rruleset()
        ruleset.rrule(rrule.rrulestr(rule_text, dtstart=naive_start))
        for line in lines:
            if line.startswith("EXDATE"):
                for value in line.split(":", 1)[1].split(","):
                    if "T" in value:
                        fmt = "%Y%m%dT%H%M%SZ" if value.endswith("Z") else "%Y%m%dT%H%M%S"
                        moment = datetime.strptime(value, fmt)
                        if value.endswith("Z"):
                            moment = moment.replace(tzinfo=UTC).astimezone(local_zone).replace(tzinfo=None)
                    else:
                        moment = datetime.combine(datetime.strptime(value, "%Y%m%d").date(), time())
                    ruleset.exdate(moment)
        found: list[datetime | date] = []
        for naive in ruleset:
            moment: datetime | date = naive.date() if all_day else naive.replace(tzinfo=local_zone)
            key = _sort_key(moment, zone)
            if rule_until is not None and key > rule_until:
                break
            if until is not None and key >= until:
                break
            found.append(moment)
            if len(found) > 5000:
                break
        return found

    def _instance(self, calendar: FakeCalendar, master: dict, original: datetime | date) -> dict:
        """`master`'s instance at `original`, as generated (or as edited on
        its own, if it's an exception)."""
        instance_id = f"{master['id']}_{_instance_suffix(original)}"
        stored = calendar.events.get(instance_id)
        if stored is not None:
            return self._under(master, stored)
        zone = calendar.zone
        start = _parse_moment(master["start"], zone)
        end = _parse_moment(master["end"], zone)
        time_zone = master["start"].get("timeZone")
        instance = {k: copy.deepcopy(v) for k, v in master.items() if k not in ("recurrence", "id", "start", "end")}
        instance["id"] = instance_id
        instance["recurringEventId"] = master["id"]
        instance["originalStartTime"] = _render_moment(original, zone, time_zone)
        instance["start"] = _render_moment(original, zone, time_zone)
        instance["end"] = _render_moment(original + (end - start), zone, master["end"].get("timeZone"))
        if master.get("status") == "cancelled":
            instance["status"] = "cancelled"
        return instance

    @staticmethod
    def _under(master: dict, exception: dict) -> dict:
        """An exception as its series shows it: cancelled with its series."""
        if master.get("status") == "cancelled" and exception.get("status") != "cancelled":
            return {**exception, "status": "cancelled"}
        return exception

    def _exceptions(self, calendar: FakeCalendar, master_id: str) -> list[dict]:
        return [e for e in calendar.events.values() if e.get("recurringEventId") == master_id]

    def _expanded(self, calendar: FakeCalendar, time_min: datetime | None, time_max: datetime | None, show_deleted: bool) -> list[dict]:
        zone = calendar.zone
        horizon = (time_max or datetime.now(UTC)) + _EXPANSION_HORIZON
        found = []
        for event in calendar.events.values():
            if event.get("recurringEventId"):
                continue  # Reached through its master.
            if event.get("recurrence"):
                seen = set()
                for original in self._occurrences(calendar, event, time_max or horizon):
                    instance = self._instance(calendar, event, original)
                    seen.add(instance["id"])
                    found.append(instance)
                # Exceptions moved into the range from outside it.
                for exception in self._exceptions(calendar, event["id"]):
                    if exception["id"] not in seen and self._is_occurrence(calendar, event, exception):
                        found.append(self._under(event, exception))
            else:
                found.append(event)
        listed = []
        for event in found:
            if event.get("status") == "cancelled" and not show_deleted:
                continue
            start = _parse_moment(event["start"], zone)
            end = _parse_moment(event["end"], zone)
            if _overlaps(start, end, zone, time_min, time_max):
                listed.append(event)
        listed.sort(key=lambda e: (_sort_key(_parse_moment(e["start"], zone), zone), e["id"]))
        return listed

    def _is_occurrence(self, calendar: FakeCalendar, master: dict, exception: dict) -> bool:
        original = _parse_moment(exception["originalStartTime"], calendar.zone)
        return original in self._occurrences(calendar, master, _sort_key(original, calendar.zone) + timedelta(seconds=1))

    def _find(self, calendar: FakeCalendar, event_id: str) -> tuple[dict, dict | None]:
        """(event, its master if it's an instance) for `event_id`, generated
        on the fly for an instance not edited on its own."""
        if event_id in calendar.events:
            event = calendar.events[event_id]
            master = calendar.events.get(event.get("recurringEventId") or "")
            return (self._under(master, event) if master else event), master
        match = re.fullmatch(r"(?P<master>.+)_(?P<when>\d{8}(T\d{6}Z)?)", event_id)
        if match and match["master"] in calendar.events:
            master = calendar.events[match["master"]]
            for original in self._occurrences(calendar, master, None if "T" not in match["when"] else datetime.strptime(match["when"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC) + timedelta(seconds=1)):
                if _instance_suffix(original) == match["when"]:
                    return self._instance(calendar, master, original), master
        raise GoogleError(404, "Not Found")

    # -- events: requests --

    def _present(self, calendar: FakeCalendar, event: dict) -> dict:
        """`event` as Google returns it: its times in its own zone, else
        the calendar's (which may have changed since it was written)."""
        event = copy.deepcopy(event)
        for key in ("start", "end", "originalStartTime"):
            if key in event:
                value = event[key]
                event[key] = _render_moment(_parse_moment(value, calendar.zone), calendar.zone, value.get("timeZone"))
        return event

    def _page(self, calendar: FakeCalendar, items: list[dict], max_results: str | None, page_token: str | None) -> dict:
        offset = int(page_token) if page_token else 0
        size = min(int(max_results or 250), self.page_cap)
        page = items[offset : offset + size]
        response: dict = {"kind": "calendar#events", "items": [self._present(calendar, e) for e in page]}
        if offset + size < len(items):
            response["nextPageToken"] = str(offset + size)
        return response

    def list_events(self, calendar_id: str, query: dict[str, list[str]]) -> dict:
        calendar = self.calendar(calendar_id)
        one = lambda name: query.get(name, [None])[0]
        if one("singleEvents") != "true":
            raise GoogleError(400, "fake: only singleEvents=true listings are supported")
        time_min = _parse_rfc3339(one("timeMin")) if one("timeMin") else None
        time_max = _parse_rfc3339(one("timeMax")) if one("timeMax") else None
        items = self._expanded(calendar, time_min, time_max, one("showDeleted") == "true")
        for prop in query.get("privateExtendedProperty", []):
            key, _, value = prop.partition("=")
            items = [e for e in items if e.get("extendedProperties", {}).get("private", {}).get(key) == value]
        response = self._page(calendar, items, one("maxResults"), one("pageToken"))
        response["timeZone"] = calendar.resource.get("timeZone", "UTC")
        return response

    def list_instances(self, calendar_id: str, event_id: str, query: dict[str, list[str]]) -> dict:
        calendar = self.calendar(calendar_id)
        master = calendar.events.get(event_id)
        if master is None or not master.get("recurrence"):
            raise GoogleError(404, "Not Found")
        one = lambda name: query.get(name, [None])[0]
        time_max = _parse_rfc3339(one("timeMax")) if one("timeMax") else None
        show_deleted = one("showDeleted") == "true"
        zone = calendar.zone
        items = []
        for original in self._occurrences(calendar, master, time_max or datetime.now(UTC) + _EXPANSION_HORIZON):
            instance = self._instance(calendar, master, original)
            if instance.get("status") == "cancelled" and not show_deleted:
                continue
            if time_max is not None and _sort_key(_parse_moment(instance["start"], zone), zone) >= time_max:
                continue
            items.append(instance)
        return self._page(calendar, items, one("maxResults"), one("pageToken"))

    def get_event(self, calendar_id: str, event_id: str) -> dict:
        calendar = self.calendar(calendar_id)
        event, _ = self._find(calendar, unquote(event_id))
        return self._present(calendar, event)

    def _new_event(self, calendar: FakeCalendar, body: dict, event_id: str) -> dict:
        event = copy.deepcopy(body)
        event["id"] = event_id
        event.setdefault("status", "confirmed")
        event.setdefault("iCalUID", f"{event_id}@google.com")
        event["kind"] = "calendar#event"
        event["etag"] = self._etag()
        private = event.get("extendedProperties", {}).get("private")
        if private is not None:
            event["extendedProperties"]["private"] = {k: v for k, v in private.items() if v is not None}
        self._check_event(calendar, event)
        return event

    def _check_event(self, calendar: FakeCalendar, event: dict) -> None:
        if "start" not in event or "end" not in event:
            raise GoogleError(400, "Missing end time.")
        zone = calendar.zone
        if _sort_key(_parse_moment(event["end"], zone), zone) < _sort_key(_parse_moment(event["start"], zone), zone):
            raise GoogleError(400, "The specified time range is empty.")
        if "colorId" in event:
            raise GoogleError(400, "fake: this server must never write colorId (it drops event labels)")

    def insert_event(self, calendar_id: str, body: dict) -> dict:
        calendar = self.calendar(calendar_id)
        event_id = body.get("id") or self._next_id("event")
        if not re.fullmatch(r"[a-v0-9]{5,1024}", event_id):
            raise GoogleError(400, "Invalid resource id value.")
        if event_id in calendar.events:
            raise GoogleError(409, "The requested identifier already exists.")
        # Inserting refuses a label the calendar doesn't have (an update
        # keeps one, as the label_lifecycle probe found).
        label = body.get("eventLabelId")
        if label is not None and not any(l.get("id") == label for l in calendar.resource.get("labelProperties", {}).get("eventLabels", [])):
            raise GoogleError(400, f"Invalid event label id: {label}. An event label with the specified id does not exist.")
        event = self._new_event(calendar, body, event_id)
        calendar.events[event_id] = event
        return self._present(calendar, event)

    def import_event(self, calendar_id: str, body: dict) -> dict:
        calendar = self.calendar(calendar_id)
        uid = body.get("iCalUID")
        if not uid:
            raise GoogleError(400, "Missing iCalUID.")
        existing = next((e for e in calendar.events.values() if e.get("iCalUID") == uid and not e.get("recurringEventId")), None)
        split = re.fullmatch(r"(?P<base>.+?)_R(?P<at>\d{8}T\d{6})@google\.com", uid)
        if existing is not None:
            if split is None or existing.get("status") != "cancelled":
                if split is None:
                    return self.update_event(calendar_id, existing["id"], {k: v for k, v in body.items() if k != "iCalUID"})
                return self._present(calendar, existing)
        if split is None:
            event = self._new_event(calendar, body, self._next_id("import"))
            calendar.events[event["id"]] = event
            return self._present(calendar, event)
        # Google's own "this and following" split, as the series_splits probe
        # found it: the series (or its latest part) is ended at the local
        # midnight before the split's day, and the rest is a new series.
        base_id = split["base"]
        at = datetime.strptime(split["at"], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
        parts_of = [e for e in calendar.events.values() if not e.get("recurringEventId") and (e["id"] == base_id or e["id"].startswith(base_id + "_R"))]
        if not parts_of:
            raise GoogleError(404, "Not Found")
        zone = calendar.zone
        earlier = [p for p in parts_of if _sort_key(_parse_moment(p["start"], zone), zone) < at]
        if not earlier:
            raise GoogleError(400, "Bad Request")
        current = max(earlier, key=lambda p: _sort_key(_parse_moment(p["start"], zone), zone))
        local_zone = ZoneInfo(current["start"].get("timeZone")) if current["start"].get("timeZone") else zone
        day_before = at.astimezone(local_zone).date() - timedelta(days=1)
        until = datetime.combine(day_before, time(23, 59, 59), local_zone).astimezone(UTC)
        lines, parts = self._rules(current)
        new_id = f"{base_id}_R{split['at']}"
        moved = [e for e in self._exceptions(calendar, current["id"]) if _sort_key(_parse_moment(e["originalStartTime"], zone), zone) >= at]
        ended = {k: v for k, v in (parts or {}).items() if k not in ("COUNT", "UNTIL")}
        ended["UNTIL"] = until.strftime("%Y%m%dT%H%M%SZ")
        current["recurrence"] = [("RRULE:" + ";".join(f"{k}={v}" for k, v in ended.items())) if line.startswith("RRULE:") else line for line in lines]
        current["etag"] = self._etag()
        rest = self._new_event(calendar, {k: v for k, v in body.items() if k != "iCalUID"}, new_id)
        rest["iCalUID"] = uid
        calendar.events[new_id] = rest
        for exception in moved:
            del calendar.events[exception["id"]]
            original = _parse_moment(exception["originalStartTime"], zone)
            exception["id"] = f"{new_id}_{_instance_suffix(original)}"
            exception["recurringEventId"] = new_id
            calendar.events[exception["id"]] = exception
        return self._present(calendar, rest)

    def update_event(self, calendar_id: str, event_id: str, body: dict) -> dict:
        """PUT: replace the event in full."""
        calendar = self.calendar(calendar_id)
        event_id = unquote(event_id)
        current, master = self._find(calendar, event_id)
        replacement = copy.deepcopy(body)
        for kept in ("recurringEventId", "originalStartTime", "iCalUID"):
            if kept in current and kept not in replacement:
                replacement[kept] = current[kept]
        if current.get("recurrence") is not None or "recurrence" in replacement:
            self._check_rule_change(calendar, current, replacement.get("recurrence"))
        event = self._new_event(calendar, replacement, event_id)
        self._apply_master_change(calendar, current, event)
        calendar.events[event_id] = event
        return self._present(calendar, event)

    def patch_event(self, calendar_id: str, event_id: str, body: dict) -> dict:
        calendar = self.calendar(calendar_id)
        event_id = unquote(event_id)
        current, master = self._find(calendar, event_id)
        patched = copy.deepcopy(current)
        for key, value in body.items():
            if key in ("id", "etag", "kind"):
                continue
            if key == "extendedProperties":
                for scope, props in (value or {}).items():
                    existing = patched.setdefault("extendedProperties", {}).setdefault(scope, {})
                    for prop, prop_value in (props or {}).items():
                        if prop_value is None:
                            if prop not in existing and master is not None:
                                # What Calendar answers removing a property an
                                # instance doesn't have (see CalendarClient.update_event).
                                raise GoogleError(400, "Required")
                            existing.pop(prop, None)
                        else:
                            existing[prop] = prop_value
            elif value is None:
                patched.pop(key, None)
            else:
                patched[key] = copy.deepcopy(value)
        if "recurrence" in body:
            self._check_rule_change(calendar, current, body["recurrence"])
        self._check_event(calendar, patched)
        patched["etag"] = self._etag()
        self._apply_master_change(calendar, current, patched, times_changed="start" in body or "end" in body)
        calendar.events[event_id] = patched
        return self._present(calendar, patched)

    def _check_rule_change(self, calendar: FakeCalendar, current: dict, rules: list[str] | None) -> None:
        """An "_R" series Google split itself can't have its rules changed:
        it's deleted instead, or the change fails if its first event is
        cancelled (the series_splits probe)."""
        if not re.search(r"_R\d{8}T\d{6}$", current.get("id", "")) or rules == current.get("recurrence"):
            return
        first = self._occurrences(calendar, current, None)[:1]
        if first and self._instance(calendar, current, first[0]).get("status") == "cancelled":
            raise GoogleError(400, "Bad Request")
        raise _SeriesDeleted()

    def _apply_master_change(self, calendar: FakeCalendar, before: dict, after: dict, *, times_changed: bool | None = None) -> None:
        """What a write to a series' master does to its exceptions
        (the series_edits and series_deletes probes)."""
        if not before.get("recurrence") or before.get("recurringEventId"):
            return
        zone = calendar.zone
        exceptions = self._exceptions(calendar, before["id"])
        changed = {k for k in set(before) | set(after) if k != "etag" and before.get(k) != after.get(k)}
        if changed <= {"status"}:
            return  # Cancelled or restored: its instances follow (see _under).
        if times_changed is None:
            times_changed = before.get("start") != after.get("start") or before.get("end") != after.get("end")
        if times_changed:
            # Every instance moves onto the series' new times; exceptions go,
            # but one cancelled on its own stays cancelled at the same place
            # in the series.
            old = self._occurrences(calendar, before, None)
            new = self._occurrences(calendar, after, None)
            for exception in exceptions:
                del calendar.events[exception["id"]]
                if exception.get("status") != "cancelled":
                    continue
                original = _parse_moment(exception["originalStartTime"], zone)
                if original in old and old.index(original) < len(new):
                    moved = self._instance(calendar, after, new[old.index(original)])
                    moved["status"] = "cancelled"
                    calendar.events[moved["id"]] = moved
            return
        if before.get("recurrence") != after.get("recurrence"):
            # Ended early: instances (and exceptions, by original start) past
            # the new end are gone outright.
            kept = set(map(_instance_suffix, self._occurrences(calendar, after, None)))
            for exception in exceptions:
                original = _parse_moment(exception["originalStartTime"], zone)
                if _instance_suffix(original) not in kept:
                    del calendar.events[exception["id"]]
            exceptions = self._exceptions(calendar, before["id"])
        # Any other change resets every exception's fields but its times
        # (and a cancelled one stays cancelled).
        for exception in exceptions:
            reset = {k: copy.deepcopy(v) for k, v in after.items() if k not in ("recurrence", "id", "start", "end", "etag", "status")}
            for k in ("id", "start", "end", "recurringEventId", "originalStartTime", "status"):
                if k in exception:
                    reset[k] = exception[k]
            calendar.events[exception["id"]] = reset

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        calendar = self.calendar(calendar_id)
        event_id = unquote(event_id)
        event, master = self._find(calendar, event_id)
        if event.get("status") == "cancelled":
            raise GoogleError(410, "Resource has been deleted")
        if re.search(r"_R\d{8}T\d{6}$", event_id) and event.get("recurrence"):
            first = self._occurrences(calendar, event, None)[:1]
            if first and self._instance(calendar, event, first[0]).get("status") == "cancelled":
                raise GoogleError(400, "Bad Request")
        cancelled = copy.deepcopy(event)
        cancelled["status"] = "cancelled"
        cancelled["etag"] = self._etag()
        calendar.events[event_id] = cancelled

    def delete_series_as_split(self, calendar: FakeCalendar, event_id: str) -> dict:
        event = calendar.events[event_id]
        event["status"] = "cancelled"
        event["recurrence"] = ["RRULE:FREQ=DAILY;COUNT=1"]
        event["etag"] = self._etag()
        for exception in self._exceptions(calendar, event_id):
            del calendar.events[exception["id"]]
        return self._present(calendar, event)

    # -- sheets --

    def _spreadsheet(self, spreadsheet_id: str) -> dict:
        self._sheets_request()
        try:
            return self.spreadsheets[spreadsheet_id]
        except KeyError:
            raise GoogleError(404, "Requested entity was not found.") from None

    def _sheets_request(self) -> None:
        if self.rate_limit_next > 0:
            self.rate_limit_next -= 1
            raise GoogleError(429, "Quota exceeded for quota metric 'Read requests'.")

    def create_spreadsheet(self, body: dict) -> dict:
        self._sheets_request()
        spreadsheet_id = self._next_id("sheet") + "x" * 18
        self.spreadsheets[spreadsheet_id] = {
            "spreadsheetId": spreadsheet_id,
            "properties": {"title": body.get("properties", {}).get("title", "Untitled spreadsheet")},
            "sheets": [_new_tab(0, "Sheet1")],
            "developerMetadata": [],
        }
        return {"spreadsheetId": spreadsheet_id}

    def get_spreadsheet(self, spreadsheet_id: str) -> dict:
        spreadsheet = self._spreadsheet(spreadsheet_id)
        return {"sheets": [{"properties": copy.deepcopy(tab["properties"])} for tab in spreadsheet["sheets"]]}

    def _tab(self, spreadsheet: dict, sheet_id: int | None = None, title: str | None = None) -> dict:
        for tab in spreadsheet["sheets"]:
            if sheet_id is not None and tab["properties"]["sheetId"] == sheet_id:
                return tab
            if title is not None and tab["properties"]["title"] == title:
                return tab
        if title is not None:
            raise GoogleError(400, f"Unable to parse range: {title}")
        raise GoogleError(400, f"No grid with id: {sheet_id}")

    def batch_update(self, spreadsheet_id: str, body: dict) -> dict:
        spreadsheet = self._spreadsheet(spreadsheet_id)
        replies = []
        for request in body.get("requests", []):
            (kind, args), = request.items()
            if kind == "addSheet":
                properties = args.get("properties", {})
                if any(t["properties"]["title"] == properties.get("title") for t in spreadsheet["sheets"]):
                    raise GoogleError(400, f"A sheet with the name \"{properties.get('title')}\" already exists.")
                sheet_id = 1_000_000 + next(self._ids)
                tab = _new_tab(sheet_id, properties.get("title", f"Sheet{len(spreadsheet['sheets']) + 1}"))
                if "tabColor" in properties:
                    tab["properties"]["tabColor"] = properties["tabColor"]
                spreadsheet["sheets"].append(tab)
                replies.append({"addSheet": {"properties": copy.deepcopy(tab["properties"])}})
            elif kind == "updateSheetProperties":
                tab = self._tab(spreadsheet, args["properties"]["sheetId"])
                for name in args["fields"].split(","):
                    tab["properties"][name] = args["properties"][name]
                replies.append({})
            elif kind == "updateSpreadsheetProperties":
                for name in args["fields"].split(","):
                    spreadsheet["properties"][name] = args["properties"][name]
                replies.append({})
            elif kind == "createDeveloperMetadata":
                metadata = dict(args["developerMetadata"])
                metadata["metadataId"] = next(self._ids)
                self._tab(spreadsheet, metadata["location"]["sheetId"])
                spreadsheet["developerMetadata"].append(metadata)
                replies.append({"createDeveloperMetadata": {"developerMetadata": metadata}})
            elif kind == "updateDimensionProperties":
                tab = self._tab(spreadsheet, args["range"]["sheetId"])
                for column in range(args["range"]["startIndex"], args["range"]["endIndex"]):
                    tab["columnWidths"][str(column)] = args["properties"]["pixelSize"]
                replies.append({})
            elif kind == "deleteDimension":
                rng = args["range"]
                tab = self._tab(spreadsheet, rng["sheetId"])
                if rng["dimension"] != "ROWS":
                    raise GoogleError(400, "fake: only ROWS")
                start, end = rng["startIndex"], rng["endIndex"]
                count = end - start
                tab["cells"] = {(r - count if r >= end else r, c): v for (r, c), v in tab["cells"].items() if not start <= r < end}
                tab["properties"]["gridProperties"]["rowCount"] -= count
                replies.append({})
            elif kind == "appendDimension":
                tab = self._tab(spreadsheet, args["sheetId"])
                tab["properties"]["gridProperties"]["rowCount"] += args["length"]
                replies.append({})
            else:
                raise GoogleError(400, f"fake: unsupported request {kind}")
        response: dict = {"spreadsheetId": spreadsheet_id, "replies": replies}
        if body.get("includeSpreadsheetInResponse"):
            response["updatedSpreadsheet"] = {"sheets": [{"properties": copy.deepcopy(t["properties"])} for t in spreadsheet["sheets"]]}
        return response

    def search_metadata(self, spreadsheet_id: str, body: dict) -> dict:
        spreadsheet = self._spreadsheet(spreadsheet_id)
        matched = []
        for data_filter in body.get("dataFilters", []):
            lookup = data_filter.get("developerMetadataLookup", {})
            for metadata in spreadsheet["developerMetadata"]:
                if all(metadata.get(k) == v for k, v in lookup.items()):
                    matched.append({"developerMetadata": copy.deepcopy(metadata), "dataFilters": [data_filter]})
        return {"matchedDeveloperMetadata": matched} if matched else {}

    def _a1(self, spreadsheet: dict, rng: str) -> tuple[dict, int, int, int | None, int | None]:
        """(tab, first row, first column, end row, end column) -- 0-based,
        ends exclusive or None for open -- for an A1 range."""
        title, _, cells = rng.rpartition("!")
        if title.startswith("'") and title.endswith("'"):
            title = title[1:-1].replace("''", "'")
        tab = self._tab(spreadsheet, title=title) if title else spreadsheet["sheets"][0]
        match = re.fullmatch(r"([A-Z]*)(\d*)(?::([A-Z]*)(\d*))?", cells)
        if not match:
            raise GoogleError(400, f"Unable to parse range: {rng}")
        c1, r1, c2, r2 = match.groups()
        start_col = _column_index(c1) if c1 else 0
        start_row = int(r1) - 1 if r1 else 0
        if match.group(3) is None and match.group(4) is None:
            end_col, end_row = (start_col + 1 if c1 else None), (start_row + 1 if r1 else None)
        else:
            end_col = _column_index(c2) + 1 if c2 else None
            end_row = int(r2) if r2 else None
        return tab, start_row, start_col, end_row, end_col

    def get_values(self, spreadsheet_id: str, rng: str) -> dict:
        spreadsheet = self._spreadsheet(spreadsheet_id)
        tab, r0, c0, r1, c1 = self._a1(spreadsheet, unquote(rng))
        response = {"range": unquote(rng), "majorDimension": "ROWS"}
        values = _read(tab, r0, c0, r1, c1)
        if values:
            response["values"] = values
        return response

    def update_values(self, spreadsheet_id: str, rng: str, body: dict) -> dict:
        spreadsheet = self._spreadsheet(spreadsheet_id)
        tab, r0, c0, _, _ = self._a1(spreadsheet, unquote(rng))
        rows = body.get("values", [])
        grid = tab["properties"]["gridProperties"]
        grid["rowCount"] = max(grid["rowCount"], r0 + len(rows))
        cells = _write(tab, r0, c0, rows)
        return {"spreadsheetId": spreadsheet_id, "updatedRange": unquote(rng), "updatedRows": len(rows), "updatedCells": cells}

    def batch_get_by_filter(self, spreadsheet_id: str, body: dict) -> dict:
        spreadsheet = self._spreadsheet(spreadsheet_id)
        value_ranges = []
        for data_filter in body.get("dataFilters", []):
            grid = data_filter["gridRange"]
            tab = self._tab(spreadsheet, grid.get("sheetId", 0))
            values = _read(tab, grid.get("startRowIndex", 0), grid.get("startColumnIndex", 0), grid.get("endRowIndex"), grid.get("endColumnIndex"))
            value_range = {"range": tab["properties"]["title"], "majorDimension": "ROWS"}
            if values:
                value_range["values"] = values
            value_ranges.append({"valueRange": value_range, "dataFilters": [data_filter]})
        return {"spreadsheetId": spreadsheet_id, "valueRanges": value_ranges}

    def batch_update_by_filter(self, spreadsheet_id: str, body: dict) -> dict:
        spreadsheet = self._spreadsheet(spreadsheet_id)
        total = 0
        for data in body.get("data", []):
            grid = data["dataFilter"]["gridRange"]
            tab = self._tab(spreadsheet, grid.get("sheetId", 0))
            rows = data.get("values", [])
            r0 = grid.get("startRowIndex", 0)
            row_count = tab["properties"]["gridProperties"]["rowCount"]
            end = min(grid.get("endRowIndex", row_count), row_count)
            if r0 + len(rows) > end:
                raise GoogleError(400, f"Requested writing within range [{tab['properties']['title']}], but tried writing to row [{r0 + len(rows)}]")
            total += _write(tab, r0, grid.get("startColumnIndex", 0), rows)
        return {"spreadsheetId": spreadsheet_id, "totalUpdatedCells": total}

    # -- state, for comparing --

    def snapshot(self, *, skip_tabs: tuple[str, ...] = ()) -> dict:
        """Everything stored, in a stable, JSON-able form: calendars with
        their events, and spreadsheets with their tabs' cells -- except
        tabs titled in `skip_tabs`."""
        with self._lock:
            calendars = {}
            for calendar_id, calendar in sorted(self.calendars.items()):
                resource = {k: v for k, v in calendar.resource.items() if k != "etag"}
                events = {eid: {k: v for k, v in e.items() if k != "etag"} for eid, e in sorted(calendar.events.items())}
                calendars[calendar_id] = {"resource": resource, "events": events}
            spreadsheets = {}
            for spreadsheet_id, spreadsheet in sorted(self.spreadsheets.items()):
                tabs = {}
                for tab in spreadsheet["sheets"]:
                    title = tab["properties"]["title"]
                    if title in skip_tabs:
                        continue
                    tabs[title] = {
                        "properties": tab["properties"],
                        "column_widths": tab["columnWidths"],
                        "rows": _read(tab, 0, 0, None, None),
                    }
                metadata = [{k: v for k, v in m.items() if k != "metadataId"} for m in spreadsheet["developerMetadata"]]
                spreadsheets[spreadsheet_id] = {"properties": spreadsheet["properties"], "tabs": tabs, "developer_metadata": metadata}
            return json.loads(json.dumps({"calendars": calendars, "spreadsheets": spreadsheets}, default=str))

    # -- HTTP --

    def app(self) -> Starlette:
        routes = [
            Route("/token", self._token, methods=["POST"]),
            # WorkOS AuthKit, for the HTTP transport (see conformance/test_http.py).
            Route("/oauth2/jwks", self._jwks, methods=["GET"]),
            Route("/oauth2/register", self._authkit_echo, methods=["POST"]),
            Route("/oauth2/token", self._authkit_echo, methods=["POST"]),
            Route("/batch/calendar/v3", self._batch, methods=["POST"]),
            Route("/calendar/v3/calendars", self._handler(lambda r, b: self.insert_calendar(b)), methods=["POST"]),
            Route("/calendar/v3/calendars/{cid}", self._calendar_route, methods=["GET", "PATCH"]),
            Route("/calendar/v3/users/me/calendarList/{cid}", self._handler(lambda r, b: {"id": unquote(r.path_params["cid"]), "hidden": True}), methods=["PATCH"]),
            Route("/calendar/v3/calendars/{cid}/events", self._events_route, methods=["GET", "POST"]),
            Route("/calendar/v3/calendars/{cid}/events/import", self._handler(lambda r, b: self.import_event(r.path_params["cid"], b)), methods=["POST"]),
            Route("/calendar/v3/calendars/{cid}/events/{eid}/instances", self._handler(lambda r, b: self.list_instances(r.path_params["cid"], unquote(r.path_params["eid"]), _query(r))), methods=["GET"]),
            Route("/calendar/v3/calendars/{cid}/events/{eid}", self._event_route, methods=["GET", "PUT", "PATCH", "DELETE"]),
            Route("/v4/spreadsheets", self._handler(lambda r, b: self.create_spreadsheet(b)), methods=["POST"]),
            Route("/v4/spreadsheets/{sid}", self._handler(lambda r, b: self.get_spreadsheet(r.path_params["sid"])), methods=["GET"]),
            Route("/v4/spreadsheets/{sid}:batchUpdate", self._handler(lambda r, b: self.batch_update(r.path_params["sid"], b)), methods=["POST"]),
            Route("/v4/spreadsheets/{sid}/developerMetadata:search", self._handler(lambda r, b: self.search_metadata(r.path_params["sid"], b)), methods=["POST"]),
            Route("/v4/spreadsheets/{sid}/values:batchGetByDataFilter", self._handler(lambda r, b: self.batch_get_by_filter(r.path_params["sid"], b)), methods=["POST"]),
            Route("/v4/spreadsheets/{sid}/values:batchUpdateByDataFilter", self._handler(lambda r, b: self.batch_update_by_filter(r.path_params["sid"], b)), methods=["POST"]),
            Route("/v4/spreadsheets/{sid}/values/{rng:path}", self._values_route, methods=["GET", "PUT"]),
        ]
        return Starlette(routes=routes)

    jwks: dict = {"keys": []}
    """AuthKit's signing keys, as /oauth2/jwks serves them."""

    async def _jwks(self, request: Request) -> Response:
        return JSONResponse(self.jwks)

    async def _authkit_echo(self, request: Request) -> Response:
        """AuthKit's registration and token endpoints, as far as the
        server's OAuth proxy can tell: what it was sent, echoed back -- a
        400 for a body asking for one."""
        raw = await request.body()
        self.authkit_requests.append({"path": request.url.path, "content_type": request.headers.get("content-type"), "user_agent": request.headers.get("user-agent"), "body": raw.decode()})
        status = 400 if b"invalid" in raw else 201
        return JSONResponse({"echo": raw.decode(), "path": request.url.path}, status_code=status, headers={"Cache-Control": "no-store", "X-Dropped": "yes"})

    async def _token(self, request: Request) -> Response:
        self.token_requests += 1
        return JSONResponse({"access_token": f"fake-access-{self.token_requests}", "expires_in": 3600, "token_type": "Bearer", "scope": "https://www.googleapis.com/auth/calendar.app.created https://www.googleapis.com/auth/drive.file"})

    def _serve(self, request, method: str, path: str, query: dict, if_match: str | None, body: Any, fn, batched: bool = False) -> tuple[int, Any]:
        """Log a request, fail it if a scenario asked for that, else answer
        it with `fn`: (status, the JSON answer, or None for none)."""
        with self._lock:
            self.requests.append(LoggedRequest(method, path, query, if_match, body, batched))
            failing = next((f for f in self.fail_next if f[0] == method and f[1] in path), None)
            if failing is not None and len(failing) > 4 and failing[4] > 0:
                self.fail_next[self.fail_next.index(failing)] = (*failing[:4], failing[4] - 1)
            elif failing is not None:
                self.fail_next.remove(failing)
                return failing[2], GoogleError(failing[2], failing[3]).payload()
            try:
                result = fn(request, body or {})
            except _SeriesDeleted:
                calendar = self.calendar(request.path_params["cid"])
                result = self.delete_series_as_split(calendar, unquote(request.path_params["eid"]))
            except GoogleError as error:
                return error.status, error.payload()
        return (204, None) if result is None else (200, result)

    def _handler(self, fn):
        async def handle(request: Request) -> Response:
            raw = await request.body()
            body = json.loads(raw) if raw else None
            if not request.headers.get("authorization", "").startswith("Bearer "):
                return GoogleError(401, "Request is missing required authentication credential.").response()
            status, answer = self._serve(request, request.method, unquote(request.url.path), _query(request), request.headers.get("if-match"), body, fn)
            return Response(status_code=204) if answer is None else JSONResponse(answer, status_code=status)

        return handle

    def _batch_route(self, method: str, path: str):
        """The handler for a request inside a Calendar batch, and its path
        parameters -- of the event requests the server batches."""
        events = re.fullmatch(r"/calendar/v3/calendars/([^/]+)/events", path)
        if events and method == "POST":
            cid = events.group(1)
            return (lambda r, b: self.insert_event(cid, b)), {"cid": cid}
        event = re.fullmatch(r"/calendar/v3/calendars/([^/]+)/events/([^/]+)", path)
        if event:
            cid, eid = event.groups()
            handlers = {
                "GET": lambda r, b: self.get_event(cid, eid),
                "PUT": lambda r, b: self.update_event(cid, eid, b),
                "PATCH": lambda r, b: self.patch_event(cid, eid, b),
                "DELETE": lambda r, b: self.delete_event(cid, eid),
            }
            if method in handlers:
                return handlers[method], {"cid": cid, "eid": eid}
        return None, {}

    async def _batch(self, request: Request) -> Response:
        """Calendar's batch endpoint: a multipart/mixed body of requests,
        each answered (and logged) as if it came on its own."""
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return GoogleError(401, "Request is missing required authentication credential.").response()
        raw = (await request.body()).decode()
        boundary = request.headers.get("content-type", "").split("boundary=")[-1].strip('"')
        answers = []
        for content_id, method, target, if_match, body in _batch_parts(raw, boundary):
            path, _, query_text = target.partition("?")
            query: dict[str, list[str]] = {}
            for key, value in parse_qsl(query_text, keep_blank_values=True):
                if key not in _DROPPED_QUERY:
                    query.setdefault(key, []).append(value)
            fn, params = self._batch_route(method, path)
            if fn is None:
                status, answer = 404, GoogleError(404, f"fake: no batch route for {method} {path}").payload()
            else:
                part = SimpleNamespace(path_params=params, headers={"if-match": if_match})
                status, answer = self._serve(part, method, unquote(path), dict(sorted(query.items())), if_match, body, fn, batched=True)
            text = "" if answer is None else json.dumps(answer)
            answers.append(
                f"--batch_fake\r\nContent-Type: application/http\r\nContent-ID: <response-{content_id}>\r\n\r\n"
                f"HTTP/1.1 {status} {'OK' if status < 400 else 'Error'}\r\nContent-Type: application/json\r\n\r\n{text}\r\n"
            )
        answers.append("--batch_fake--\r\n")
        return Response("".join(answers), media_type="multipart/mixed; boundary=batch_fake")

    async def _calendar_route(self, request: Request) -> Response:
        if request.method == "GET":
            return await self._handler(lambda r, b: self.get_calendar(r.path_params["cid"]))(request)
        return await self._handler(lambda r, b: self.patch_calendar(r.path_params["cid"], b, r.headers.get("if-match")))(request)

    async def _events_route(self, request: Request) -> Response:
        if request.method == "GET":
            return await self._handler(lambda r, b: self.list_events(r.path_params["cid"], _query(r)))(request)
        return await self._handler(lambda r, b: self.insert_event(r.path_params["cid"], b))(request)

    async def _event_route(self, request: Request) -> Response:
        cid, eid = request.path_params["cid"], request.path_params["eid"]
        handlers = {
            "GET": lambda r, b: self.get_event(cid, eid),
            "PUT": lambda r, b: self.update_event(cid, eid, b),
            "PATCH": lambda r, b: self.patch_event(cid, eid, b),
            "DELETE": lambda r, b: self.delete_event(cid, eid),
        }
        return await self._handler(handlers[request.method])(request)

    async def _values_route(self, request: Request) -> Response:
        sid, rng = request.path_params["sid"], request.path_params["rng"]
        if request.method == "GET":
            return await self._handler(lambda r, b: self.get_values(sid, rng))(request)
        return await self._handler(lambda r, b: self.update_values(sid, rng, b))(request)


class _SeriesDeleted(Exception):
    """A rule change to an "_R" series: Google deletes it instead."""


def _batch_parts(text: str, boundary: str) -> list[tuple[str, str, str, str | None, Any]]:
    """A batch's requests: (Content-ID, method, target, If-Match, JSON body)."""
    parts = []
    for part in text.split(f"--{boundary}")[1:]:
        if part.startswith("--"):
            break
        outer, _, http = part.lstrip("\r\n").partition("\r\n\r\n")
        headers_of = lambda lines: {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines.splitlines()) if v}
        content_id = headers_of(outer).get("content-id", "").strip("<>")
        head, _, body = http.partition("\r\n\r\n")
        request_line, _, headers = head.partition("\r\n")
        method, target = (request_line.split() + ["", ""])[:2]
        body = body.strip()
        parts.append((content_id, method, target, headers_of(headers).get("if-match"), json.loads(body) if body else None))
    return parts


def _query(request: Request) -> dict[str, list[str]]:
    query: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        if key not in _DROPPED_QUERY:
            query.setdefault(key, []).append(value)
    return dict(sorted(query.items()))


def _new_tab(sheet_id: int, title: str) -> dict:
    return {
        "properties": {"sheetId": sheet_id, "title": title, "index": 0, "sheetType": "GRID", "gridProperties": {"rowCount": 1000, "columnCount": 26}},
        "cells": {},
        "columnWidths": {},
    }


def _column_index(letters: str) -> int:
    n = 0
    for letter in letters:
        n = n * 26 + ord(letter) - ord("A") + 1
    return n - 1


def _cell_text(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if value is None:
        return ""
    return str(value)


def _read(tab: dict, r0: int, c0: int, r1: int | None, c1: int | None) -> list[list[str]]:
    """Cells in the range as Sheets returns them: trailing empty cells and
    rows left off."""
    cells = tab["cells"]
    if not cells:
        return []
    last_row = max(r for r, _ in cells) + 1
    last_col = max(c for _, c in cells) + 1
    rows = []
    for r in range(r0, min(r1 if r1 is not None else last_row, last_row)):
        row = [cells.get((r, c), "") for c in range(c0, min(c1 if c1 is not None else last_col, last_col))]
        while row and row[-1] == "":
            row.pop()
        rows.append(row)
    while rows and not rows[-1]:
        rows.pop()
    return rows


def _write(tab: dict, r0: int, c0: int, rows: list[list[Any]]) -> int:
    count = 0
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            text = _cell_text(value)
            if text == "":
                tab["cells"].pop((r0 + i, c0 + j), None)
            else:
                tab["cells"][(r0 + i, c0 + j)] = text
            count += 1
    return count
