//! The heap model's tests. (How close it comes to real allocations is
//! tests/memory_estimates.rs's to check.)

use super::*;

#[test]
fn a_block_is_its_size_and_header_rounded_to_sixteen_and_thirty_two_at_least() {
    assert_eq!([0, 1, 24, 25, 40, 100].map(block), [0, 32, 32, 48, 48, 112]);
}

#[test]
fn a_buffer_is_one_block_of_its_capacity() {
    assert_eq!((buffer::<u64>(0), buffer::<u64>(3), buffer::<u8>(100)), (0, 32, 112));
}

#[test]
fn a_hash_table_has_a_power_of_two_buckets_no_more_than_seven_eighths_full() {
    assert_eq!(hash_table(0, 8), 0);
    // 3 items: 4 buckets, 32 + 4 + 16 bytes.
    assert_eq!(hash_table(3, 8), block(32 + 4 + 16));
    // 14 items: 16 buckets; 15: 32.
    assert_eq!(hash_table(14, 8), block(128 + 16 + 16));
    assert_eq!(hash_table(15, 8), block(256 + 32 + 16));
}

#[test]
fn a_hash_table_has_room_for_one_less_than_its_buckets_small_and_seven_eighths_big() {
    assert_eq!([0, 1, 3, 4, 7, 8, 14, 15].map(hash_capacity), [0, 3, 3, 7, 7, 14, 14, 28]);
}

#[test]
fn a_btree_grows_with_its_entries() {
    assert_eq!(btree(0, 100), 0);
    assert!(btree(8, 100) < btree(9, 100));
    assert!(btree(1000, 100) > 1000 * 100);
}
