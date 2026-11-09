//! The vCPU's resident code cache (`natemu_abi::execution::cache`): host-admitted
//! blocks placed by sequential fill into the code area, their records (key, code
//! range, access sites, guest-physical source pages) in the metadata arena, lookup
//! by (guest RIP, context) through L1 and L2, native PC → block, whole-cache
//! flush, block linking (patching a link point into a branch to its target's entry
//! and restoring it), system instructions' decodes kept as records without code
//! ([`place_system`], K2), per-page invalidation of the blocks translated from a written
//! page, the uncached single-step block in the slot, and this vCPU's side of the
//! global area shared by all vCPUs: its bits in the code-page and writable masks,
//! the invalidation records it posts to and drains, and its shadow-reset counter.
//! Every global update is an atomic read-modify-write (SeqCst). The code area's head
//! holds the slot and the dispatcher (`gates.S` `dispatch_template`), which probes L1 directly. A precise stop arms
//! the interrupted block by patching its exits to [`STOP_WORD`] ([`arm`], [`disarm`]). Blocks and L1 are this
//! vCPU's own (its code area is backed by its own memory), so a patch stops no other vCPU.
use super::*;
use natemu_abi::code_object::{Header, Reloc, Site};
use core::mem::align_of;
use core::sync::atomic::{AtomicU64, Ordering::SeqCst};
use natemu_abi::execution::{
    ENTRY_OFFSET, LinkPoint, MAX_LINK_POINTS, STATE_VA, STOP_WORD, State, System,
    cache::{self as area, Control, CpuMask, LinkRecord, Record, MASK_PAGES_PER_WORD, SLOT, SLOT_BYTES, SLOT_VA, SLOT_WRITE_VA},
};
pub use natemu_abi::execution::cache::{Block, CODE_VA, DEAD, DISPATCH, DISPATCH_MISS, DISPATCH_VA, FLUSH, GLOBAL, HEAD_BYTES, INBOUND, LARGE, LOCAL_INBOUND, MAX_VCPUS, PAGES, RELOCATABLE, REUSE_CHECK, REUSE_OFF, SHADOWS, STEP, STI_FIRST, SYSTEM};
use natemu_abi::execution::cache::{MAX_RELOCS, RELOCS_SHIFT};
/// L1's code (the dispatcher's head ends there).
pub const L1_VA: u64 = CODE_VA + area::L1_CODE as u64;

/// This vCPU's number (its state record's; checked below `MAX_VCPUS` at start).
fn vcpu() -> usize {
    unsafe { (&raw const (*(STATE_VA as *const State)).vcpu).read_volatile() as usize }
}
/// This vCPU's bit in the mask page masks.
fn own() -> CpuMask {
    1 << vcpu()
}
/// This vCPU's bit in every page mask of a mask word.
fn own_all() -> u64 {
    1 << vcpu()
}
/// Guest-physical pages of guest RAM (the VM's R / 4096; the host set `ram_bytes` at prepare).
fn pages() -> usize {
    (natemu_abi::execution::ram_end(control().ram_bytes) / 4096) as usize
}
/// Code placement alignment (a cache line).
const CODE_ALIGN: u64 = 64;

/// A placed block's access sites (they follow its record), sorted by native offset (checked by the host).
pub fn sites(b: &Block) -> &'static [Site] {
    let at = (b as *const Block).wrapping_add(1).cast::<Site>();
    unsafe { core::slice::from_raw_parts(at, b.sites_count as usize) }
}
const _: () = assert!(size_of::<Block>() % align_of::<Site>() == 0);
/// A [`RELOCATABLE`] block's packed guest-RIP relocations (perf chunk 10; they follow its sites).
pub fn relocs(b: &Block) -> &'static [u32] {
    let at = sites(b).as_ptr_range().end.cast::<u32>();
    let n = if b.flags & RELOCATABLE != 0 { b.flags >> RELOCS_SHIFT & MAX_RELOCS } else { 0 };
    unsafe { core::slice::from_raw_parts(at, n as usize) }
}
const _: () = assert!(size_of::<Site>() % 4 == 0);

fn lookup_area<T>(offset: usize) -> *mut T {
    (area::LOOKUP_VA + offset as u64) as *mut T
}
pub fn control() -> &'static mut Control {
    unsafe { &mut *lookup_area(area::CONTROL) }
}
/// {guest RIP, block number + 1}; 0 marks an empty slot.
fn l2(i: usize) -> *mut [u64; 2] {
    lookup_area::<[u64; 2]>(area::L2).wrapping_add(i)
}
fn index(n: usize) -> *mut u32 {
    lookup_area::<u32>(area::INDEX).wrapping_add(n)
}
fn block_ptr(n: usize) -> *mut Block {
    unsafe { (area::META_VA + u64::from(*index(n))) as *mut Block }
}
fn block(n: usize) -> &'static Block {
    unsafe { &*block_ptr(n) }
}
/// The single-step block's record (zero, so holding no code, until the first step).
fn step_block() -> &'static Block {
    unsafe { &*lookup_area::<Block>(area::STEP_RECORD) }
}
fn record(i: usize) -> *mut LinkRecord {
    lookup_area::<LinkRecord>(area::LINKS).wrapping_add(i)
}
fn global<T>(offset: usize) -> *mut T {
    (area::GLOBAL_VA + offset as u64) as *mut T
}
/// Monotonic physical-page dependency, published before a guest page-table load. The host sees
/// all prior marks after the resident's I/O acknowledgement; no cache/shadow reset clears them.
pub fn table_read(gpa: u64) {
    let page = (gpa >> 12) as usize;
    if page < natemu_abi::execution::MAX_GUEST_PAGES {
        let word = unsafe { AtomicU64::from_ptr(global::<u64>(area::TABLE_PAGES).add(page / 64)) };
        word.fetch_or(1 << (page % 64), SeqCst);
    }
}
/// The word of the mask at global offset `at` (code or writable pages) holding `page`'s peer bits, and their shift.
fn mask(at: usize, page: usize) -> (&'static AtomicU64, u32) {
    let (word, shift) = area::mask_location(page);
    (unsafe { AtomicU64::from_ptr(global::<u64>(at).wrapping_add(word)) }, shift)
}
/// The vCPUs whose bits are set in mask at `shift` of mask word `w`.
fn peers(w: u64, shift: u32) -> CpuMask {
    area::mask_peers(w, shift)
}
/// The code-page mask word and shift of guest-physical `gpa`; `None` outside guest RAM.
fn code_page(gpa: u64) -> Option<(&'static AtomicU64, u32)> {
    let page = (gpa >> 12) as usize;
    (page < pages()).then(|| mask(area::CODE_PAGES, page))
}
/// This vCPU's summary of the mask at `mask` (`area::CODE_SUMMARY`, `area::WRITABLE_SUMMARY`).
fn summary(mask: usize) -> *mut u64 {
    lookup_area(if mask == area::CODE_PAGES { area::CODE_SUMMARY } else { area::WRITABLE_SUMMARY })
}
/// Sets this vCPU's bit of `page` in the mask at `mask`, noting its word in the summary.
fn set_own(mask_at: usize, page: usize) {
    let (m, shift) = mask(mask_at, page);
    m.fetch_or(u64::from(own()) << shift, SeqCst);
    unsafe { *summary(mask_at).add(page / MASK_PAGES_PER_WORD / 64) |= 1 << (page / MASK_PAGES_PER_WORD % 64) };
}
/// Clears this vCPU's bits in the mask at `mask_at`: only in the words its summary names (only this vCPU sets its bits, so
/// no other word holds one; peers only clear them), and empties the summary. Its cost follows what was marked, not R:
/// zero summary words are only read, and a word whose load shows none of its bits (a peer cleared them) is not written.
fn clear_own(mask_at: usize) {
    let own = own_all();
    for k in 0..pages().div_ceil(MASK_PAGES_PER_WORD * 64) {
        let s = unsafe { summary(mask_at).add(k) };
        let mut bits = unsafe { s.read() };
        if bits == 0 {
            continue;
        }
        unsafe { s.write(0) };
        while bits != 0 {
            let w = unsafe { AtomicU64::from_ptr(global::<u64>(mask_at).wrapping_add(k * 64 + bits.trailing_zeros() as usize)) };
            if w.load(SeqCst) & own != 0 {
                w.fetch_and(!own, SeqCst);
            }
            bits &= bits - 1;
        }
    }
}
/// vCPU `v`'s invalidation record: its `pending` word and `stale` word `i`.
fn record_word(v: usize, i: Option<usize>) -> &'static AtomicU64 {
    let r = global::<Record>(area::RECORDS).wrapping_add(v);
    unsafe {
        AtomicU64::from_ptr(match i {
            None => &raw mut (*r).pending,
            Some(i) => (&raw mut (*r).stale).cast::<u64>().wrapping_add(i),
        })
    }
}
/// vCPU `v`'s invalidation record's `summary` word `k`.
fn summary_word(v: usize, k: usize) -> &'static AtomicU64 {
    let r = global::<Record>(area::RECORDS).wrapping_add(v);
    unsafe { AtomicU64::from_ptr((&raw mut (*r).summary).cast::<u64>().wrapping_add(k)) }
}
/// vCPU `v`'s shadow-reset counter.
fn resets(v: usize) -> &'static AtomicU64 {
    unsafe { AtomicU64::from_ptr(global::<u64>(area::RESETS).wrapping_add(v)) }
}

