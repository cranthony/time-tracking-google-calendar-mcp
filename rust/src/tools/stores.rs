//! The prefetches that make a tool read all its tabs in one request.

use crate::app::App;
use crate::google::sheets::TabRange;

/// `_prefetch(*tabs)`: every tab's whole range, in one read request.
pub fn prefetch(app: &App, ranges: Vec<TabRange>) -> anyhow::Result<()> {
    app.actions()?.prefetch(&ranges)
}

/// `_prefetch_stores`: the actions', people's and locations' tabs.
pub fn prefetch_stores(app: &App) -> anyhow::Result<()> {
    let (actions, people) = (app.actions()?, app.people()?);
    let locations = app.locations()?;
    let mut ranges = actions.whole_tabs();
    ranges.extend(people.whole_tabs());
    ranges.extend(locations.whole_tabs());
    actions.prefetch(&ranges)
}

/// `_prefetch_people`: those, and the Cancellations tab (and what it reads).
pub fn prefetch_people(app: &App) -> anyhow::Result<()> {
    let (actions, people, locations, cancellations) = (app.actions()?, app.people()?, app.locations()?, app.cancellations()?);
    let mut ranges = actions.whole_tabs();
    ranges.extend(people.whole_tabs());
    ranges.extend(locations.whole_tabs());
    ranges.extend(cancellations.whole_tabs());
    ranges.extend(app.person_facts()?.whole_tabs());
    actions.prefetch(&ranges)
}

/// `_prefetch_habits`: the habits', actions' and traits' tabs -- and, with
/// `cancellations`, the Cancellations tab and what it reads.
pub fn prefetch_habits(app: &App, cancellations: bool) -> anyhow::Result<()> {
    let (habits, actions, traits) = (app.habits()?, app.actions()?, app.traits()?);
    let mut ranges = vec![habits.whole_tab()];
    ranges.extend(actions.whole_tabs());
    ranges.push(traits.whole_tab());
    if cancellations {
        ranges.extend(app.cancellations()?.whole_tabs());
    }
    habits.prefetch(&ranges)
}
