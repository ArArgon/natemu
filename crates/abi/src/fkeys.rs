//! Frame-keyed plan step 1a (docs/restructure/frame-keyed-plan.md): the exact table of templates and shared translations by
//! (frame, page offset, half, context), as pure functions over its slots (the resident's `cache` keeps it in its lookup area,
//! `execution::cache::FKEYS`). Open addressing with linear probing: a slot is {32-bit tag of the key's hash, block number + 1}
//! (0: empty). A key names at most one block, and a block is in at most one slot, so the table never holds more entries than
//! blocks; [`enter`] keeps at most `max` live entries, half the slots, so every probe ends at an empty slot. A tag match is
//! only a candidate: the caller checks the block's record (`fits`).

/// A slot: {tag, block number + 1}.
pub type Slot = [u32; 2];

/// The 64-bit hash of the key (frame `gpa`, `rip`'s page offset and half, `context`): [`home`] is its top bits, the tag its
/// low 32.
pub const fn hash(gpa: u64, rip: u64, context: u64) -> u64 {
    let key = (gpa & !0xfff | rip & 0xfff) ^ (rip >> 63) << 62 ^ context.rotate_left(29);
    (key ^ key >> 31).wrapping_mul(0x9e37_79b9_7f4a_7c15)
}
/// The home slot of hash `h` in a table of `len` (a power of two) slots.
pub const fn home(h: u64, len: usize) -> usize {
    (h >> (64 - len.trailing_zeros())) as usize
}
/// The block (number + 1) of the key hashed `h` for which `fits(number + 1)` holds, from its home on.
pub fn find(table: &[Slot], h: u64, fits: impl Fn(u32) -> bool) -> Option<u32> {
    let mask = table.len() - 1;
    let mut i = home(h, table.len());
    loop {
        let [tag, number] = table[i];
        if number == 0 {
            return None;
        }
        if tag == h as u32 && fits(number) {
            return Some(number);
        }
        i = (i + 1) & mask;
    }
}
/// What [`enter`] did.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Entered {
    /// In a free slot (one more live entry).
    New,
    /// In place of the key's block (`replaces`: a shared translation in place of its template).
    Replaced,
    /// The key names a block already (a copy of an entered template): nothing written.
    Kept,
    /// The table holds `max` entries: nothing written.
    Full,
}
/// Enters block `number` (+ 1) under the key hashed `h`: in place of the key's block when there is one (`same(number + 1)`: a
/// live block of the same key) and `replaces`, else kept as it is; in the first free slot otherwise, unless `live` entries
/// are `max` or more.
pub fn enter(table: &mut [Slot], h: u64, number: u32, replaces: bool, same: impl Fn(u32) -> bool, live: usize, max: usize) -> Entered {
    let mask = table.len() - 1;
    let mut i = home(h, table.len());
    loop {
        let [tag, n] = table[i];
        if n == 0 {
            if live >= max {
                return Entered::Full;
            }
            table[i] = [h as u32, number];
            return Entered::New;
        }
        if tag == h as u32 && same(n) {
            if replaces {
                table[i] = [tag, number];
                return Entered::Replaced;
            }
            return Entered::Kept;
        }
        i = (i + 1) & mask;
    }
}
/// Takes block `number` (+ 1, hashed `h`) out if a slot names it (backward-shift deletion, so no probe of another key ends
/// early); `home_of(n)` is the home of the block a slot names. True when it was there (one live entry less).
pub fn remove(table: &mut [Slot], h: u64, number: u32, home_of: impl Fn(u32) -> usize) -> bool {
    let mask = table.len() - 1;
    let mut i = home(h, table.len());
    loop {
        let n = table[i][1];
        if n == 0 {
            return false;
        }
        if n == number {
            break;
        }
        i = (i + 1) & mask;
    }
    let mut j = i;
    loop {
        j = (j + 1) & mask;
        let [tag, n] = table[j];
        if n == 0 {
            table[i] = [0, 0];
            return true;
        }
        // The entry at j moves into the hole at i unless its home lies cyclically in (i, j].
        let h = home_of(n);
        if (j.wrapping_sub(h) & mask) >= (j.wrapping_sub(i) & mask) {
            table[i] = [tag, n];
            i = j;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A model: blocks numbered 1.. with a key each; the table's answers checked against a scan of the live blocks.
    struct Model {
        table: [Slot; 64],
        keys: [(u64, u64, u64); 200],
        live: [bool; 200],
        translation: [bool; 200],
        entries: usize,
    }
    impl Model {
        fn new() -> Self {
            Model { table: [[0; 2]; 64], keys: [(0, 0, 0); 200], live: [false; 200], translation: [false; 200], entries: 0 }
        }
        fn h(&self, n: u32) -> u64 {
            let (g, r, c) = self.keys[n as usize];
            hash(g, r, c)
        }
        fn same(&self, n: u32, m: u32) -> bool {
            self.live[m as usize] && self.keys[m as usize] == self.keys[n as usize]
        }
        fn place(&mut self, n: u32, key: (u64, u64, u64), translation: bool) -> Entered {
            (self.keys[n as usize], self.live[n as usize], self.translation[n as usize]) = (key, true, translation);
            let h = self.h(n);
            let keys = self.keys;
            let live = self.live;
            let e = enter(&mut self.table, h, n, translation, |m| live[m as usize] && keys[m as usize] == keys[n as usize], self.entries, 32);
            self.entries += usize::from(e == Entered::New);
            e
        }
        fn kill(&mut self, n: u32) {
            self.live[n as usize] = false;
            let h = self.h(n);
            let keys = self.keys;
            let len = self.table.len();
            if remove(&mut self.table, h, n, |m| {
                let (g, r, c) = keys[m as usize];
                home(hash(g, r, c), len)
            }) {
                self.entries -= 1;
            }
        }
        fn found(&self, key: (u64, u64, u64)) -> Option<u32> {
            let (g, r, c) = key;
            find(&self.table, hash(g, r, c), |m| self.live[m as usize] && self.keys[m as usize] == key)
        }
    }

    #[test]
    fn hash_keys_frame_offset_half_and_context() {
        let k = hash(0x1234_5000, 0x7f00_0000_0123, 3);
        assert_eq!(k, hash(0x1234_5000, 0x5500_0000_7123, 3), "the RIP's page does not count, only its offset");
        assert_ne!(k, hash(0x1234_6000, 0x7f00_0000_0123, 3));
        assert_ne!(k, hash(0x1234_5000, 0x7f00_0000_0124, 3));
        assert_ne!(k, hash(0x1234_5000, 0xffff_8000_0000_0123, 3));
        assert_ne!(k, hash(0x1234_5000, 0x7f00_0000_0123, 0x8000_0003));
    }

    #[test]
    fn every_live_entered_block_is_found_after_kills() {
        let mut m = Model::new();
        // 32 keys of several frames and contexts, a table of 64: long probe runs, kills inside them.
        for n in 1..=32u32 {
            let key = (u64::from(n % 5) << 12, 0x4000_0000_0000 + u64::from(n) * 8, u64::from(n % 3));
            assert_eq!(m.place(n, key, false), Entered::New);
        }
        assert_eq!(m.place(33, (0, 0x10, 0), false), Entered::Full, "half the slots at most");
        for n in (1..=32u32).step_by(3) {
            m.kill(n);
        }
        for n in 1..=32u32 {
            let want = m.live[n as usize].then_some(n);
            assert_eq!(m.found(m.keys[n as usize]), want, "block {n}");
        }
        assert_eq!(m.entries, (1..=32).filter(|n| (n - 1) % 3 != 0).count());
    }

    #[test]
    fn a_translation_replaces_its_template_and_a_copy_does_not() {
        let mut m = Model::new();
        let key = (0x9000, 0x7f12_3440_b000, 7);
        assert_eq!(m.place(1, key, false), Entered::New);
        assert_eq!(m.place(2, key, false), Entered::Kept, "a copy of the template: the key names one block");
        assert_eq!(m.found(key), Some(1));
        assert_eq!(m.place(3, key, true), Entered::Replaced);
        assert_eq!(m.found(key), Some(3));
        // The template dies: the slot names the translation, which stays.
        m.kill(1);
        assert_eq!(m.found(key), Some(3));
        m.kill(3);
        assert_eq!(m.found(key), None);
        assert_eq!(m.entries, 0);
        assert!(m.same(2, 2), "the copy lives on, at its own RIP only");
    }

    #[test]
    fn removal_shifts_back_across_the_table_end() {
        let mut m = Model::new();
        // Keys found to share the last home slot, so their run wraps to slot 0.
        let last = m.table.len() - 1;
        let mut placed = 0;
        let mut n = 1u32;
        let mut offset = 0u64;
        while placed < 4 {
            offset += 1;
            let key = (0x5000, 0x1000_0000 + offset, 1);
            if home(hash(key.0, key.1, key.2), m.table.len()) == last {
                m.place(n, key, false);
                (placed, n) = (placed + 1, n + 1);
            }
        }
        m.kill(1);
        for k in 2..n {
            assert_eq!(m.found(m.keys[k as usize]), Some(k));
        }
    }
}