fn hash(rip: u64, context: u64) -> usize {
    let h = rip ^ context.rotate_left(47);
    (h.wrapping_mul(0x9e37_79b9_7f4a_7c15) >> (64 - area::L2_ENTRIES.trailing_zeros())) as usize
}

/// Empties the cache: bump pointers, L1, L2, block index, link records and this vCPU's code-page bits.
/// Called with no generated code live and the state banked (links already undone, see [`unlink_all`]).
pub fn reset() {
    let c = control();
    for (p, used) in c.peak.iter_mut().zip([c.code_used, c.meta_used, c.blocks]) {
        *p = (*p).max(used);
    }
    (c.code_used, c.meta_used, c.blocks, c.links, c.links_cr3, c.links_tlb) = (area::HEAD_BYTES as u64, 0, 0, 0, 0, 0);
    // A `checked` of 0 is never current.
    (c.epoch, c.global_epoch) = (c.epoch.max(1), c.global_epoch.max(1));
    (c.regions, c.large_regions, c.large_count) = ([0; 16], [0; 16], 0);
    // Block numbers start again: the current table is emptied (it keeps its context), the others hold none. Perf chunk
    // 12: no block is left to exit to a slot, so every slot is free again (each bound one made to miss), then the current
    // table's context takes one.
    let now = c.l1_table as usize;
    clear_l1(now);
    for t in (0..area::L1_TABLES).filter(|&t| t != now) {
        (c.l1_contexts[t], c.l1_used[t]) = (area::NO_CONTEXT, 0);
    }
    for k in 1..area::DISPATCH_SLOTS {
        if c.slot_contexts[k] != area::NO_CONTEXT {
            write_slot(k, None);
            c.slot_contexts[k] = area::NO_CONTEXT;
        }
    }
    c.l1_slots = [0; area::L1_TABLES];
    if c.l1_contexts[now] != area::NO_CONTEXT {
        bind_slot(now, c.l1_contexts[now]);
    }
    unsafe { ptr::write_bytes(lookup_area::<u8>(area::L2), 0, area::L2_ENTRIES * 16) };
    unsafe { ptr::write_bytes(lhead(0), 0, area::LINEAR_BUCKETS) };
    // The reverse map's heads: only pages this vCPU marked (its code summary) can hold one.
    for k in 0..pages().div_ceil(MASK_PAGES_PER_WORD * 64) {
        let mut bits = unsafe { summary(area::CODE_PAGES).add(k).read() };
        while bits != 0 {
            let first = MASK_PAGES_PER_WORD * (64 * k + bits.trailing_zeros() as usize);
            unsafe { ptr::write_bytes(head(first), 0, MASK_PAGES_PER_WORD) };
            bits &= bits - 1;
        }
    }
    clear_own(area::CODE_PAGES);
}
/// The linear map's bucket of linear page `page` ([`area::LINEAR_HEADS`]) and its head.
fn lbucket(page: u64) -> usize {
    (page.wrapping_mul(0x9e37_79b9_7f4a_7c15) >> (64 - area::LINEAR_BUCKETS.trailing_zeros())) as usize
}
fn lhead(bucket: usize) -> *mut u32 {
    lookup_area::<u32>(area::LINEAR_HEADS).wrapping_add(bucket)
}
/// The linear pages of a block's first and last guest byte (all of its bytes: a block's source is at most two pages).
fn lpages(b: &Block) -> [u64; 2] {
    [b.rip >> 12, b.rip.wrapping_add(u64::from(b.guest_bytes).max(1) - 1) >> 12]
}
/// Calls `f` with every live block with a guest byte on linear page `page`, from the page's bucket chain; dead blocks met
/// leave the chain (each once: the chain's cost follows the blocks placed). `f` must not kill blocks.
fn on_linear_page(page: u64, mut f: impl FnMut(usize)) {
    let c = control();
    let h = lbucket(page);
    let mut prev: Option<(usize, usize)> = None;
    let mut cur = unsafe { lhead(h).read() };
    while cur != 0 {
        let n = cur as usize - 1;
        let b = *block(n);
        let pages = lpages(&b);
        let k = usize::from(lbucket(pages[0]) != h);
        let next = b.lnext[k];
        c.scanned += 1;
        if b.flags & DEAD != 0 {
            match prev {
                None => unsafe { lhead(h).write(next) },
                Some((m, j)) => unsafe { (*block_ptr(m)).lnext[j] = next },
            }
        } else {
            if pages.contains(&page) {
                f(n);
            }
            prev = Some((n, k));
        }
        cur = next;
    }
}
/// The reverse map's head of guest-physical page `page` ([`area::PAGE_BLOCKS`]).
fn head(page: usize) -> *mut u32 {
    lookup_area::<u32>(area::PAGE_BLOCKS).wrapping_add(page)
}
/// Code-area offset of entry `i` of L1 table `t`.
fn l1_entry(t: usize, i: usize) -> u64 {
    (area::L1_CODE + t * area::L1_TABLE_BYTES + i * area::L1_ENTRY_BYTES) as u64
}
/// The L1 entry at `at` for `rip`, entering code-area offset `entry`: `movz/movk x2, #rip; sub x2, x2, x0; cbnz x2,
/// +8; b entry; b miss` (the dispatcher's miss; perf chunk 19: a `b`, as the tables lie beyond `cbnz`'s ±1 MiB).
fn l1_words(at: u64, rip: u64, entry: u64) -> [u32; 8] {
    let half = |k: u32| ((rip >> (16 * k)) & 0xffff) as u32;
    [
        0xd280_0002 | half(0) << 5,
        0xf2a0_0002 | half(1) << 5,
        0xf2c0_0002 | half(2) << 5,
        0xf2e0_0002 | half(3) << 5,
        0xcb00_0042,
        0xb500_0002 | 2 << 5,
        branch(at + 24, entry),
        branch(at + 28, area::DISPATCH_MISS as u64),
    ]
}
/// Entry `i` of L1 table `t`'s owner: the block it enters, number + 1 (0: empty), and [`area::L1_LISTED`].
fn l1_owner(t: usize, i: usize) -> *mut u32 {
    lookup_area::<u32>(area::L1_OWNERS).wrapping_add(t * area::L1_ENTRIES + i)
}
/// The block entry `i` of L1 table `t` enters, number + 1 (0: empty).
fn l1_block(t: usize, i: usize) -> u32 {
    unsafe { l1_owner(t, i).read() & !area::L1_LISTED }
}
/// Slot `k` of L1 table `t`'s list ([`area::L1_LISTS`]).
fn l1_list(t: usize, k: usize) -> *mut u32 {
    lookup_area::<u32>(area::L1_LISTS).wrapping_add(t * area::L1_ENTRIES + k)
}
/// Empties entry `i` of L1 table `t` (its first word branches to the dispatcher's miss; it stays on its table's list
/// if it was); the caller synchronizes the code.
fn empty_l1(t: usize, i: usize) {
    unsafe { l1_owner(t, i).write(l1_owner(t, i).read() & area::L1_LISTED) };
    let at = l1_entry(t, i);
    unsafe { ((area::CODE_WRITE_VA + at) as *mut u32).write_volatile(branch(at, area::DISPATCH_MISS as u64)) };
}
/// Empties every entry of L1 table `t` and its list.
fn clear_l1(t: usize) {
    for i in 0..area::L1_ENTRIES {
        empty_l1(t, i);
        unsafe { l1_owner(t, i).write(0) };
    }
    control().l1_listed[t] = 0;
    synchronize_l1(t);
}
fn synchronize_l1(t: usize) {
    synchronize_code(area::CODE_WRITE_VA + l1_entry(t, 0), area::CODE_VA + l1_entry(t, 0), area::L1_TABLE_BYTES);
}
/// Code-area offset of dispatcher slot `k` ([`area::DISPATCH`]; slot 0 is the dispatcher itself).
fn slot_at(k: usize) -> u64 {
    (area::DISPATCH + k * area::DISPATCH_SLOT_BYTES) as u64
}
/// The `adrp x2` at code-area offset `at` (a dispatcher's second word) to L1 table `t` (page aligned; the code area is).
fn dispatch_adr(at: u64, t: usize) -> u32 {
    let imm = (l1_entry(t, 0) >> 12).wrapping_sub(at >> 12) as u32;
    0x9000_0002 | (imm & 3) << 29 | (imm >> 2 & 0x7_ffff) << 5
}

