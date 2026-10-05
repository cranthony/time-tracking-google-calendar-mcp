"""A CalendarClient's label and metadata calls, for the stores that keep
the calendar's event labels in step with a tab (goals, actions)."""

from dataclasses import replace

from calendar_clients.google_calendar import EventLabelConflictError

_SPREADSHEET_KEY = "calendar-metadata-spreadsheet-id"


class FakeLabelCalendar:
    """The label and metadata calls `Goals` makes on a CalendarClient,
    with the real etag check on writes."""

    def __init__(self, labels=(), *, spreadsheet_id: str | None = "spreadsheet-1"):
        self.labels = [replace(label) for label in labels]
        self.metadata = {_SPREADSHEET_KEY: spreadsheet_id} if spreadsheet_id else {}
        self.version = 0
        self.writes = 0

    def get_calendar_metadata(self, key):
        return self.metadata.get(key)

    def set_calendar_metadata(self, key, value):
        self.metadata[key] = value

    def list_event_labels(self):
        return [replace(label) for label in self.labels], f"etag-{self.version}"

    def replace_event_labels(self, labels, etag=None):
        if etag is not None and etag != f"etag-{self.version}":
            raise EventLabelConflictError("stale etag")
        self.labels = [replace(label) for label in labels]
        self.version += 1
        self.writes += 1
        return [replace(label) for label in self.labels]

    def named(self):
        return {label.id: (label.name, label.background_color) for label in self.labels if label.name}
