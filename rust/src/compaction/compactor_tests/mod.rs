//! The module's tests, against the fake Google: the notes, the journal and
//! the stores in a real metadata spreadsheet, the events on a real (fake)
//! calendar. Where Python checked a MagicMock client's calls, these check
//! the requests the fake was sent, or what it now holds.

use super::*;
use crate::compaction::goal_changes::{GoalChange, GoalChangeChoice};
use crate::compaction::plan::EventDecision;
use crate::compaction::proposals::{AdditionChoice, NoteEdit, Proposal, ProposalEvent};
use crate::sheet::{ACTIONS, GOALS, LOCATIONS, PEOPLE, TRAITS};
use crate::stores::journal::{PlannedDay, ABANDONED, APPLYING, PLANNED, PROPOSED, STAMPED, SUPERSEDED};
use crate::testing::app::{test_app, TestApp};
use crate::testing::event_at;
use crate::testing::fake_google::LoggedRequest;
use crate::write_lock::WRITE_LOCK;

mod commit;
mod dry_run;
mod facts;
mod goal_changes;
mod goals;
mod judging;
mod historical_notes;
mod mornings;
mod new_goals;
mod prepare;
mod proposal_notes;
mod proposals;
mod recap;
mod several_days;
mod trait_facts;

pub fn time_at(time: &str) -> Moment {
    crate::testing::time_at(time)
}

pub fn strings(items: &[&str]) -> Vec<String> {
    crate::testing::strings(items)
}

/// The day the notes are on: two pieces of work, lunch, and the night.
pub fn day() -> Vec<Event> {
    vec![
        event_at("09:00-10:00").id("e1").summary("Email").priority(2).build(),
        event_at("10:00-11:00").id("e2").summary("Report").priority(2).build(),
        event_at("12:00-13:00").id("e3").summary("Lunch").priority(1).build(),
        event_at("20:00-07:00+1").id("s1").summary("Sleep").priority(0).build(),
    ]
}

/// The stores beyond the notes and the journal, seeded by row.
#[derive(Default)]
pub struct Stores {
    pub actions: Vec<Value>,
    pub people: Vec<Value>,
    pub locations: Vec<Value>,
    pub goals: Vec<Value>,
    /// Traits turn judging and cancellations on, as in Python.
    pub traits: Option<Vec<Value>>,
}

pub struct Setup {
    pub t: TestApp,
    pub compactor: NoteCompactor,
}

/// What `dry_run` takes beyond the decisions.
#[derive(Default)]
pub struct DryRun {
    pub ignore_notes: Option<Vec<String>>,
    pub additions: Additions,
    pub proposal_id: Option<String>,
    pub revision: Option<i64>,
    pub replies: Vec<FeedbackReply>,
    pub annotate_notes: Vec<(String, String)>,
    pub recap: Option<Recap>,
    pub create_refs: Vec<Option<String>>,
    pub fact_summaries: Vec<ProposedSummary>,
    pub goal_changes: Vec<GoalChange>,
}

/// What `amend` takes: the user's edits.
#[derive(Default)]
pub struct Amend {
    pub decisions: Vec<EventDecision>,
    pub as_planned: Vec<String>,
    pub notes: Vec<NoteEdit>,
    pub additions: Vec<AdditionChoice>,
    pub goal_changes: Vec<GoalChangeChoice>,
    pub through: Option<Moment>,
}

impl DryRun {
    pub fn revising(proposal_id: &str, revision: i64) -> DryRun {
        DryRun { proposal_id: Some(proposal_id.into()), revision: Some(revision), ..Default::default() }
    }
}

impl Setup {
    pub fn new(notes: &[(&str, &str)], events: Option<Vec<Event>>, now: &str) -> Setup {
        Setup::with(notes, events, now, Stores::default())
    }

    pub fn with(notes: &[(&str, &str)], events: Option<Vec<Event>>, now: &str, stores: Stores) -> Setup {
        let t = test_app();
        let app = &t.app;
        let actions = app.actions().unwrap();
        let people = app.people().unwrap();
        let locations = app.locations().unwrap();
        let notes_sheet = app.noted_time_sheet().unwrap();
        let journal = app.journal().unwrap();
        let goals = app.goals().unwrap();
        for (title, rows) in [(ACTIONS.1, &stores.actions), (PEOPLE.1, &stores.people), (LOCATIONS.1, &stores.locations), (GOALS.1, &stores.goals)] {
            if !rows.is_empty() {
                t.seed(title, rows);
            }
        }
        let (judging, cancellations) = match &stores.traits {
            Some(traits) => {
                let trait_store = app.traits().unwrap();
                t.seed(TRAITS.1, traits);
                let judging = Judging {
                    client: ActionCalendar::new(app.calendar_client().unwrap(), actions.clone()),
                    actions: actions.clone(),
                    people: people.clone(),
                    locations: locations.clone(),
                    traits: trait_store,
                    habits: Some(app.habits().unwrap()),
                    person_facts: Some(app.person_facts().unwrap()),
                };
                (Some(judging), Some(app.cancellations().unwrap()))
            }
            None => (None, None),
        };
        let compactor = NoteCompactor {
            calendar: ActionCalendar::new(app.calendar_client().unwrap(), actions.clone()),
            client: ActionCalendar::new(app.calendar_client().unwrap(), actions.clone()),
            notes: notes_sheet,
            journal,
            actions: Some(actions),
            people: Some(people),
            locations: Some(locations),
            goals: Some(goals),
            facts: judging.as_ref().map(|j| FactKeeping { traits: j.traits.clone(), person_facts: app.person_facts().unwrap() }),
            judging,
            cancellations,
        };
        for event in events.unwrap_or_else(day) {
            t.seed_event(&event);
        }
        let setup = Setup { t, compactor };
        for (at, description) in notes {
            setup.append_note(at, Some(description));
        }
        setup.set_now(now);
        setup.t.fake.clear_requests();
        setup
    }