/// Fills the code area's head once, before any block runs: the dispatcher's code (slot 0, its `adrp` written here to L1
/// table 0: the template's page offset in the resident is not the copy's) and every other slot unbound (`b` to the
/// miss). False if the dispatcher's second word is not an `adrp x2`, or if any slot's `adrp` to any table does not
/// decode to that table.
pub fn fill_head(dispatcher: &[u8]) -> bool {
    unsafe { copy_record((area::CODE_WRITE_VA + area::DISPATCH as u64) as *mut u8, dispatcher.as_ptr(), dispatcher.len()) };
    let form = word(slot_at(0) + 4) & 0x9f00_001f == 0x9000_0002;
    unsafe { ((area::CODE_WRITE_VA + slot_at(0) + 4) as *mut u32).write_volatile(dispatch_adr(slot_at(0) + 4, 0)) };
    let c = control();
    for k in 1..area::DISPATCH_SLOTS {
        let words = [branch(slot_at(k), area::DISPATCH_MISS as u64), 0, 0, 0, 0, 0, 0, 0];
        unsafe { copy_record((area::CODE_WRITE_VA + slot_at(k)) as *mut u8, words.as_ptr().cast(), area::DISPATCH_SLOT_BYTES) };
        c.slot_contexts[k] = area::NO_CONTEXT;
    }
    (c.slot_contexts[0], c.l1_slots) = (area::NO_CONTEXT, [0; area::L1_TABLES]);
    synchronize_code(area::CODE_WRITE_VA + area::DISPATCH as u64, area::DISPATCH_VA, area::DISPATCH_BYTES);
    // ADRP: immlo bits 30:29, immhi bits 23:5, a signed 21-bit count of 4 KiB pages from the instruction's page.
    let target = |at: u64, w: u32| (at & !0xfff).wrapping_add((((((w >> 29 & 3) | (w >> 5 & 0x7_ffff) << 2) << 11) as i32 >> 11) as i64 as u64) << 12);
    let adr = |k: usize, t: usize| (slot_at(k) + 4, dispatch_adr(slot_at(k) + 4, t));
    c.l1_table == 0
        && form
        && (0..area::DISPATCH_SLOTS).all(|k| (0..area::L1_TABLES).all(|t| {
            let (at, w) = adr(k, t);
            w & 0x9f00_001f == 0x9000_0002 && target(at, w) == l1_entry(t, 0)
        }))
}
/// Perf chunk 12: slot `k` names L1 table `t` (the dispatcher's words with its own `adrp`), or misses (`None`).
fn write_slot(k: usize, t: Option<usize>) {
    let at = slot_at(k);
    let words = match t {
        Some(t) => [word(slot_at(0)), dispatch_adr(at + 4, t), word(slot_at(0) + 8), word(slot_at(0) + 12)],
        None => [branch(at, area::DISPATCH_MISS as u64), 0, 0, 0],
    };
    unsafe { copy_record((area::CODE_WRITE_VA + at) as *mut u8, words.as_ptr().cast(), size_of_val(&words)) };
    synchronize_code(area::CODE_WRITE_VA + at, area::CODE_VA + at, size_of_val(&words));
    control().slot_writes += 1;
}
/// Perf chunk 12: table `t` now holds `context`'s entries: its slot (bound before, else a free one; none free: 0, its
/// blocks exit to the miss) names `t`.
fn bind_slot(t: usize, context: u64) {
    let c = control();
    let k = (1..area::DISPATCH_SLOTS)
        .find(|&k| c.slot_contexts[k] == context)
        .or_else(|| (1..area::DISPATCH_SLOTS).find(|&k| c.slot_contexts[k] == area::NO_CONTEXT))
        .unwrap_or(0);
    if k != 0 {
        c.slot_contexts[k] = context;
        write_slot(k, Some(t));
        // No other table names it any more (a context holds one table).
        for other in (0..area::L1_TABLES).filter(|&o| o != t) {
            if c.l1_slots[other] == k as u64 {
                c.l1_slots[other] = 0;
            }
        }
    }
    c.l1_slots[t] = k as u64;
}
/// Perf chunk 12: the code-area offset a computed exit of a block of `context` placed now branches to: the slot of the
/// current table's context when that is `context` ([`select_l1`] ran for it), else the miss.
pub fn dispatcher_for(context: u64) -> u64 {
    let c = control();
    let t = c.l1_table as usize;
    match c.l1_slots[t] as usize {
        k if k != 0 && c.l1_contexts[t] == context && c.slot_contexts[k] == context => slot_at(k),
        _ => area::DISPATCH_MISS as u64,
    }
}

/// L1 is keyed by RIP alone, so each table holds entries of one context, and a block's computed exit probes the table of
/// its own context through its context's dispatcher slot (perf chunk 12; [`area::DISPATCH`]): a block runs only in its
/// context, so no block is entered from L1 under another context than its own, and a change of context switches tables
/// with no code written (before, the one dispatcher's `adr` was rewritten at every change, about 90 ns). A context
/// with no table takes the least recently left one, cleared first: the old context's slot then misses (its blocks exit
/// to the resident until it takes a table again) and the new one's names the table. Every table's entries are kept as
/// exact as the current one's: a TLB flush, an invalidation or a reset reaches them all.
pub fn select_l1(context: u64) {
    let c = control();
    let now = c.l1_table as usize;
    if c.l1_contexts[now] == context {
        return;
    }
    c.l1_switches += 1;
    c.l1_used[now] = c.l1_switches;
    let t = match (0..area::L1_TABLES).find(|&t| c.l1_contexts[t] == context) {
        Some(t) => t,
        None => {
            let p = crate::perf::enter(crate::perf::L1);
            let t = (0..area::L1_TABLES).min_by_key(|&t| c.l1_used[t]).unwrap_or(0);
            // Before the table changes hands: the old context's blocks reach no entry of the new one.
            if c.l1_slots[t] != 0 {
                write_slot(c.l1_slots[t] as usize, None);
            }
            clear_l1(t);
            c.l1_contexts[t] = context;
            c.l1_clears += 1;
            bind_slot(t, context);
            crate::perf::leave(p);
            t
        }
    };
    c.l1_table = t as u64;
}
/// Enters `block` for `rip` in the dispatcher's L1 table if the block's context is the table's ([`select_l1`] ran for
/// it before the lookup that found it); otherwise nothing is entered (the block is still entered from the resident).
pub fn fill_l1(rip: u64, block: &Block) {
    let t = control().l1_table as usize;
    if control().l1_contexts[t] != block.context {
        return;
    }
    let i = (rip & area::L1_MASK) as usize;
    let mut listed = unsafe { l1_owner(t, i).read() } & area::L1_LISTED;
    if listed == 0 && block.flags & GLOBAL == 0 {
        let c = control();
        unsafe { l1_list(t, c.l1_listed[t] as usize).write(i as u32) };
        (c.l1_listed[t], listed) = (c.l1_listed[t] + 1, area::L1_LISTED);
    }
    unsafe { l1_owner(t, i).write(listed | (block.number + 1)) };
    let at = l1_entry(t, i);
    let words = l1_words(at, rip, block.entry() - area::CODE_VA);
    // Already there (a resident entry that ends at the same block again, e.g. after a system instruction): no code change.
    if (0..words.len()).all(|k| unsafe { ((area::CODE_WRITE_VA + at) as *const u32).add(k).read_volatile() } == words[k]) {
        return;
    }
    unsafe { copy_record((area::CODE_WRITE_VA + at) as *mut u8, words.as_ptr().cast(), size_of_val(&words)) };
    synchronize_code(area::CODE_WRITE_VA + at, area::CODE_VA + at, area::L1_ENTRY_BYTES);
}

/// K1: `b`'s source mapping may be used now, as a TLB entry ([`Control::epoch`]).
pub fn current(b: &Block) -> bool {
    let c = control();
    b.checked == c.epoch || (b.flags & GLOBAL != 0 && b.checked >= c.global_epoch)
}
/// Perf chunk 22: the live block cached for (`rip`, `context`) that is current now ([`current`]), looked up with no walk:
/// a TLB entry in use, as the CPU fetches through one without walking (never one of another address space: it is not
/// current). `None` also when only blocks not current are cached; [`lookup`] is then the one to revalidate them.
pub fn current_at(rip: u64, context: u64) -> Option<&'static Block> {
    cached_at(rip, context, true)
}
#[cfg(feature = "perf")]
/// Perf chunk 22 (`perf` accounting): the first live block cached for (`rip`, `context`), current or not, with no walk.
#[cfg(feature = "perf")]
pub fn kept_at(rip: u64, context: u64) -> Option<&'static Block> {
    cached_at(rip, context, false)
}
fn cached_at(rip: u64, context: u64, current_only: bool) -> Option<&'static Block> {
    let mut i = hash(rip, context);
    loop {
        let [key, number] = unsafe { l2(i).read() };
        if number == 0 {
            return None;
        }
        let b = block(number as usize - 1);
        if key == rip && b.rip == rip && b.context == context && b.flags & DEAD == 0 && (!current_only || current(b)) {
            return Some(b);
        }
        i = (i + 1) & (area::L2_ENTRIES - 1);
    }
}
/// The block cached for (`rip`, `context`) whose source pages are where `rip` is fetched from now: a current one, or one
/// `mapped` finds so (it walks the fetch of each source page's linear page: `Some(flags)` when each maps to the block's
/// page, [`GLOBAL`] and [`LARGE`] as the walk found them; the walk may write an A bit, so the block is looked at again).
/// Blocks of other address spaces at the same RIP stay for when their mapping returns.
pub fn lookup(rip: u64, context: u64, mut mapped: impl FnMut(&Block) -> Option<u32>) -> Option<&'static Block> {
    let mut i = hash(rip, context);
    loop {
        let [key, number] = unsafe { l2(i).read() };
        if number == 0 {
            return None;
        }
        let n = number as usize - 1;
        // A copy: the walk below may kill the block (its record is written then).
        let b = *block(n);
        if key == rip && b.rip == rip && b.context == context && b.flags & DEAD == 0 {
            if current(&b) {
                return Some(block(n));
            }
            let walked = mapped(&b);
            let c = control();
            if let Some(walked) = walked.filter(|_| block(n).flags & DEAD == 0) {
                c.revalidated += 1;
                unsafe { (*block_ptr(n)).checked = c.epoch };
                unsafe { (*block_ptr(n)).flags = (*block_ptr(n)).flags & !(GLOBAL | LARGE) | walked & (GLOBAL | LARGE) };
                if walked & LARGE != 0 {
                    mark_large(&b);
                }
                return Some(block(n));
            }
            c.remapped += 1;
        }
        i = (i + 1) & (area::L2_ENTRIES - 1);
    }
}
/// K1: a TLB flush of this vCPU (`global`: global entries too): the epoch moves, and every L1 entry (of every table) and link into a block
/// no longer current goes but local ones (the blocks stay, found again by [`lookup`] after a fetch walk). Returns the
/// links undone; `None` if a record does not describe its word (as [`unlink_all`]).
pub fn tlb(global: bool) -> Option<u64> {
    let c = control();
    c.epoch += 1;
    if global {
        c.global_epoch = c.epoch;
        c.global_flushes += 1;
        // No block is current: the dispatcher's table is emptied, the others hold no context any more (perf chunk 12: their
        // contexts' slots miss until those take a table again, as at a reset).
        let now = c.l1_table as usize;
        clear_l1(now);
        for t in (0..area::L1_TABLES).filter(|&t| t != now) {
            (c.l1_contexts[t], c.l1_used[t]) = (area::NO_CONTEXT, 0);
            if c.l1_slots[t] != 0 {
                write_slot(c.l1_slots[t] as usize, None);
                c.l1_slots[t] = 0;
            }
        }
    } else {
        c.cr3_loads += 1;
        // Perf chunk 19: a CR3 load moves `epoch`, not `global_epoch`, so every global block stays current: only the
        // listed entries (filled with a non-global block) are looked at. Each one emptied is cleaned to the point of
        // unification; after one barrier its instructions are invalidated as the list is compacted (an entry still
        // filled stays listed, the rest leave it).
        for t in 0..area::L1_TABLES {
            let listed = c.l1_listed[t] as usize;
            if c.l1_contexts[t] == area::NO_CONTEXT || listed == 0 {
                continue;
            }
            for k in 0..listed {
                let i = unsafe { l1_list(t, k).read() } as usize;
                let owner = l1_block(t, i);
                if owner != 0 && !current(block(owner as usize - 1)) {
                    empty_l1(t, i);
                    unsafe { asm!("dc cvau, {0}", in(reg) area::CODE_WRITE_VA + l1_entry(t, i)) };
                }
            }
            unsafe { asm!("dsb ish") };
            let mut kept = 0;
            for k in 0..listed {
                let i = unsafe { l1_list(t, k).read() } as usize;
                if l1_block(t, i) != 0 {
                    unsafe { l1_list(t, kept).write(i as u32) };
                    kept += 1;
                } else {
                    unsafe { l1_owner(t, i).write(0) };
                    unsafe { asm!("ic ivau, {0}", in(reg) area::CODE_VA + l1_entry(t, i)) };
                }
            }
            unsafe { asm!("dsb ish", "isb") };
            c.l1_listed[t] = kept as u64;
        }
    }
    // A CR3 load leaves global blocks current: only the links into non-global ones are looked at.
    prune(if global { TLB } else { CR3 }, false)
}

