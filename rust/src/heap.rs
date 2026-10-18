//! What the heap sets aside for the data the server keeps -- an estimate,
//! for the memory breakdown (health::MemoryParts): the blocks a structure
//! owns, each as glibc's malloc sets one aside on Linux, where the server
//! runs. tests/memory_estimates.rs checks the estimates against what a
//! counting allocator sees.

use std::mem::size_of;

/// What malloc sets aside for a block of `size` bytes: the size and an
/// 8-byte header, rounded up to 16 bytes, 32 at least -- nothing for no
/// block (an empty Vec or String allocates none).
pub fn block(size: usize) -> usize {
    if size == 0 {
        0
    } else {
        ((size + 8 + 15) & !15).max(32)
    }
}

/// A Vec's (or String's) buffer: `capacity` items of type `T`.
pub fn buffer<T>(capacity: usize) -> usize {
    block(capacity * size_of::<T>())
}

/// A hashbrown table of `len` items of `item` bytes, at the fewest buckets
/// it grows to for them: a power of two, kept no more than 7/8 full (4 or
/// 8 for a small one), each an item and a control byte, and a group of 16
/// control bytes more.
pub fn hash_table(len: usize, item: usize) -> usize {
    if len == 0 {
        return 0;
    }
    let buckets = buckets(len);
    block(((buckets * item + 15) & !15) + buckets + 16)
}

/// How many items a hashbrown table grown for `len` has room for: one
/// less than its buckets, small, else 7/8 of them. (An IndexMap keeps
/// room for as many entries as its table has.)
pub fn hash_capacity(len: usize) -> usize {
    match buckets(len) {
        0 => 0,
        b if b < 8 => b - 1,
        8 => 7,
        b => b / 8 * 7,
    }
}

fn buckets(len: usize) -> usize {
    if len == 0 {
        0
    } else if len < 4 {
        4
    } else if len < 8 {
        8
    } else {
        (len * 8 / 7).next_power_of_two()
    }
}

/// A BTreeMap of `len` entries of `entry` bytes: its leaves (11 entries
/// each at most, about two thirds full once filled in no order), their
/// parents' edges, and each node's few bytes more.
pub fn btree(len: usize, entry: usize) -> usize {
    if len == 0 {
        return 0;
    }
    let leaf = 8 + 11 * entry + 4;
    let leaves = len.div_ceil(8);
    let internals = if leaves > 1 { leaves.div_ceil(8) } else { 0 };
    leaves * block(leaf) + internals * block(leaf + 12 * size_of::<usize>())
}

#[cfg(test)]
#[path = "heap_tests.rs"]
mod tests;
