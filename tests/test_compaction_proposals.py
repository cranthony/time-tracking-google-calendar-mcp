from tests.event_time_helpers import time_at
from tests.fake_sheets import FakeSheets
from utilities.compaction_journal import APPLYING, PROPOSED, STAMPED, CompactionJournal, PlannedDay, RevisionMeta
from utilities.compaction_proposals import Feedback, UserEdit, is_key, merge, split_feedback_id
from utilities.note_compaction import CompactionPlan, EventDecision

_P = "0123456789ab"


def _edit(seq, event_id, status="active", **edit):
    return UserEdit(
        id=f"{_P}u{seq}", seq=seq, event_id=event_id, edit=edit, status=status,
        created=time_at("11:00"), base_revision=1,
    )


def _by(merged):
    return {(d.key if d.action == "create" else d.event_id): d for d in merged.decisions}


class TestMerge:
    def test_an_edit_overrides_claudes_decision_field_by_field(self):
        claude = [EventDecision(action="keep", event_id="e1", start_note="n1", end_note="n2", summary="Email")]

        merged = merge(_P, claude, [_edit(1, "e1", action="keep", end=time_at("10:30").isoformat())])

        email = _by(merged)["e1"]
        # The end moved by time lets go of the note that set it.
        assert (email.start_note, email.end_note, email.end, email.summary) == ("n1", None, time_at("10:30"), "Email")
        assert merged.decided_by == {"e1": "user"}

    def test_a_later_edit_wins_and_as_planned_clears_everything_before_it(self):
        claude = [EventDecision(action="cancel", event_id="e1")]
        edits = [
            _edit(1, "e1", action="keep", summary="Inbox"),
            _edit(2, "e2", action="cancel", counts_against_follow_through=False),
            _edit(3, "e1", action="as_planned"),
        ]

        merged = merge(_P, claude, edits)

        assert set(_by(merged)) == {"e2"}
        assert _by(merged)["e2"].counts_against_follow_through is False

    def test_the_users_creates_are_keyed_by_their_edit_and_can_be_cancelled(self):
        edits = [
            _edit(1, None, action="create", summary="Walk", start=time_at("11:00").isoformat(), end=time_at("11:20").isoformat()),
            _edit(2, None, action="create", summary="Nap", start=time_at("13:00").isoformat(), end=time_at("13:20").isoformat()),
            _edit(3, f"{_P}u2", action="cancel"),
        ]

        merged = merge(_P, [], edits)

        assert list(_by(merged)) == [f"{_P}u1"]

    def test_an_edit_of_a_key_or_event_that_isnt_there_is_unknown(self):
        edits = [_edit(1, f"{_P}c4", action="keep", summary="x"), _edit(2, "gone", action="keep", summary="y")]

        merged = merge(_P, [], edits, known_ids={"e1"})

        assert [e.seq for e in merged.unknown] == [1, 2]
        assert merged.decisions == []

    def test_edits_that_arent_active_are_left_out(self):
        merged = merge(_P, [], [_edit(1, "e1", status="replaced", action="keep", summary="x")])

        assert merged.decisions == []

    def test_a_key_of_an_event_already_created_follows_it(self):
        merged = merge(_P, [], [_edit(1, f"{_P}c1", action="keep", summary="Long walk")], aliases={f"{_P}c1": "cmpx"})

        assert _by(merged)["cmpx"].summary == "Long walk"

    def test_ids(self):
        assert is_key(_P, f"{_P}c12") and is_key(_P, f"{_P}u3")
        assert not is_key(_P, "e1") and not is_key(_P, f"{_P}f1")
        assert split_feedback_id(f"{_P}f7") == (_P, 7)
        assert split_feedback_id("nope") is None


class TestJournalRows:
    def _journal(self):
        sheets = FakeSheets()
        return CompactionJournal(sheets, "s", 2)

    def _revision(self, journal, revision, status=PROPOSED):
        meta = RevisionMeta(
            proposal=_P, revision=revision, window_start=time_at("08:00"), created=time_at("11:30"),
            base=None, user_seq=0, by="claude", reason="proposed",
            claude_decisions=[EventDecision(action="keep", event_id="e1", summary="Inbox")],
        )
        journal.start_batch(
            [
                PlannedDay(compaction_id=f"{_P}r{revision}", now=time_at("11:30"), note_ids=[], decisions=[], plan=CompactionPlan(changes=[])),
                PlannedDay(compaction_id=f"{_P}r{revision}d2", now=time_at("11:30"), note_ids=[], decisions=[], plan=CompactionPlan(changes=[])),
            ],
            meta,
        )
        return journal.load_batch(f"{_P}r{revision}")

    def test_a_revision_round_trips_its_meta_and_claudes_decisions(self):
        journal = self._journal()

        first, second = self._revision(journal, 1)

        assert (first.status, first.proposal, first.revision, second.proposal) == (PROPOSED, _P, 1, _P)
        assert first.meta.claude_decisions == [EventDecision(action="keep", event_id="e1", summary="Inbox")]
        assert first.meta.window_start == time_at("08:00")
        assert journal.revisions(_P) == [(1, f"{_P}r1", PROPOSED)]

    def test_a_proposal_applied_partway_is_still_open(self):
        journal = self._journal()
        first, second = self._revision(journal, 1)
        journal.set_status(first, STAMPED)
        journal.set_status(second, APPLYING)

        assert journal.open_proposal() == _P

        journal.set_status(second, STAMPED)
        assert journal.open_proposal() is None

    def test_user_edits_and_feedback_round_trip(self):
        journal = self._journal()
        edit = _edit(1, "e1", action="keep", summary="Inbox")
        journal.add_user_edits([edit])
        item = Feedback(id=f"{_P}f1", seq=1, text="hm", event_id="e1", at=time_at("10:00"), created=time_at("11:31"))
        journal.add_feedback(item, _P)

        (edit_row, read_edit), = journal.user_edits(_P)
        (feedback_row, read_item), = journal.feedback(_P)
        assert read_edit == edit
        assert read_item == item

        journal.set_user_edit_status(edit_row, "replaced")
        answered = Feedback(**{**item.__dict__, "status": "answered", "reply": "done", "answered_in": 2})
        journal.update_feedback(feedback_row, answered)
        assert journal.user_edits(_P)[0][1].status == "replaced"
        assert journal.feedback(_P)[0][1] == answered