/// Places the admitted object at `object` (header `h`) for (`h.guest_rip`, `context`) with its guest-physical
/// source pages and link points, relocates its computed exit's `b` (at `dispatch_exit`, 0 for none;
/// the reply was checked to hold the placeholders there), and enters it in L2. `walked`: [`GLOBAL`] and [`LARGE`] as its
/// fetch walks found them (the placement is checked at this epoch, as a revalidation). `None` when the code area, the
/// metadata arena or the block index is full (the caller flushes and places again).
pub fn place(
    h: &Header,
    object: *const u8,
    context: u64,
    source_gpa: [u64; 2],
    walked: u32,
    links: [LinkPoint; MAX_LINK_POINTS],
    dispatch_exit: u64,
    rep_exits: u64,
    relocatable: bool,
    sti_first: bool,
) -> Option<&'static Block> {
    let c = control();
    // Perf chunk 10: a template keeps its guest-RIP relocations (packed after its sites); one that cannot be packed is none.
    let reloc = |i: u32| unsafe { object.add(h.relocs_offset as usize + i as usize * size_of::<Reloc>()).cast::<Reloc>().read_unaligned() };
    let packed = || (0..h.relocs_count).filter_map(move |i| natemu_abi::execution::pack_reloc(&reloc(i), h.code_size)?);
    let relocatable = relocatable
        && c.reuse != REUSE_OFF
        && h.relocs_count <= MAX_RELOCS
        && (0..h.relocs_count).all(|i| natemu_abi::execution::pack_reloc(&reloc(i), h.code_size).is_some());
    let count = if relocatable { packed().count() as u32 } else { 0 };
    let code = c.code_used.next_multiple_of(CODE_ALIGN);
    let code_end = code + u64::from(h.code_size);
    if code_end > c.code_bytes {
        c.full[0] += 1;
        return None;
    }
    let record = Block {
        rip: h.guest_rip,
        context,
        checked: c.epoch,
        source_gpa,
        next: [0; 2],
        lnext: [0; 2],
        code: code as u32,
        code_size: h.code_size,
        sites_count: h.sites_count,
        guest_bytes: h.guest_bytes,
        number: 0,
        flags: walked & (GLOBAL | LARGE) | if relocatable { RELOCATABLE | count << RELOCS_SHIFT } else { 0 } | if sti_first { STI_FIRST } else { 0 },
        dispatch_exit: dispatch_exit as u32,
        rep_exits: rep_exits as u32,
        links,
    };
    unsafe {
        // Copied above the fill point first: nothing refers to it until the record is filed.
        copy_record((area::CODE_WRITE_VA + code) as *mut u8, object.add(h.code_offset as usize), h.code_size as usize);
        relocate(code, dispatch_exit, dispatcher_for(context));
        synchronize_code(area::CODE_WRITE_VA + code, area::CODE_VA + code, h.code_size as usize);
        let placed = file(record, object.add(h.sites_offset as usize), size_of::<Site>() * h.sites_count as usize, 4 * count as usize)?;
        // In increasing order (insertion sort: at most 255; fexc lists them in emission order already).
        let table = sites(placed).as_ptr_range().end.cast::<u32>().cast_mut();
        for (i, r) in packed().take(count as usize).enumerate() {
            let mut j = i;
            while j > 0 && table.add(j - 1).read() > r {
                table.add(j).write(table.add(j - 1).read());
                j -= 1;
            }
            table.add(j).write(r);
        }
        c.code_used = code_end;
        Some(placed)
    }
}
/// Perf chunk 10: the template the hint table holds for guest-physical page `gpa` (its frame), `rip`'s half and page
/// offset and `context`, if its record still says so: live, [`RELOCATABLE`] (never a step or system record), from that
/// frame at that offset and half, in that context. Any such block is a template, whichever hint led to it.
pub fn template(rip: u64, gpa: u64, context: u64) -> Option<&'static Block> {
    let n = unsafe { hint(gpa, rip, context).read() }.checked_sub(1)? as usize;
    if n >= control().blocks as usize {
        return None;
    }
    let b = block(n);
    let fits = b.flags & (DEAD | SYSTEM | STEP | RELOCATABLE) == RELOCATABLE && b.source_gpa[0] == gpa && b.context == context;
    (fits && (b.rip ^ rip) & (1 << 63 | 0xfff) == 0).then_some(b)
}
/// The hint table's entry for (`gpa`'s frame, `rip`'s half and page offset, `context`).
fn hint(gpa: u64, rip: u64, context: u64) -> *mut u32 {
    let key = (gpa & !0xfff | rip & 0xfff) ^ (rip >> 63) << 62 ^ context.rotate_left(29);
    let i = (key.wrapping_mul(0x9e37_79b9_7f4a_7c15) >> (64 - area::HINT_ENTRIES.trailing_zeros())) as usize;
    lookup_area::<u32>(area::HINTS).wrapping_add(i)
}
/// Perf chunk 10: places a copy of template `b` for `rip` (same half and page offset; `source_gpa` its frames now, the
/// first `b`'s, the second checked by the caller; `walked` as for [`place`]), its code rewritten for `rip` by the shared
/// re-check ([`natemu_abi::execution::relocate_code`]) above the fill point, then filed as an ordinary block (with `b`'s
/// sites and relocations). `Ok(None)` when full (as [`place`]); `Err(kind)`: the re-check refused (nothing filed).
pub fn copy(b: &Block, rip: u64, source_gpa: [u64; 2], walked: u32) -> Result<Option<&'static Block>, usize> {
    let c = control();
    let code = c.code_used.next_multiple_of(CODE_ALIGN);
    let code_end = code + u64::from(b.code_size);
    if code_end > c.code_bytes {
        c.full[0] += 1;
        return Ok(None);
    }
    let mut links = b.links;
    unsafe {
        copy_record((area::CODE_WRITE_VA + code) as *mut u8, (area::CODE_WRITE_VA + u64::from(b.code)) as *const u8, b.code_size as usize);
        let at = core::slice::from_raw_parts_mut((area::CODE_WRITE_VA + code) as *mut u8, b.code_size as usize);
        natemu_abi::execution::relocate_code(at, relocs(b), &mut links, b.rip, rip, b.guest_bytes)?;
        relocate(code, u64::from(b.dispatch_exit), dispatcher_for(b.context));
        synchronize_code(area::CODE_WRITE_VA + code, area::CODE_VA + code, b.code_size as usize);
    }
    let record = Block {
        rip,
        checked: c.epoch,
        source_gpa,
        next: [0; 2],
        lnext: [0; 2],
        code: code as u32,
        number: 0,
        flags: walked & (GLOBAL | LARGE) | b.flags & (RELOCATABLE | MAX_RELOCS << RELOCS_SHIFT | STI_FIRST),
        links,
        ..*b
    };
    let tail = size_of::<Site>() * b.sites_count as usize + 4 * relocs(b).len();
    let placed = unsafe { file(record, sites(b).as_ptr().cast(), tail, 0) };
    if placed.is_some() {
        c.code_used = code_end;
    }
    Ok(placed)
}
/// K2: enters the host's decode `i` of the system instruction at `rip` (`guest_bytes` long, from `source_gpa`; `walked` as
/// for [`place`]) in the cache for `context` as a [`SYSTEM`] record (no code); `None` when the metadata arena or the
/// block index is full.
pub fn place_system(rip: u64, context: u64, source_gpa: [u64; 2], walked: u32, guest_bytes: u32, i: &System) -> Option<&'static Block> {
    // `code` at the fill point keeps the records in address order ([`block_at`]); a size of 0 holds no PC.
    let code = control().code_used as u32;
    let links = [LinkPoint::NONE; MAX_LINK_POINTS];
    let record = Block { rip, context, checked: control().epoch, source_gpa, next: [0; 2], lnext: [0; 2], code, code_size: 0, sites_count: 0, guest_bytes, number: 0, flags: SYSTEM | walked & (GLOBAL | LARGE), dispatch_exit: 0, rep_exits: 0, links };
    unsafe { file(record, (i as *const System).cast(), size_of::<System>(), 0) }
}
/// The decode a [`SYSTEM`] record holds.
pub fn system_of(b: &Block) -> System {
    unsafe { (b as *const Block).add(1).cast::<System>().read() }
}
const _: () = assert!(size_of::<Block>() % align_of::<System>() == 0);
/// Files `record` (its number assigned here) with `tail_bytes` from `tail` after it (sites, or a decode) and `extra` bytes
/// the caller writes after them (a template's relocations) in the metadata arena, the block index and L2 (and, when
/// [`RELOCATABLE`], the hint table); `None` when either is full.
unsafe fn file(record: Block, tail: *const u8, tail_bytes: usize, extra: usize) -> Option<&'static Block> {
    let c = control();
    let meta = c.meta_used.next_multiple_of(align_of::<Block>() as u64);
    let meta_end = meta + (size_of::<Block>() + tail_bytes + extra) as u64;
    if meta_end > area::META_BYTES as u64 || c.blocks as usize >= area::MAX_BLOCKS {
        c.full[if meta_end > area::META_BYTES as u64 { 1 } else { 2 }] += 1;
        return None;
    }
    let n = c.blocks as usize;
    unsafe {
        let at = (area::META_VA + meta) as *mut Block;
        // Chained into each source page's list (pages outside guest RAM hold no code: fetches need RAM).
        let mut next = [0; 2];
        for (k, &g) in record.source_gpa.iter().enumerate() {
            let page = (g >> 12) as usize;
            if g != u64::MAX && page < pages() {
                next[k] = head(page).read();
                head(page).write(n as u32 + 1);
                // In the code summary even if a reset came between its mark and here, so the next reset clears the head.
                *summary(area::CODE_PAGES).add(page / MASK_PAGES_PER_WORD / 64) |= 1 << (page / MASK_PAGES_PER_WORD % 64);
            }
        }
        let mut lnext = [0; 2];
        let [b0, b1] = lpages(&record).map(lbucket);
        lnext[0] = lhead(b0).read();
        lhead(b0).write(n as u32 + 1);
        if b1 != b0 {
            lnext[1] = lhead(b1).read();
            lhead(b1).write(n as u32 + 1);
        }
        at.write(Block { number: n as u32, next, lnext, ..record });
        mark_regions(&mut c.regions, &record);
        if record.flags & LARGE != 0 {
            mark_large(&record);
        }
        copy_record(at.add(1).cast(), tail, tail_bytes);
        index(n).write(meta as u32);
        let mut i = hash(record.rip, record.context);
        while l2(i).read()[1] != 0 {
            i = (i + 1) & (area::L2_ENTRIES - 1);
        }
        l2(i).write([record.rip, n as u64 + 1]);
        if record.flags & RELOCATABLE != 0 {
            hint(record.source_gpa[0], record.rip, record.context).write(n as u32 + 1);
        }
        (c.meta_used, c.blocks) = (meta_end, n as u64 + 1);
        Some(&*at)
    }
}

