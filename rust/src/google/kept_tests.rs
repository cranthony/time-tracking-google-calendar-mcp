//! The module's tests: the kept cells change as Sheets' would.

use super::*;

fn rows(items: &[&[&str]]) -> Rows {
    items.iter().map(|row| row.iter().map(|c| c.to_string()).collect()).collect()
}

#[test]
fn a_write_replaces_the_cells_it_gives_and_leaves_the_rest() {
    let mut kept = rows(&[&["h1", "h2", "h3"], &["a", "b", "c"]]);

    set_cells(&mut kept, 1, 1, &rows(&[&["B", ""], &["", "y"]]));

    assert_eq!(kept, rows(&[&["h1", "h2", "h3"], &["a", "B"], &["", "", "y"]]));
}

#[test]
fn clearing_the_last_cells_trims_them_as_a_read_would() {
    let mut kept = rows(&[&["h"], &["a", "b"], &["c"]]);

    set_cells(&mut kept, 1, 0, &rows(&[&["a", ""], &[""]]));

    assert_eq!(kept, rows(&[&["h"], &["a"]]));
}

#[test]
fn a_write_past_the_kept_columns_keeps_only_what_fits() {
    let mut kept = Rows::new();

    set_cells(&mut kept, 0, 25, &rows(&[&["z", "past"]]));

    assert_eq!(kept[0].len(), 26);
    assert_eq!(kept[0][25], "z");
}

#[test]
fn deleting_rows_moves_those_below_up() {
    let mut kept = rows(&[&["h"], &["a"], &["b"], &["c"]]);

    delete_rows(&mut kept, 1, 3);
    assert_eq!(kept, rows(&[&["h"], &["c"]]));

    delete_rows(&mut kept, 5, 9);
    assert_eq!(kept, rows(&[&["h"], &["c"]]));
}

#[test]
fn differences_name_each_cell_either_way() {
    let found = rows(&[&["h", "x"], &["a"], &["extra"]]);
    let kept = rows(&[&["h"], &["a", "b"]]);

    let cells: Vec<(String, String, String)> = differences(&found, &kept).into_iter().map(|d| (a1(d.row, d.column), d.found, d.kept)).collect();

    assert_eq!(
        cells,
        vec![
            ("B1".to_string(), "x".to_string(), String::new()),
            ("B2".to_string(), String::new(), "b".to_string()),
            ("A3".to_string(), "extra".to_string(), String::new()),
        ]
    );
}

#[test]
fn column_letters_run_past_z() {
    assert_eq!([0, 25, 26, 701, 702].map(column_letters), ["A", "Z", "AA", "ZZ", "AAA"]);
}

#[test]
fn spreadsheets_lists_only_those_with_a_tab_kept() {
    let root = format!("kept-test-{}", uuid::Uuid::new_v4());
    with(&root, "read", |tabs| tabs.entry(1).or_default().rows = Some(Rows::new()));
    with(&root, "written-only", |tabs| tabs.entry(1).or_default().generation = 3);

    assert_eq!(spreadsheets(&root), vec!["read".to_string()]);

    forget_spreadsheet(&root, "read");
    assert!(spreadsheets(&root).is_empty());
    assert_eq!(with(&root, "read", |tabs| tabs[&1].generation), 1);
}
