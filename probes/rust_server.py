"""Run the Rust server (rust/) against Google for real, over MCP: the
conformance scenarios hold it to the Python server's behavior against a
fake Google; this checks the real one answers it the same way -- OAuth,
Calendar and Sheets, labels, developer metadata -- before it's deployed.

It creates a throwaway calendar, seeds three planned days on it, starts the
server over stdio against it (with a copy of your token, so a refresh
can't touch the real one), and walks a compaction through: actions,
people and locations, notes, a proposal, the user's edit, confirming it,
judging it -- then the event tools, a recurring series, and health. Each
step prints what came back; a failure stops it. Then it deletes the
calendar. Its metadata spreadsheet is left in Drive: this app can't
trash it (see probes/metadata_tabs.py).

Usage:
    cargo build --manifest-path rust/Cargo.toml
    python -m probes.rust_server [--server rust/target/debug/ctt-server] [--keep]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from calendar_clients.google_auth import load_credentials
from config import get_credentials_path, get_token_path

_SCRATCH_TITLE = "CTT Rust probe (safe to delete)"
_ZONE = "America/New_York"
_SLEEP_KEY = "cascading-time-tracker-is_end_of_day_sleep"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default = Path("rust/target/debug") / ("ctt-server.exe" if os.name == "nt" else "ctt-server")
    parser.add_argument("--server", default=str(default), help="The Rust server binary")
    parser.add_argument("--keep", action="store_true", help="Keep the scratch calendar")
    args = parser.parse_args()

    creds = load_credentials(get_token_path(), get_credentials_path())
    service = build("calendar", "v3", credentials=creds)
    calendar_id = service.calendars().insert(body={"summary": _SCRATCH_TITLE, "timeZone": _ZONE}).execute()["id"]
    print(f"Scratch calendar: {calendar_id}")
    try:
        days = _seed(service, calendar_id)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            token = Path(tmp) / "token.json"
            shutil.copy(get_token_path(), token)
            env = {
                **os.environ,
                "GOOGLE_CALENDAR_ID": calendar_id,
                "GOOGLE_OAUTH_TOKEN_PATH": str(token),
                "GOOGLE_OAUTH_CREDENTIALS_PATH": str(Path(get_credentials_path()).resolve()),
                "MCP_TRANSPORT": "stdio",
                "CTT_HEALTH_WRITER": "off",
            }
            asyncio.run(_walk(Path(args.server).resolve(), env, days))
        print("All steps passed.")
    finally:
        if args.keep:
            print(f"Kept {calendar_id}")
        else:
            service.calendars().delete(calendarId=calendar_id).execute()
            print("Deleted the scratch calendar.")


def _seed(service, calendar_id: str) -> dict[date, dict[str, str]]:
    """The three days before today (in New York), planned as conformance/
    seed.py plans them: the first for the night before the other two."""
    today = datetime.now(ZoneInfo(_ZONE)).date()
    ids: dict[date, dict[str, str]] = {}
    for offset in (3, 2, 1):
        day = today - timedelta(days=offset)

        def add(start: str, end: str, summary: str, *, next_day: bool = False, sleep: bool = False) -> str:
            end_day = day + timedelta(days=1) if next_day else day
            body = {
                "summary": summary,
                "start": {"dateTime": f"{day.isoformat()}T{start}:00", "timeZone": _ZONE},
                "end": {"dateTime": f"{end_day.isoformat()}T{end}:00", "timeZone": _ZONE},
            }
            if sleep:
                body["extendedProperties"] = {"private": {_SLEEP_KEY: "true"}}
            return service.events().insert(calendarId=calendar_id, body=body).execute()["id"]

        ids[day] = {
            "Breakfast": add("07:00", "07:30", "Breakfast"),
            "Work": add("08:00", "12:00", "Work"),
            "Lunch": add("12:00", "13:00", "Lunch"),
            "Dinner": add("18:00", "19:00", "Dinner"),
            "Sleep": add("22:30", "06:30", "Sleep", next_day=True, sleep=True),
        }
    return ids


async def _walk(server: Path, env: dict[str, str], days: dict[date, dict[str, str]]) -> None:
    first, second = sorted(days)[1:]
    d1, d2 = days[first], days[second]

    def at(day: date, hhmm: str) -> str:
        return datetime.fromisoformat(f"{day.isoformat()}T{hhmm}:00").replace(tzinfo=ZoneInfo(_ZONE)).isoformat()

    params = StdioServerParameters(command=str(server), args=[], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        print(f"{len(tools.tools)} tools")

        async def call(tool: str, **arguments):
            result = await session.call_tool(tool, arguments)
            text = "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")
            if result.is_error:
                raise SystemExit(f"{tool} failed: {text}")
            content = result.structured_content
            if isinstance(content, dict) and set(content) == {"result"}:
                content = content["result"]
            shown = json.dumps(content, ensure_ascii=False)
            print(f"  {tool}: {shown[:200]}{'...' if len(shown) > 200 else ''}")
            return content

        print("Stores")
        eat = (await call("create_action", action={"name": "eat a meal", "status": "active"}))["created_id"]
        await call("create_action_group", group={"name": "Meals", "priority": 1})
        friend = (await call("create_person", person={"name": "Probe Friend", "what_matters": "good food"}))["created_id"]
        cafe = (await call("create_location", location={"name": "Probe Cafe", "hint": "the cafe"}))["created_id"]
        await call("create_trait", trait={"name": "Probe close", "parts": [{"kind": "judgment", "rubric": "How close?", "ratings": {"0": "apart", "1": "close"}, "facts": ["general_notes"]}]})
        await call("create_habit", habit={"name": "Probe meals", "action_id": eat})
        await call("get_actions")
        await call("get_people")
        await call("get_priority_colors")

        print("Events")
        await call("list_events", min_time=at(first, "00:00"), max_time=at(second, "23:59"))
        await call("update_event", updates=[{"event": {"id": d1["Breakfast"], "action_ids": [eat]}}])
        created = await call("create_event", events=[{"summary": "Probe walk", "start": at(second, "19:15"), "end": at(second, "19:45")}])
        walk = created["events"][0]["id"] if isinstance(created, dict) else created[0]["id"]
        await call("get_event", id=walk)
        await call("delete_event", cancels=[{"event_id": walk, "counts_against_follow_through": False}])

        print("Compaction")
        for when, text in ((at(first, "07:45"), "finished breakfast"), (at(first, "12:30"), "lunch with the friend at the cafe"), (at(second, "19:20"), "dinner done")):
            await call("note", noted_time={"timestamp": when, "description": text})
        context = await call("prepare_compaction")
        notes = {n["description"]: n["id"] for n in context["notes"]}
        proposed = await call(
            "compact_notes",
            recap={"overview": "Probe.", "did": [{"text": "Recorded the probe days.", "event_ids": [], "note_ids": []}], "unsure": []},
            updates=[
                {"event_id": d1["Breakfast"], "end_note": notes["finished breakfast"], "action_ids": [eat]},
                {"event_id": d1["Lunch"], "action_ids": [eat], "facts": {"with_ids": [friend], "location_id": cafe, "notes": {friend: "chatty"}}},
                {"event_id": d2["Dinner"], "end_note": notes["dinner done"]},
            ],
            creates=[{"summary": "Probe stroll", "start": at(second, "19:30"), "end": at(second, "20:00"), "action_ids": ["new:stroll"]}],
            new_actions=[{"ref": "new:stroll", "name": "take a stroll"}],
        )
        pid = proposed["proposal_id"]
        await call("get_proposal")
        amended = await call("amend_proposal", proposal_id=pid, revision=proposed["revision"], updates=[{"event": {"id": d2["Dinner"], "summary": "Probe dinner"}}])
        feedback = await call("add_proposal_note", proposal_id=pid, text="probe feedback")
        await call("withdraw_proposal_note", feedback_id=feedback["id"])
        applied = await call("confirm_proposal", proposal_id=pid, revision=amended["revision"])
        assert applied["status"] == "applied", applied
        due = await call("prepare_judgments")
        requests = [f"{e['event_id']}/{p}/{part}" for e in due["events"] for p, parts in e["people"].items() for part in parts]
        if requests:
            await call("record_judgments", compaction_id=due["compaction_id"], judgments=[{"request_id": r, "rating": 1, "reasoning": "probe"} for r in requests])
        status = await call("get_compaction_status")
        assert status["judgments_pending"] is None, status
        dinner = await call("get_event", id=d2["Dinner"])
        assert dinner["summary"] == "Probe dinner", dinner
        await call("prepare_habit_judgments", habit_id="Probe meals")

        print("Health")
        await call("get_health", samples=False)


if __name__ == "__main__":
    main()