/// Places the admitted object at `object` as the single-step block in the slot: uncached (not in the index, L1 or L2,
/// never linked) and marking no source page, so the write it steps through completes (it drops the page's blocks
/// itself, see the runtime's `code_written`). Every exit of it enters the resident: its literal exits are never linked,
/// and its computed exit branches to the dispatcher's miss (`svc #MISS`), not through L1 (X2: the boundary after a
/// stepped instruction is where a pending event is taken). `None` if it does not fit the slot or the record area.
pub fn place_step(
    h: &Header,
    object: *const u8,
    context: u64,
    source_gpa: [u64; 2],
    links: [LinkPoint; MAX_LINK_POINTS],
    dispatch_exit: u64,
    rep_exits: u64,
) -> Option<&'static Block> {
    let sites = size_of::<Site>() * h.sites_count as usize;
    if size_of::<Block>() + sites > area::STEP_RECORD_BYTES
        || h.code_size as usize > SLOT_BYTES
        || h.guest_insns != 1
        || h.guest_bytes > 15
    {
        return None;
    }
    let record = Block {
        rip: h.guest_rip,
        context,
        checked: 0,
        source_gpa,
        next: [0; 2],
        lnext: [0; 2],
        code: SLOT as u32,
        code_size: h.code_size,
        sites_count: h.sites_count,
        guest_bytes: h.guest_bytes,
        number: u32::MAX,
        flags: STEP,
        dispatch_exit: dispatch_exit as u32,
        rep_exits: rep_exits as u32,
        links,
    };
    unsafe {
        let at = lookup_area::<Block>(area::STEP_RECORD);
        at.write(record);
        copy_record(at.add(1).cast(), object.add(h.sites_offset as usize), sites);
        copy_record(SLOT_WRITE_VA as *mut u8, object.add(h.code_offset as usize), h.code_size as usize);
        relocate(SLOT as u64, dispatch_exit, DISPATCH_MISS as u64);
        synchronize_code(SLOT_WRITE_VA, SLOT_VA, h.code_size as usize);
        Some(&*at)
    }
}

/// `bytes` of code written at `write` (a writable alias) become visible to instruction fetch at `exec` (its executable alias).
fn synchronize_code(write: u64, exec: u64, bytes: usize) {
    unsafe {
        let ctr: u64;
        asm!("mrs {0}, ctr_el0", out(reg) ctr);
        let (dline, iline) = (4u64 << ((ctr >> 16) & 15), 4u64 << (ctr & 15));
        let mut at = 0;
        while at < bytes as u64 + (write & (dline - 1)) {
            asm!("dc cvau, {0}", in(reg) (write & !(dline - 1)) + at);
            at += dline;
        }
        asm!("dsb ish");
        at = 0;
        while at < bytes as u64 + (exec & (iline - 1)) {
            asm!("ic ivau, {0}", in(reg) (exec & !(iline - 1)) + at);
            at += iline;
        }
        asm!("dsb ish", "isb");
    }
}

/// Code-area word at `offset` (read through the writable alias: the same memory).
fn word(offset: u64) -> u32 {
    unsafe { ((area::CODE_WRITE_VA + offset) as *const u32).read_volatile() }
}
/// Rewrites one placed word and makes it visible to instruction fetch.
fn rewrite(offset: u64, w: u32) {
    unsafe { ((area::CODE_WRITE_VA + offset) as *mut u32).write_volatile(w) };
    synchronize_code(area::CODE_WRITE_VA + offset, area::CODE_VA + offset, 4);
}
/// `b` from code-area offset `from` to `to` (the area is at most 128 MiB, so every target is in range).
fn branch(from: u64, to: u64) -> u32 {
    0x1400_0000 | (to.wrapping_sub(from) >> 2) as u32 & 0x03ff_ffff
}
/// Relocates the placeholder of the block just copied to code-area offset `code` (before its code is synchronized):
/// its computed exit (at `dispatch_exit`, 0 for none) becomes `b` to `to` (its context's dispatcher slot, or the miss,
/// [`dispatcher_for`]). (The placeholder was checked at the reply.)
fn relocate(code: u64, dispatch_exit: u64, to: u64) {
    if dispatch_exit != 0 {
        let at = code + dispatch_exit;
        unsafe { ((area::CODE_WRITE_VA + at) as *mut u32).write_volatile(branch(at, to)) };
    }
}

