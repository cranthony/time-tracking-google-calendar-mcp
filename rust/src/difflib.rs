//! Python's `difflib.get_close_matches`, exactly: error messages suggest
//! close matches ("did you mean ...?") and must suggest the same ones, in
//! the same order. A port of SequenceMatcher's matching-block algorithm
//! (no junk: the strings compared here are far shorter than the 200
//! characters where its "popular element" heuristic starts).

use std::collections::HashMap;

struct Matcher<'a> {
    a: &'a [char],
    b: &'a [char],
    b2j: HashMap<char, Vec<usize>>,
}

impl<'a> Matcher<'a> {
    fn new(a: &'a [char], b: &'a [char]) -> Matcher<'a> {
        let mut b2j: HashMap<char, Vec<usize>> = HashMap::new();
        for (j, c) in b.iter().enumerate() {
            b2j.entry(*c).or_default().push(j);
        }
        if b.len() >= 200 {
            // autojunk: drop elements making up more than 1% of b.
            let ntest = b.len() / 100 + 1;
            b2j.retain(|_, js| js.len() <= ntest);
        }
        Matcher { a, b, b2j }
    }

    fn find_longest_match(&self, alo: usize, ahi: usize, blo: usize, bhi: usize) -> (usize, usize, usize) {
        let (mut besti, mut bestj, mut bestsize) = (alo, blo, 0usize);
        let mut j2len: HashMap<usize, usize> = HashMap::new();
        for i in alo..ahi {
            let mut newj2len: HashMap<usize, usize> = HashMap::new();
            if let Some(js) = self.b2j.get(&self.a[i]) {
                for &j in js {
                    if j < blo {
                        continue;
                    }
                    if j >= bhi {
                        break;
                    }
                    let k = j2len.get(&(j.wrapping_sub(1))).copied().unwrap_or(0) + 1;
                    let k = if j == 0 { 1 } else { k };
                    newj2len.insert(j, k);
                    if k > bestsize {
                        besti = i + 1 - k;
                        bestj = j + 1 - k;
                        bestsize = k;
                    }
                }
            }
            j2len = newj2len;
        }
        // With no junk, Python's extensions run while the elements around
        // the match are equal (which the search above already took in).
        while besti > alo && bestj > blo && self.a[besti - 1] == self.b[bestj - 1] {
            besti -= 1;
            bestj -= 1;
            bestsize += 1;
        }
        while besti + bestsize < ahi
            && bestj + bestsize < bhi
            && self.a[besti + bestsize] == self.b[bestj + bestsize]
        {
            bestsize += 1;
        }
        (besti, bestj, bestsize)
    }

    fn matching_total(&self) -> usize {
        let mut queue = vec![(0, self.a.len(), 0, self.b.len())];
        let mut total = 0;
        while let Some((alo, ahi, blo, bhi)) = queue.pop() {
            let (i, j, k) = self.find_longest_match(alo, ahi, blo, bhi);
            if k > 0 {
                total += k;
                if alo < i && blo < j {
                    queue.push((alo, i, blo, j));
                }
                if i + k < ahi && j + k < bhi {
                    queue.push((i + k, ahi, j + k, bhi));
                }
            }
        }
        total
    }
}

fn ratio(matches: usize, la: usize, lb: usize) -> f64 {
    if la + lb == 0 {
        1.0
    } else {
        2.0 * matches as f64 / (la + lb) as f64
    }
}

/// `difflib.get_close_matches(word, possibilities, n=3, cutoff=0.6)`.
pub fn get_close_matches(word: &str, possibilities: &[String], n: usize) -> Vec<String> {
    let cutoff = 0.6;
    let b: Vec<char> = word.chars().collect();
    let mut b_counts: HashMap<char, i64> = HashMap::new();
    for c in &b {
        *b_counts.entry(*c).or_default() += 1;
    }
    let mut scored: Vec<(f64, String)> = Vec::new();
    for x in possibilities {
        let a: Vec<char> = x.chars().collect();
        let (la, lb) = (a.len(), b.len());
        // real_quick_ratio
        if ratio(la.min(lb), la, lb) < cutoff {
            continue;
        }
        // quick_ratio
        let mut avail = b_counts.clone();
        let mut matches = 0;
        for c in &a {
            let slot = avail.entry(*c).or_insert(0);
            if *slot > 0 {
                matches += 1;
            }
            *slot -= 1;
        }
        if ratio(matches, la, lb) < cutoff {
            continue;
        }
        let matcher = Matcher::new(&a, &b);
        let score = ratio(matcher.matching_total(), la, lb);
        if score >= cutoff {
            scored.push((score, x.clone()));
        }
    }
    // heapq.nlargest(n, (score, x)): best score first, ties to the larger x.
    scored.sort_by(|p, q| q.0.partial_cmp(&p.0).unwrap().then_with(|| q.1.cmp(&p.1)));
    scored.into_iter().take(n).map(|(_, x)| x).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strings(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn matches_python() {
        // difflib.get_close_matches('appel', ['ape', 'apple', 'peach', 'puppy'])
        assert_eq!(get_close_matches("appel", &strings(&["ape", "apple", "peach", "puppy"]), 3), vec!["apple", "ape"]);
        assert_eq!(get_close_matches("play gutar", &strings(&["play guitar", "read a book"]), 3), vec!["play guitar"]);
        assert!(get_close_matches("zzz", &strings(&["home"]), 3).is_empty());
    }
}