    pub fn standard() -> Setup {
        Setup::new(&[("09:05", "email"), ("10:20", "report")], None, "11:30")
    }

    /// The compactor's clock (on this test's thread).
    pub fn set_now(&self, at: &str) {
        self.set_moment(time_at(at));
    }

    pub fn set_moment(&self, now: Moment) {
        determinism::TEST_NOW.with(|c| c.set(Some(now)));
    }

    /// Run `f` as a tool call does: holding WRITE_LOCK, inside one call's
    /// cache of Calendar listings.
    pub fn call<T>(&self, f: impl FnOnce(&NoteCompactor) -> T) -> T {
        let _held = WRITE_LOCK.hold();
        f(&self.compactor)
    }

    pub fn append_note(&self, at: &str, description: Option<&str>) {
        let note = NotedTime { timestamp: time_at(at), description: description.map(str::to_string), compaction_id: None };
        self.call(|c| c.notes.append(&note, None).unwrap());
    }

    pub fn notes(&self, include_compacted: bool) -> Vec<SheetNote> {
        self.compactor.notes.read_with_rows(include_compacted).unwrap()
    }

    /// The id of the note in sheet row `row` (see SheetNote.id).
    pub fn note_id(&self, row: i64) -> String {
        self.notes(true).into_iter().find(|n| n.row == row).map(|n| n.id()).expect("a note in that row")
    }

    pub fn note_ids(&self) -> Vec<String> {
        self.notes(false).iter().map(SheetNote::id).collect()
    }

    /// The 09:05 note started the email; the 10:20 note ended it and
    /// started the report, which then ran to its planned end.
    pub fn email_then_report(&self) -> Vec<EventDecision> {
        vec![
            EventDecision { event_id: Some("e1".into()), start_note: Some(self.note_id(2)), end_note: Some(self.note_id(3)), ..EventDecision::new("keep") },
            EventDecision { event_id: Some("e2".into()), start_note: Some(self.note_id(3)), ..EventDecision::new("keep") },
        ]
    }

    /// Coffee from the 09:00 note to the 09:30 one, then the email.
    pub fn coffee_instead_of_email(&self) -> Vec<EventDecision> {
        vec![
            EventDecision { summary: Some("Coffee".into()), start_note: Some(self.note_id(2)), end_note: Some(self.note_id(3)), ..EventDecision::new("create") },
            EventDecision { event_id: Some("e1".into()), start_note: Some(self.note_id(3)), ..EventDecision::new("keep") },
        ]
    }

    pub fn prepare(&self) -> CompactionContext {
        self.call(|c| c.prepare()).unwrap()
    }

    pub fn try_dry_run(&self, decisions: Vec<EventDecision>, options: DryRun) -> anyhow::Result<CompactionResult> {
        self.call(|c| {
            c.dry_run(
                decisions,
                options.ignore_notes,
                options.additions,
                options.proposal_id,
                options.revision,
                options.replies,
                options.annotate_notes,
                options.recap,
                options.create_refs,
                options.fact_summaries,
                options.goal_changes,
            )
        })
    }

    pub fn dry_run(&self, decisions: Vec<EventDecision>) -> CompactionResult {
        self.try_dry_run(decisions, DryRun::default()).unwrap()
    }

    pub fn try_commit(&self, compaction_id: &str) -> anyhow::Result<CompactionResult> {
        self.call(|c| c.commit(compaction_id, true))
    }

    pub fn commit(&self, compaction_id: &str) -> CompactionResult {
        self.try_commit(compaction_id).unwrap()
    }

    pub fn try_amend(&self, proposal_id: &str, revision: i64, amend: Amend) -> anyhow::Result<Proposal> {
        self.call(|c| c.amend(proposal_id, revision, amend.decisions, amend.as_planned, amend.notes, amend.additions, amend.goal_changes, amend.through))
    }