/// Arms a precise stop in `b` (the block the doorbell interrupted; live, or the step block): every exit's word (each
/// link point's first word, linked or not, and the computed exit's `b`) becomes [`STOP_WORD`], recorded with its word
/// for [`disarm`]. Every path out of a block passes one of them or enters the resident (`svc`, a stub, an abort):
/// branches inside a block go forward and none enters a link point after its first word (admission). False (nothing
/// patched) when a stop is armed already: every resident entry disarms first, so a second arm is a resident bug.
pub fn arm(b: &Block) -> bool {
    let c = control();
    if c.armed != 0 {
        return false;
    }
    let points = b.links.iter().filter(|p| p.offset != 0).map(|p| p.offset);
    let exits = points.chain((b.dispatch_exit != 0).then_some(b.dispatch_exit));
    for offset in exits {
        let at = u64::from(b.code) + u64::from(offset);
        c.armed_words[c.armed as usize] = [at as u32, word(at)];
        c.armed += 1;
        unsafe { ((area::CODE_WRITE_VA + at) as *mut u32).write_volatile(STOP_WORD) };
    }
    synchronize_words(c);
    true
}
/// Restores the words [`arm`] patched (each must still hold [`STOP_WORD`]) and empties the record: at every resident
/// entry from generated code, before anything reads or rewrites code. Returns the restored code-area offsets (the
/// first `n`), or `None` (nothing restored) when the record is malformed.
pub fn disarm() -> Option<([u32; MAX_LINK_POINTS + 1], usize)> {
    let c = control();
    let n = c.armed as usize;
    let mut at = [0; MAX_LINK_POINTS + 1];
    if n == 0 {
        return Some((at, 0));
    }
    let words = c.armed_words.get(..n)?;
    if words.iter().any(|&[o, _]| u64::from(o) >= c.code_bytes || o % 4 != 0 || word(u64::from(o)) != STOP_WORD) {
        return None;
    }
    for (i, &[o, original]) in words.iter().enumerate() {
        at[i] = o;
        unsafe { ((area::CODE_WRITE_VA + u64::from(o)) as *mut u32).write_volatile(original) };
    }
    synchronize_words(c);
    c.armed = 0;
    Some((at, n))
}
/// The armed record's words become visible to instruction fetch (one barrier pair for all of them).
fn synchronize_words(c: &Control) {
    unsafe {
        for &[o, _] in &c.armed_words[..c.armed as usize] {
            asm!("dc cvau, {0}", in(reg) area::CODE_WRITE_VA + u64::from(o));
        }
        asm!("dsb ish");
        for &[o, _] in &c.armed_words[..c.armed as usize] {
            asm!("ic ivau, {0}", in(reg) area::CODE_VA + u64::from(o));
        }
        asm!("dsb ish", "isb");
    }
}
/// The link point's word is its admitted form (the first word of its RIP move).
fn unlinked(b: &Block, p: &LinkPoint) -> bool {
    word(u64::from(b.code) + u64::from(p.offset)) == p.word
}

pub enum Link {
    /// This many link points now branch to the target.
    Linked(u64),
    /// `source` has no link point to the target, or one holds a word the cache did not put there.
    Bad,
}
/// Links every link point of `source` to `target.rip` (in the same context) to `target`: its first word becomes `b`
/// to the target's entry, recorded with the original word in the segment of its kind ([`Control::links_tlb`]: local,
/// else by the target's [`GLOBAL`], which stays as it is while the target is current, that is while the link is there).
/// Link points already branching there stay, unless the record area is full: then links are dropped first.
pub fn link(source: &Block, target: &Block) -> Link {
    let c = control();
    let points = || source.links.iter().filter(|p| p.offset != 0 && p.target == target.rip);
    if source.context != target.context || points().next().is_none() {
        return Link::Bad;
    }
    let entry = u64::from(target.code) + u64::from(ENTRY_OFFSET);
    for p in points() {
        let at = u64::from(source.code) + u64::from(p.offset);
        if !unlinked(source, p) && word(at) != branch(at, entry) {
            return Link::Bad;
        }
    }
    if c.links as usize + MAX_LINK_POINTS > area::MAX_LINKS {
        // Full (perf chunk 11): any link may go, its point then enters the linker again. Local links outlive their
        // targets' currency, so those into blocks not current go first (chunk 8); if that leaves less than a quarter
        // free, every link goes. Each such pass is followed by at least a quarter of the area's links: amortised O(1)
        // per link (before, a pass that freed nothing was repeated at every linker entry).
        c.reclaims += 1;
        let Some(mut n) = prune(LOCAL, true) else { return Link::Bad };
        if control().links as usize > area::MAX_LINKS - area::MAX_LINKS / 4 {
            let Some(all) = unlink_all() else { return Link::Bad };
            n += all;
        }
        control().reclaimed += n;
    }
    let (segment, inbound) = if local(target, source) {
        (LOCAL, LOCAL_INBOUND)
    } else if target.flags & GLOBAL != 0 {
        (TLB, INBOUND)
    } else {
        (CR3, INBOUND)
    };
    let mut linked = 0;
    for p in points().filter(|p| unlinked(source, p)) {
        let at = u64::from(source.code) + u64::from(p.offset);
        let original = word(at);
        rewrite(at, branch(at, entry));
        insert(LinkRecord { source: source.number, offset: p.offset, original, target: target.number }, segment);
        unsafe { (*block_ptr(target.number as usize)).flags |= inbound };
        linked += 1;
    }
    Link::Linked(linked)
}
/// Perf chunk 8: a link from `source` into `target` is local when every linear page of `target` is a page of `source`
/// translated from the same guest-physical page. It may then stay while `target` is not current: `source` runs only
/// under a translation of its pages that is valid now (it was entered current, or by a local link from a block that
/// was), and `target` reads only those pages, through the same frames, in the same context (CPL and gating).
fn local(target: &Block, source: &Block) -> bool {
    let pages = |b: &Block| [(b.rip >> 12, b.source_gpa[0]), ((b.rip >> 12).wrapping_add(1), b.source_gpa[1])];
    pages(target).iter().all(|&(p, g)| g == u64::MAX || pages(source).contains(&(p, g)))
}
/// The link records' segments ([`Control::links_cr3`], `links_tlb`, `links`), by which events look at them.
const CR3: usize = 0;
const TLB: usize = 1;
const LOCAL: usize = 2;
fn ends(c: &mut Control) -> [&mut u64; 3] {
    [&mut c.links_cr3, &mut c.links_tlb, &mut c.links]
}
/// Appends `r` to segment `k`: each later segment's first record moves to that segment's end.
fn insert(r: LinkRecord, k: usize) {
    let mut ends = ends(control());
    let mut hole = *ends[LOCAL];
    for j in (k + 1..=LOCAL).rev() {
        let first = *ends[j - 1];
        unsafe { record(hole as usize).write(record(first as usize).read()) };
        hole = first;
    }
    unsafe { record(hole as usize).write(r) };
    for e in &mut ends[k..] {
        **e += 1;
    }
}
/// Drops record `i` of segment `k`: its segment's last record fills it, then each later segment's last record fills the
/// slot so freed.
fn remove(i: u64, k: usize) {
    let ends = ends(control());
    let mut hole = i;
    for e in ends.into_iter().skip(k) {
        *e -= 1;
        unsafe { record(hole as usize).write(record(*e as usize).read()) };
        hole = *e;
    }
}
/// Restores every linked point's original word and drops every record (the invalidation of all blocks at a flush).
/// `None` if a record does not describe its word (it is not left as is).
pub fn unlink_all() -> Option<u64> {
    let c = control();
    for i in 0..c.links as usize {
        unlink(unsafe { record(i).read() })?;
    }
    let n = c.links;
    (c.links, c.links_cr3, c.links_tlb) = (0, 0, 0);
    Some(n)
}
/// Restores the original word of the link point `r` describes, if it holds the branch `r` says.
fn unlink(r: LinkRecord) -> Option<()> {
    let (source, target) = (block(r.source as usize), block(r.target as usize));
    let at = u64::from(source.code) + u64::from(r.offset);
    let entry = u64::from(target.code) + u64::from(ENTRY_OFFSET);
    (word(at) == branch(at, entry)).then(|| rewrite(at, r.original))
}

/// This vCPU is about to write `gpa`'s page, which holds translations: clears every vCPU's code bit of the page at
/// once, posts the page to each other vCPU that had one (page bit, then `PAGES`, then a kick: it drops its blocks at
/// its next block entry) and invalidates this vCPU's own blocks from it ([`invalidate`]). A write through the RAM
/// alias is followed by [`rechecked`].
pub fn written(gpa: u64) -> Option<u64> {
    if let Some((m, shift)) = code_page(gpa) {
        post(peers(m.fetch_and(!(u64::from(CpuMask::MAX) << shift), SeqCst), shift) & !own(), gpa);
    }
    invalidate(gpa)
}
/// This vCPU wrote `gpa`'s page through the RAM alias (a split store's part, a walker's A/D update): a peer that
/// marked the page after [`written`] cleared it, or after a check found it unmarked, may have been given the old
/// bytes. Posts the page to every such peer and clears their bits (this vCPU marks no page while it writes one).
pub fn rechecked(gpa: u64) {
    if let Some((m, shift)) = code_page(gpa) {
        unsafe { asm!("dmb ish") };
        let others = !own();
        if peers(m.load(SeqCst), shift) & others != 0 {
            post(peers(m.fetch_and(!(u64::from(others) << shift), SeqCst), shift) & others, gpa);
        }
    }
}
/// Posts `gpa`'s page to the vCPUs whose bits are set in `peers` and kicks them.
fn post(peers: CpuMask, gpa: u64) {
    let page = (gpa >> 12) as usize;
    for v in (0..MAX_VCPUS).filter(|v| peers & 1 << v != 0) {
        record_word(v, Some(page / 64)).fetch_or(1 << (page % 64), SeqCst);
        summary_word(v, page / 64 / 64).fetch_or(1 << (page / 64 % 64), SeqCst);
        record_word(v, None).fetch_or(PAGES, SeqCst);
        kick(v);
    }
}
/// Rings vCPU `v`'s doorbell (SGI to its grouped Arm affinity): it stops at its next block boundary and
/// drains its record. The `dsb` makes this vCPU's record updates visible before the interrupt.
pub fn kick(v: usize) {
    let c = control();
    let sgi = natemu_abi::execution::vcpu_sgi(v);
    let (t0, t1): (u64, u64);
    unsafe {
        asm!("dsb ish", "isb", "mrs {t0}, cntvct_el0", "msr S3_0_C12_C11_5, {sgi}", "isb", "mrs {t1}, cntvct_el0",
             sgi = in(reg) sgi, t0 = out(reg) t0, t1 = out(reg) t1);
    }
    c.sgis += 1;
    c.sgi_ticks += t1 - t0;
}

