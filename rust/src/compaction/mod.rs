//! Compaction: realigning the days since the last compaction to their
//! notes, as proposals the user confirms, then judging what it settled.

pub mod additions;
pub mod compactor;
pub mod fact_changes;
pub mod goal_changes;
pub mod guide;
pub mod judgments;
pub mod plan;
pub mod proposals;