    pub fn amend(&self, proposal_id: &str, revision: i64, decisions: Vec<EventDecision>) -> Proposal {
        self.try_amend(proposal_id, revision, Amend { decisions, ..Default::default() }).unwrap()
    }

    pub fn get_proposal(&self) -> Proposal {
        self.call(|c| c.get_proposal(None, None)).unwrap()
    }

    pub fn journal(&self) -> &CompactionJournal {
        &self.compactor.journal
    }

    pub fn load(&self, compaction_id: &str) -> JournalCompaction {
        self.journal().load(compaction_id).unwrap()
    }

    pub fn set_status(&self, compaction_id: &str, status: &str) {
        let mut loaded = self.load(compaction_id);
        self.call(|c| c.journal.set_status(&mut loaded, status).unwrap());
    }

    /// Record an unapplied plan from before proposals, with no changes, as
    /// of `now`.
    pub fn start_plain(&self, compaction_id: &str, now: Moment) {
        let plan = CompactionPlan::default();
        let day = PlannedDay {
            compaction_id: compaction_id.into(),
            now,
            note_ids: Vec::new(),
            decisions: &[],
            plan: &plan,
            ignore_notes: Vec::new(),
            additions: Map::new(),
            note_targets: Vec::new(),
            revisits: Vec::new(),
        };
        self.call(|c| c.journal.start_batch(&[day], None, &[]).unwrap());
    }

    /// The writes the calendar was sent since the setup.
    pub fn calendar_writes(&self) -> Vec<LoggedRequest> {
        self.t.fake.requests().into_iter().filter(|r| r.path.starts_with("/calendar/") && r.method != "GET").collect()
    }

    /// Each event patch the calendar was sent: (event id, body).
    pub fn patches(&self) -> Vec<(String, Value)> {
        self.calendar_writes()
            .into_iter()
            .filter(|r| r.method == "PATCH" && r.path.contains("/events/"))
            .map(|r| (r.path.rsplit('/').next().unwrap().to_string(), r.body))
            .collect()
    }

    pub fn patch(&self, event_id: &str) -> Value {
        self.patches().into_iter().find(|(id, _)| id == event_id).map(|(_, body)| body).unwrap_or_else(|| panic!("no patch of {event_id}"))
    }

    /// Each event the calendar was asked to create.
    pub fn creates(&self) -> Vec<Value> {
        self.calendar_writes().into_iter().filter(|r| r.method == "POST" && r.path.ends_with("/events")).map(|r| r.body).collect()
    }

    /// Fail the next request matching `method` and `path_piece` with
    /// `status`, once.
    pub fn fail_next(&self, method: &str, path_piece: &str, status: u16) {
        self.t.fake.with(|s| s.fail_next.push((method.into(), path_piece.into(), status, "injected".into())));
    }

    /// Change an event behind the compactor's back (as the user might in
    /// Calendar), leaving no requests logged.
    pub fn change_event(&self, patch: Event) {
        let _held = WRITE_LOCK.hold();
        self.compactor.client.client().update_event(&patch).unwrap();
        self.t.fake.clear_requests();
    }

    pub fn event(&self, event_id: &str) -> Event {
        self.compactor.client.client().get_event(event_id).unwrap()
    }
}

/// The refusal `result` failed with: a CompactionError matching `pattern`.
pub fn refused<T>(result: anyhow::Result<T>, pattern: &str) -> String {
    let Err(error) = result else { panic!("{pattern:?} wasn't refused") };
    let message = match compaction_error(&error) {
        Some(refusal) => refusal.message.clone(),
        None => panic!("not a CompactionError: {error:#}"),
    };
    assert!(regex::Regex::new(pattern).unwrap().is_match(&message), "{pattern:?} isn't in: {message}");
    message
}

/// A time in a request body ("start": {"dateTime": ...}).
pub fn moment_in(body: &Value, field: &str) -> Option<Moment> {
    body[field]["dateTime"].as_str().map(|t| crate::event::parse_iso(t, None).unwrap())
}

pub fn keep(event_id: &str) -> EventDecision {
    EventDecision { event_id: Some(event_id.into()), ..EventDecision::new("keep") }
}

pub fn change<'a>(result: &'a CompactionResult, event_id: &str) -> &'a CompactionChange {
    result.changes.iter().find(|c| c.event_id.as_deref() == Some(event_id)).unwrap_or_else(|| panic!("no change to {event_id}"))
}

pub fn ids<T>(items: &[T], id: impl Fn(&T) -> &str) -> Vec<String> {
    items.iter().map(|i| id(i).to_string()).collect()
}

pub fn event_ids(context: &CompactionContext) -> Vec<String> {
    ids(&context.events, |e| e.id.as_str())
}

pub fn timeline_event_ids(timeline: &Timeline) -> Vec<String> {
    timeline.events.iter().map(|e| e.event_id.clone().unwrap_or_default()).collect()
}