/// Invalidates every live block of this vCPU translated from `gpa`'s page (the page is about to be written, or was):
/// each leaves L1 (every table) and L2, every link into or out of it is undone and its record dropped, and it is marked dead. Then
/// this vCPU's bit for the page is cleared. Returns the links undone; `None` if a record does not describe its word
/// (as [`unlink_all`]). K1: the blocks are found from the page's chain ([`area::PAGE_BLOCKS`]), which then empties (every
/// block on it is dead).
pub fn invalidate(gpa: u64) -> Option<u64> {
    let page = (gpa >> 12) as usize;
    if page >= pages() {
        return Some(0);
    }
    let first = unsafe { head(page).read() };
    let chain = core::iter::successors(Some(first).filter(|&x| x != 0), |&x| {
        let b = block(x as usize - 1);
        let k = usize::from(b.source_gpa[0] >> 12 != page as u64);
        Some(b.next[k]).filter(|&y| y != 0)
    });
    let unlinked = cut(chain.map(|x| x as usize - 1), true)?;
    unsafe { head(page).write(0) };
    if let Some((m, shift)) = code_page(gpa) {
        m.fetch_and(!(u64::from(own()) << shift), SeqCst);
    }
    Some(unlinked)
}
/// Invalidates the live block `b` alone (as [`invalidate`], its pages' marks untouched): a block placed from a page
/// whose mark this vCPU lost while it was fetched.
pub fn discard(b: &Block) -> Option<u64> {
    cut(core::iter::once(b.number as usize), true)
}
/// The bit of linear 2 MiB region `r` in [`Control::regions`].
fn region(r: u64) -> (usize, u64) {
    ((r % 1024 / 64) as usize, 1 << (r % 64))
}
/// Sets the bits of `b`'s 2 MiB regions (its first and last byte's) in `regions`.
fn mark_regions(regions: &mut [u64; 16], b: &Block) {
    for r in [b.rip >> 21, b.rip.wrapping_add(u64::from(b.guest_bytes).max(1) - 1) >> 21] {
        let (w, bit) = region(r);
        regions[w] |= bit;
    }
}
/// Records `b`'s 2 MiB regions as [`mark_regions`] does, in [`Control::large_regions`] and, by whole region number, in
/// [`Control::large_list`] (perf chunk 19: the bits alias modulo 1024, so a #PF in a region sharing a bit with one that
/// held large blocks scanned 512 chains for nothing).
fn mark_large(b: &Block) {
    let c = control();
    mark_regions(&mut c.large_regions, b);
    for r in [b.rip >> 21, b.rip.wrapping_add(u64::from(b.guest_bytes).max(1) - 1) >> 21] {
        let listed = (c.large_count as usize).min(area::LARGE_REGIONS);
        if !c.large_list[..listed].contains(&r) {
            if listed < area::LARGE_REGIONS {
                c.large_list[listed] = r;
            }
            c.large_count += 1;
        }
    }
}
/// Region `r` may hold a [`LARGE`] block: its bit is set and, while the list is complete, it is listed.
fn large_held(c: &Control, r: u64) -> bool {
    let (w, bit) = region(r);
    c.large_regions[w] & bit != 0
        && (c.large_count as usize > area::LARGE_REGIONS || c.large_list[..c.large_count as usize].contains(&r))
}
/// A guest INVLPG of `address`, or a #PF at it (K1; SDM Vol.3 4.10.4.1: the TLB entries for its page, global ones too,
/// and all of them for a page larger than 4 KiB): every live block of this vCPU with guest bytes on its 4 KiB page, and
/// every [`LARGE`] one with bytes in its 2 MiB region, is no longer current (`checked` 0, global or not): out of L1, every
/// link from another page into it undone; it stays in L2 and is found again only after a fetch walk ([`lookup`]). Other
/// blocks keep their translations (a 4 KiB TLB entry of another page is not one for this page). Its code-page bits
/// stay: they only say it may hold translations. Blocks are looked for only when the address's 2 MiB region may hold
/// one ([`Control::regions`]; for large blocks [`large_held`]), then on the linear map's chains of the pages
/// ([`area::LINEAR_HEADS`]).
pub fn invlpg(address: u64) -> Option<u64> {
    let c = control();
    let r = address >> 21;
    let held = |regions: &[u64; 16]| {
        let (w, bit) = region(r);
        regions[w] & bit != 0
    };
    let mut inbound = false;
    if held(&c.regions) {
        on_linear_page(address >> 12, |n| inbound |= cut_one(n, false).is_some());
    }
    if large_held(c, r) {
        for page in r << 9..(r + 1) << 9 {
            on_linear_page(page, |n| inbound |= block(n).flags & LARGE != 0 && cut_one(n, false).is_some());
        }
    }
    if inbound { prune(TLB, false) } else { Some(0) }
}
/// Takes block `n`'s entry out of L2 (linear probing's backward-shift deletion), so probes never pass dead blocks: a
/// page rewritten over and over leaves no trail of dead copies at its RIPs. A [`lookup`] under way (its walk may kill)
/// at worst misses its block and asks for it again.
fn l2_remove(n: usize) {
    let b = *block(n);
    let mask = area::L2_ENTRIES - 1;
    let mut i = hash(b.rip, b.context);
    loop {
        let [_, number] = unsafe { l2(i).read() };
        if number == 0 {
            return; // not filed (cannot happen)
        }
        if number as usize == n + 1 {
            break;
        }
        i = (i + 1) & mask;
    }
    let mut j = i;
    loop {
        j = (j + 1) & mask;
        let [key, number] = unsafe { l2(j).read() };
        if number == 0 {
            unsafe { l2(i).write([0, 0]) };
            return;
        }
        // The entry at j moves into the hole at i unless its home lies cyclically in (i, j].
        let home = hash(key, block(number as usize - 1).context);
        if (j.wrapping_sub(home) & mask) >= (j.wrapping_sub(i) & mask) {
            unsafe { l2(i).write([key, number]) };
            i = j;
        }
    }
}
/// Kills (`dead`) the blocks numbered `doomed`, or makes them no longer current: each leaves L1; then one pass over the
/// link records undoes every link into a dead block and out of one, and every link from another page into a block no
/// longer current ([`prune`]; links out of a block only not current stay: they run only once it is entered, which needs
/// a fetch walk first, and so do local links into it). Dead blocks leave L2 (as
/// [`lookup`] skips them). Returns the links undone; `None` if a record does not describe its word.
fn cut(doomed: impl Iterator<Item = usize>, dead: bool) -> Option<u64> {
    let mut scope = None;
    for n in doomed {
        scope = scope.max(cut_one(n, dead));
    }
    scope.map_or(Some(0), |k| prune(k, false))
}
/// [`cut`]'s work on block `n` but the link records: the segments that need a pass, if any (links from other pages may
/// enter it: up to [`TLB`]; it is dead and local links may enter it, or links may leave it: all).
fn cut_one(n: usize, dead: bool) -> Option<usize> {
    let c = control();
    let b = *block(n);
    if b.flags & DEAD != 0 || (!dead && b.checked == 0) {
        return None;
    }
    let scope = if dead && (b.flags & LOCAL_INBOUND != 0 || b.links.iter().any(|p| p.offset != 0)) {
        Some(LOCAL)
    } else if b.flags & INBOUND != 0 {
        Some(TLB)
    } else {
        None
    };
    unsafe { (*block_ptr(n)).flags &= !INBOUND };
    if dead {
        unsafe { (*block_ptr(n)).flags |= DEAD };
        c.killed += 1;
        l2_remove(n);
    } else {
        unsafe { (*block_ptr(n)).checked = 0 };
        c.unvalidated += 1;
    }
    let i = (b.rip & area::L1_MASK) as usize;
    for t in 0..area::L1_TABLES {
        if l1_block(t, i) == b.number + 1 {
            empty_l1(t, i);
            let at = l1_entry(t, i);
            synchronize_code(area::CODE_WRITE_VA + at, area::CODE_VA + at, 4);
        }
    }
    scope
}
/// In the link records of segments up to `scope` ([`CR3`], [`TLB`], [`LOCAL`]), undoes every link whose source or target
/// is dead, or, from another page (or any, if `strict`), whose target is no longer current (a local link may stay: see
/// [`local`]), and drops its record. The caller names every segment such a link may be in.
fn prune(scope: usize, strict: bool) -> Option<u64> {
    let mut unlinked = 0;
    let (mut i, mut k) = (0, 0);
    loop {
        let c = control();
        while k <= scope && i >= *ends(c)[k] {
            k += 1;
        }
        if k > scope {
            return Some(unlinked);
        }
        let r = unsafe { record(i as usize).read() };
        let target = block(r.target as usize);
        if target.flags & DEAD == 0 && block(r.source as usize).flags & DEAD == 0 && ((k == LOCAL && !strict) || current(target)) {
            i += 1;
            continue;
        }
        unlink(r)?;
        unlinked += 1;
        remove(i, k);
    }
}
/// Self-check: every link point of every placed block holds its admitted word (after [`unlink_all`]).
pub fn links_restored() -> bool {
    (0..control().blocks as usize).all(|n| {
        let b = block(n);
        b.links.iter().filter(|p| p.offset != 0).all(|p| unlinked(b, p))
    })
}

