"""Run the metadata spreadsheet's newer tabs end to end against Google:
actions, their labels on the calendar, and whatever else lives in
row-per-item tabs (utilities/row_sheet.py).

It creates a throwaway calendar (this app's calendar.app.created scope
allows that), which gets its own metadata spreadsheet, runs every step
against them, prints what happened, then deletes the calendar --
nothing else in your account is touched. The spreadsheet is renamed
"CTT tabs probe (safe to delete)" and left in Drive: this app's Google
Cloud project doesn't enable the Drive API, so it can't trash it.

Usage:
    python -m probes.metadata_tabs [--keep]
"""

from __future__ import annotations

import argparse

from googleapiclient.discovery import build

from calendar_clients.google_auth import load_credentials
from calendar_clients.google_calendar import CalendarClient
from calendar_clients.google_sheets import SheetsClient
from calendar_clients.write_lock import WRITE_LOCK
from config import get_credentials_path, get_token_path
from utilities import calendar_metadata_sheet
from utilities.action_groups import ActionGroup
from utilities.actions import Action, Actions

_SCRATCH_TITLE = "CTT tabs probe (safe to delete)"


def main() -> None:
    with WRITE_LOCK:
        _main()


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="Keep the scratch calendar and spreadsheet")
    args = parser.parse_args()

    creds = load_credentials(get_token_path(), get_credentials_path())
    calendar_service = build("calendar", "v3", credentials=creds)
    calendar_id = calendar_service.calendars().insert(body={"summary": _SCRATCH_TITLE}).execute()["id"]
    print(f"Scratch calendar: {calendar_id}")
    calendar = CalendarClient(calendar_service, calendar_id)
    sheets_service = build("sheets", "v4", credentials=creds)
    sheets = SheetsClient(sheets_service)
    spreadsheet_id = None
    try:
        spreadsheet_id, _ = calendar_metadata_sheet.ensure_spreadsheet(calendar, sheets)
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"updateSpreadsheetProperties": {
                "properties": {"title": _SCRATCH_TITLE}, "fields": "title",
            }}]},
        ).execute()
        print(f"Scratch spreadsheet: https://docs.google.com/spreadsheets/d/{spreadsheet_id}")
        _probe_actions(calendar, sheets, spreadsheet_id)
        print("\nAll steps passed.")
    finally:
        if args.keep:
            print("Kept the scratch calendar and spreadsheet")
        else:
            calendar_service.calendars().delete(calendarId=calendar_id).execute()
            print(f"Deleted the scratch calendar; trash the {_SCRATCH_TITLE!r} spreadsheet by hand")


def _check(step: str, condition: bool, detail: object = "") -> None:
    print(f"{'ok  ' if condition else 'FAIL'} {step} {detail}")
    if not condition:
        raise SystemExit(f"Step failed: {step}")


def _labels(calendar: CalendarClient) -> dict[str, str]:
    labels, _etag = calendar.list_event_labels()
    return {label.id: label.name for label in labels if label.name}


def _probe_actions(calendar: CalendarClient, sheets: SheetsClient, spreadsheet_id: str) -> None:
    print("\nActions")
    actions = Actions.ensure(calendar, sheets, spreadsheet_id)
    walk = actions.create_action(Action(name="Walk", status="active", priority=1)).created_id
    idea = actions.create_action(Action(name="Juggle", note="proposed by the assistant")).created_id
    by_id = {a.id: a for a in actions.all()}
    _check("created two actions", set(by_id) == {walk, idea}, [a.name for a in by_id.values()])
    labels = _labels(calendar)
    _check(
        "both hold labels on the calendar",
        labels.get(by_id[walk].label_id) == "Walk" and labels.get(by_id[idea].label_id) == "Juggle",
        labels,
    )
    actions.update_action(Action(id=walk, name="Stroll"))
    _check("renaming renames the label", _labels(calendar).get(by_id[walk].label_id) == "Stroll")
    actions.update_action(Action(id=idea, status="archived"))
    _check("archiving removes the label", by_id[idea].label_id not in _labels(calendar))
    _check("found by name", actions.get_action("stroll").id == walk)
    try:
        actions.create_action(Action(name="stroll"))
        _check("a duplicate name is refused", False)
    except ValueError as exc:
        _check("a duplicate name is refused", "already an action" in str(exc), f"({exc})")
    again = Actions.ensure(calendar, sheets, spreadsheet_id)
    _check("the tab is found again", {a.id for a in again.all()} == {walk, idea})

    print("\nAction groups")
    outdoors = actions.create_action_group(ActionGroup(name="Outdoors", background_color="#0b8043")).created_id
    actions.update_action(Action(id=walk, group_id=outdoors))
    label_id = by_id[walk].label_id
    labels, _etag = calendar.list_event_labels()
    color = next(label.background_color for label in labels if label.id == label_id)
    _check("an action's label takes its group's color", color.lower() == "#0b8043", color)
    actions.delete_action_group(outdoors)
    _check(
        "deleting the group moves the action to the top",
        actions.get_action(walk).group_id is None and actions.get_action_groups() == [],
    )


if __name__ == "__main__":
    main()