/// The block whose code holds native address `pc` (the single-step block in the slot, or a cache block, dead ones too;
/// never the head's dispatcher or L1).
pub fn block_at(pc: u64) -> Option<&'static Block> {
    if let Some(offset) = pc.checked_sub(SLOT_VA).filter(|&o| o < SLOT_BYTES as u64) {
        let b = step_block();
        return (b.flags & STEP != 0 && offset < u64::from(b.code_size)).then_some(b);
    }
    let offset = pc.checked_sub(area::CODE_VA)?;
    let count = control().blocks as usize;
    let holds = |b: &Block| u64::from(b.code) <= offset && offset < u64::from(b.code) + u64::from(b.code_size);
    // Perf chunk 12: the block found at this word last ([`Control::pc_hints`]).
    let hint = (offset >> 2) as usize % area::PC_HINTS;
    let n = control().pc_hints[hint] as usize;
    if n < count && holds(block(n)) {
        return Some(block(n));
    }
    // The last block starting at or below `offset` (placement order is address order).
    let (mut lo, mut hi) = (0, count);
    while lo < hi {
        let mid = (lo + hi) / 2;
        if u64::from(block(mid).code) <= offset {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    let n = lo.checked_sub(1)?;
    let b = block(n);
    if !holds(b) {
        return None;
    }
    control().pc_hints[hint] = n as u32;
    Some(b)
}

/// Marks `gpa`'s page as holding this vCPU's translations, before its bytes are copied for a block: from here on a
/// write to the page posts it to this vCPU. Returns the vCPUs that may hold a writable shadow of it, read after the
/// mark (a writable install sets its bit before it reads the marks, so it either sees this mark or is returned here).
pub fn mark(gpa: u64) -> CpuMask {
    if code_page(gpa).is_none() {
        return 0;
    }
    set_own(area::CODE_PAGES, (gpa >> 12) as usize);
    writable(gpa)
}
/// This vCPU's mark of `gpa`'s page is still set (no write cleared it since [`mark`]); pages outside guest RAM count as
/// marked (no code is fetched from them).
pub fn marked(gpa: u64) -> bool {
    code_page(gpa).is_none_or(|(m, shift)| peers(m.load(SeqCst), shift) & own() != 0)
}
/// The vCPUs that may hold a writable shadow of `gpa`'s page.
pub fn writable(gpa: u64) -> CpuMask {
    let page = (gpa >> 12) as usize;
    if page >= pages() {
        return 0;
    }
    let (m, shift) = mask(area::WRITABLE_PAGES, page);
    peers(m.load(SeqCst), shift)
}
/// vCPU `v`'s shadow-reset count (it moves each time `v` drops all its shadows, see [`shadows_reset`]).
pub fn resets_of(v: usize) -> u64 {
    resets(v).load(SeqCst)
}
/// Asks vCPU `v` to drop all its shadows ([`SHADOWS`], then a kick).
pub fn ask_shadows(v: usize) {
    record_word(v, None).fetch_or(SHADOWS, SeqCst);
    kick(v);
}
/// Some vCPU holds translations from `gpa`'s page: shadows of it stay read-only.
pub fn translated(gpa: u64) -> bool {
    code_page(gpa).is_some_and(|(m, shift)| peers(m.load(SeqCst), shift) != 0)
}
/// Records that this vCPU shadows `gpa`'s page writable.
pub fn shadowed_writable(gpa: u64) {
    let page = (gpa >> 12) as usize;
    if page < pages() {
        set_own(area::WRITABLE_PAGES, page);
    }
}
/// This vCPU's shadows were all removed (and the TLB invalidated): clears its writable bits, then counts the reset
/// (a peer waiting for this vCPU to drop its writable shadows waits for the counter to change).
pub fn shadows_reset() {
    split_word(3 + vcpu()).store(0, SeqCst);
    clear_own(area::WRITABLE_PAGES);
    resets(vcpu()).fetch_add(1, SeqCst);
}

/// Split locks (no-stop change, November 6, 2026): a LOCKed access (or XCHG, or CMPXCHG8B/16B) crossing 16 bytes, which
/// no Arm atomic performs, is made by the resident with plain loads and stores while no other vCPU can access its pages
/// ([`split_begin`] .. [`split_end`]): one split lock for the VM; its owner names the pages, then makes every peer that may
/// hold a shadow drop all of them ([`SHADOWS`], a kick) and waits for each reset, so every peer access to the pages from
/// then on misses its shadows and waits in its resident ([`split_wait`], before installing a shadow or making an access of
/// its own) until the owner is done. `serve` is called while waiting (the caller drains its record: two vCPUs waiting for
/// each other both progress). The x86 locked access is atomic against every access of every vCPU and orders as a fence:
/// a peer's reset completes its earlier stores (the TLB invalidation's `dsb`), and the owner's stores complete before it
/// releases. While waiting, a vCPU serves only shadow-drop requests ([`take_shadows`]): enough for every wait to progress
/// (peers wait on one another only for shadow resets), and safe with a block of its own live.
fn split_word(k: usize) -> &'static AtomicU64 {
    unsafe { AtomicU64::from_ptr(global::<u64>(area::SPLIT).wrapping_add(k)) }
}
/// This vCPU may hold a shadow from now on (set before a shadow is installed, cleared by [`shadows_reset`]).
pub fn holding() {
    split_word(3 + vcpu()).store(1, SeqCst);
}
pub fn split_begin(gpa: [u64; 2], vcpus: usize, mut serve: impl FnMut()) {
    while split_word(0).compare_exchange(0, vcpu() as u64 + 1, SeqCst, SeqCst).is_err() {
        serve();
        core::hint::spin_loop();
    }
    split_word(1).store((gpa[0] >> 12) + 1, SeqCst);
    split_word(2).store((gpa[1] >> 12) + 1, SeqCst);
    let mut counts = [0; area::MAX_VCPUS];
    let mut requested = 0u64;
    for p in (0..vcpus).filter(|&p| p != vcpu()) {
        counts[p] = resets_of(p);
        if split_word(3 + p).load(SeqCst) == 0 {
            continue;
        }
        requested |= 1 << p;
        ask_shadows(p);
    }
    // Let peers reset concurrently; serialize only the locked memory access.
    while requested != 0 {
        let p = requested.trailing_zeros() as usize;
        while resets_of(p) == counts[p] {
            serve();
            core::hint::spin_loop();
        }
        requested &= requested - 1;
    }
}
pub fn split_end() {
    unsafe { asm!("dsb ish") };
    split_word(1).store(0, SeqCst);
    split_word(2).store(0, SeqCst);
    split_word(0).store(0, SeqCst);
}
/// While waiting on a split lock: serves only a peer's request to drop all shadows ([`SHADOWS`]; whether to reset them now),
/// leaving the rest of the record (code invalidations, a flush) to the next resident entry, as a block of this vCPU may be
/// live here (its abort handler resumes it).
pub fn take_shadows() -> bool {
    record_word(vcpu(), None).fetch_and(!SHADOWS, SeqCst) & SHADOWS != 0
}
/// Another vCPU holds the split lock over `gpa`'s page.
pub fn split_held(gpa: u64) -> bool {
    let page = (gpa >> 12) + 1;
    split_word(0).load(SeqCst) != vcpu() as u64 + 1 && (split_word(1).load(SeqCst) == page || split_word(2).load(SeqCst) == page)
}

/// Perf chunk 22: [`drain`] would find something pending (its first load).
pub fn pending() -> bool {
    record_word(vcpu(), None).load(SeqCst) != 0
}
/// Takes this vCPU's pending invalidations: swaps `pending` to 0, then each summary word and each stale word it names
/// (a word that loads 0 is skipped unwritten: a bit set after that load comes with a later `pending`, as its producer
/// sets the stale bit, then the summary bit, then `pending`), and invalidates each listed
/// page ([`invalidate`], whose `None` this returns) unless the whole cache goes anyway. Returns the pending bits;
/// the caller flushes on [`FLUSH`] and resets its shadows on [`SHADOWS`].
pub fn drain() -> Option<u64> {
    // Perf chunk 12: a load first, the swap only when something is pending (a swap returning 0 changes nothing; a bit
    // posted after the load is taken at the next drain, as one posted after a swap).
    let word = record_word(vcpu(), None);
    if word.load(SeqCst) == 0 {
        return Some(0);
    }
    let pending = word.swap(0, SeqCst);
    if pending & PAGES != 0 {
        for k in 0..pages().div_ceil(64 * 64) {
            let summary = summary_word(vcpu(), k);
            if summary.load(SeqCst) == 0 {
                continue;
            }
            let mut words = summary.swap(0, SeqCst);
            while words != 0 {
                let i = k * 64 + words.trailing_zeros() as usize;
                let stale = record_word(vcpu(), Some(i));
                let mut bits = if stale.load(SeqCst) == 0 { 0 } else { stale.swap(0, SeqCst) };
                while bits != 0 && pending & FLUSH == 0 {
                    invalidate(((i * 64 + bits.trailing_zeros() as usize) as u64) << 12)?;
                    bits &= bits - 1;
                }
                words &= words - 1;
            }
        }
    }
    Some(pending)
}
