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
use natemu_abi::code_object::{Header, Site};
use core::mem::align_of;
use core::sync::atomic::{AtomicU64, Ordering::SeqCst};
use natemu_abi::execution::{
    ENTRY_OFFSET, KICK_SGI, LinkPoint, MAX_LINK_POINTS, STATE_VA, STOP_WORD, State, System,
    cache::{self as area, Control, LinkRecord, Record, SLOT, SLOT_BYTES, SLOT_VA, SLOT_WRITE_VA},
};
pub use natemu_abi::execution::cache::{Block, CODE_VA, DEAD, DISPATCH, DISPATCH_MISS, DISPATCH_VA, FLUSH, GLOBAL, HEAD_BYTES, INBOUND, MAX_VCPUS, PAGES, SHADOWS, STEP, SYSTEM};
/// L1's code (the dispatcher's head ends there).
pub const L1_VA: u64 = CODE_VA + area::L1_CODE as u64;

/// This vCPU's number (its state record's; checked below `MAX_VCPUS` at start).
fn vcpu() -> usize {
    unsafe { (&raw const (*(STATE_VA as *const State)).vcpu).read_volatile() as usize }
}
/// This vCPU's bit in the mask nibbles.
fn own() -> u8 {
    1 << vcpu()
}
/// This vCPU's bit in every nibble of a mask word.
fn own_all() -> u64 {
    0x1111_1111_1111_1111 << vcpu()
}
/// Guest-physical pages of guest RAM (the VM's R / 4096; the host set `ram_bytes` at prepare).
fn pages() -> usize {
    (control().ram_bytes / 4096) as usize
}
/// Code placement alignment (a cache line).
const CODE_ALIGN: u64 = 64;

/// A placed block's access sites (they follow its record), sorted by native offset (checked by the host).
pub fn sites(b: &Block) -> &'static [Site] {
    let at = (b as *const Block).wrapping_add(1).cast::<Site>();
    unsafe { core::slice::from_raw_parts(at, b.sites_count as usize) }
}
const _: () = assert!(size_of::<Block>() % align_of::<Site>() == 0);

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
/// The word of the mask at global offset `at` (code or writable pages) holding `page`'s nibble, and the nibble's shift.
fn mask(at: usize, page: usize) -> (&'static AtomicU64, u32) {
    (unsafe { AtomicU64::from_ptr(global::<u64>(at).wrapping_add(page / 16)) }, 4 * (page % 16) as u32)
}
/// The vCPUs whose bits are set in nibble `shift` of mask word `w`.
fn nibble(w: u64, shift: u32) -> u8 {
    (w >> shift) as u8 & 0xf
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
    unsafe { *summary(mask_at).add(page / 16 / 64) |= 1 << (page / 16 % 64) };
}
/// Clears this vCPU's bits in the mask at `mask_at`: only in the words its summary names (only this vCPU sets its bits, so
/// no other word holds one; peers only clear them), and empties the summary. Its cost follows what was marked, not R:
/// zero summary words are only read, and a word whose load shows none of its bits (a peer cleared them) is not written.
fn clear_own(mask_at: usize) {
    let own = own_all();
    for k in 0..pages().div_ceil(16 * 64) {
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
    (c.code_used, c.meta_used, c.blocks, c.links) = (area::HEAD_BYTES as u64, 0, 0, 0);
    // A `checked` of 0 is never current.
    (c.epoch, c.global_epoch) = (c.epoch.max(1), c.global_epoch.max(1));
    c.regions = [0; 16];
    // Block numbers start again: the dispatcher's table is emptied (it keeps its context), the others hold none.
    let now = c.l1_table as usize;
    clear_l1(now);
    for t in (0..area::L1_TABLES).filter(|&t| t != now) {
        (c.l1_contexts[t], c.l1_used[t]) = (area::NO_CONTEXT, 0);
    }
    unsafe { ptr::write_bytes(lookup_area::<u8>(area::L2), 0, area::L2_ENTRIES * 16) };
    unsafe { ptr::write_bytes(lhead(0), 0, area::LINEAR_BUCKETS) };
    // The reverse map's heads: only pages this vCPU marked (its code summary) can hold one.
    for k in 0..pages().div_ceil(16 * 64) {
        let mut bits = unsafe { summary(area::CODE_PAGES).add(k).read() };
        while bits != 0 {
            let first = 16 * (64 * k + bits.trailing_zeros() as usize);
            unsafe { ptr::write_bytes(head(first), 0, 16) };
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
/// miss; b entry` (the dispatcher's miss), then `udf`.
fn l1_words(at: u64, rip: u64, entry: u64) -> [u32; 8] {
    let half = |k: u32| ((rip >> (16 * k)) & 0xffff) as u32;
    let miss = area::DISPATCH_MISS as u64;
    [
        0xd280_0002 | half(0) << 5,
        0xf2a0_0002 | half(1) << 5,
        0xf2c0_0002 | half(2) << 5,
        0xf2e0_0002 | half(3) << 5,
        0xcb00_0042,
        0xb500_0002 | ((miss.wrapping_sub(at + 20) >> 2) as u32 & 0x7_ffff) << 5,
        branch(at + 24, entry),
        0,
    ]
}
/// Entry `i` of L1 table `t`'s owner: the block it enters, number + 1 (0: empty).
fn l1_owner(t: usize, i: usize) -> *mut u32 {
    lookup_area::<u32>(area::L1_OWNERS).wrapping_add(t * area::L1_ENTRIES + i)
}
/// Empties entry `i` of L1 table `t` (its first word branches to the dispatcher's miss); the caller synchronizes the code.
fn empty_l1(t: usize, i: usize) {
    unsafe { l1_owner(t, i).write(0) };
    let at = l1_entry(t, i);
    unsafe { ((area::CODE_WRITE_VA + at) as *mut u32).write_volatile(branch(at, area::DISPATCH_MISS as u64)) };
}
fn clear_l1(t: usize) {
    for i in 0..area::L1_ENTRIES {
        empty_l1(t, i);
    }
    synchronize_l1(t);
}
fn synchronize_l1(t: usize) {
    synchronize_code(area::CODE_WRITE_VA + l1_entry(t, 0), area::CODE_VA + l1_entry(t, 0), area::L1_TABLE_BYTES);
}
/// The dispatcher's `adr x2` (its second word) to L1 table `t`.
const DISPATCH_ADR: u64 = area::DISPATCH as u64 + 4;
fn dispatch_adr(t: usize) -> u32 {
    let imm = l1_entry(t, 0).wrapping_sub(DISPATCH_ADR) as u32;
    0x1000_0002 | (imm & 3) << 29 | (imm >> 2 & 0x7_ffff) << 5
}

/// Fills the code area's head once, before any block runs: the dispatcher's code. False if its `adr` is not the form
/// [`select_l1`] rewrites (to L1 table 0), or if any table's `adr` does not decode to that table.
pub fn fill_head(dispatcher: &[u8]) -> bool {
    unsafe { copy_record((area::CODE_WRITE_VA + area::DISPATCH as u64) as *mut u8, dispatcher.as_ptr(), dispatcher.len()) };
    synchronize_code(area::CODE_WRITE_VA + area::DISPATCH as u64, area::DISPATCH_VA, dispatcher.len());
    // ADR: immlo bits 30:29, immhi bits 23:5, a signed 21-bit offset from the instruction.
    let target = |w: u32| DISPATCH_ADR.wrapping_add(((((w >> 29 & 3) | (w >> 5 & 0x7_ffff) << 2) << 11) as i32 >> 11) as u64);
    control().l1_table == 0
        && word(DISPATCH_ADR) == dispatch_adr(0)
        && (0..area::L1_TABLES).all(|t| dispatch_adr(t) & 0x9f00_001f == 0x1000_0002 && target(dispatch_adr(t)) == l1_entry(t, 0))
}

/// L1 is keyed by RIP alone, so each table holds entries of one context, and the dispatcher probes the table of the
/// current one: a change of context switches tables (the dispatcher's `adr` rewritten), so no block is entered from L1
/// under another context than its own. A context with no table takes the least recently left one, cleared first. Every
/// table's entries are kept as exact as the current one's: a TLB flush, an invalidation or a reset reaches them all.
pub fn select_l1(context: u64) {
    let c = control();
    let now = c.l1_table as usize;
    if c.l1_contexts[now] == context {
        return;
    }
    let p = crate::perf::enter(crate::perf::L1);
    c.l1_switches += 1;
    c.l1_used[now] = c.l1_switches;
    let t = match (0..area::L1_TABLES).find(|&t| c.l1_contexts[t] == context) {
        Some(t) => t,
        None => {
            let t = (0..area::L1_TABLES).min_by_key(|&t| c.l1_used[t]).unwrap_or(0);
            clear_l1(t);
            c.l1_contexts[t] = context;
            c.l1_clears += 1;
            t
        }
    };
    c.l1_table = t as u64;
    rewrite(DISPATCH_ADR, dispatch_adr(t));
    crate::perf::leave(p);
}
/// Enters `block` for `rip` in the dispatcher's L1 table if the block's context is the table's ([`select_l1`] ran for
/// it before the lookup that found it); otherwise nothing is entered (the block is still entered from the resident).
pub fn fill_l1(rip: u64, block: &Block) {
    let t = control().l1_table as usize;
    if control().l1_contexts[t] != block.context {
        return;
    }
    unsafe { l1_owner(t, (rip & area::L1_MASK) as usize).write(block.number + 1) };
    let at = l1_entry(t, (rip & area::L1_MASK) as usize);
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
/// The block cached for (`rip`, `context`) whose source pages are where `rip` is fetched from now: a current one, or one
/// `mapped` finds so (it walks the fetch of each source page's linear page: `Some(global)` when each maps to the block's
/// page; the walk may write an A bit, so the block is looked at again). Blocks of other address spaces at the same RIP
/// stay for when their mapping returns.
pub fn lookup(rip: u64, context: u64, mut mapped: impl FnMut(&Block) -> Option<bool>) -> Option<&'static Block> {
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
            let global = mapped(&b);
            let c = control();
            if let Some(global) = global.filter(|_| block(n).flags & DEAD == 0) {
                c.revalidated += 1;
                unsafe { (*block_ptr(n)).checked = c.epoch };
                unsafe { (*block_ptr(n)).flags = (*block_ptr(n)).flags & !GLOBAL | if global { GLOBAL } else { 0 } };
                return Some(block(n));
            }
            c.remapped += 1;
        }
        i = (i + 1) & (area::L2_ENTRIES - 1);
    }
}
/// K1: a TLB flush of this vCPU (`global`: global entries too): the epoch moves, and every L1 entry (of every table) and link into a block
/// no longer current goes (the blocks stay, found again by [`lookup`] after a fetch walk). Returns the links undone;
/// `None` if a record does not describe its word (as [`unlink_all`]).
pub fn tlb(global: bool) -> Option<u64> {
    let c = control();
    c.epoch += 1;
    if global {
        c.global_epoch = c.epoch;
        c.global_flushes += 1;
        // No block is current: the dispatcher's table is emptied, the others hold no context any more.
        let now = c.l1_table as usize;
        clear_l1(now);
        for t in (0..area::L1_TABLES).filter(|&t| t != now) {
            (c.l1_contexts[t], c.l1_used[t]) = (area::NO_CONTEXT, 0);
        }
    } else {
        c.cr3_loads += 1;
        for t in (0..area::L1_TABLES).filter(|&t| c.l1_contexts[t] != area::NO_CONTEXT) {
            let mut emptied = false;
            for i in 0..area::L1_ENTRIES {
                let owner = unsafe { l1_owner(t, i).read() };
                if owner != 0 && !current(block(owner as usize - 1)) {
                    empty_l1(t, i);
                    emptied = true;
                }
            }
            if emptied {
                synchronize_l1(t);
            }
        }
    }
    prune()
}

/// Places the admitted object at `object` (header `h`) for (`h.guest_rip`, `context`) with its guest-physical
/// source pages and link points, relocates its computed exit's `b` (at `dispatch_exit`, 0 for none;
/// the reply was checked to hold the placeholders there), and enters it in L2. `None` when the code area, the metadata arena or the
/// block index is full (the caller flushes and places again).
pub fn place(
    h: &Header,
    object: *const u8,
    context: u64,
    source_gpa: [u64; 2],
    links: [LinkPoint; MAX_LINK_POINTS],
    dispatch_exit: u64,
    rep_exits: u64,
) -> Option<&'static Block> {
    let c = control();
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
        flags: 0,
        dispatch_exit: dispatch_exit as u32,
        rep_exits: rep_exits as u32,
        links,
    };
    unsafe {
        // Copied above the fill point first: nothing refers to it until the record is filed.
        copy_record((area::CODE_WRITE_VA + code) as *mut u8, object.add(h.code_offset as usize), h.code_size as usize);
        relocate(code, dispatch_exit, area::DISPATCH);
        synchronize_code(area::CODE_WRITE_VA + code, area::CODE_VA + code, h.code_size as usize);
        let placed = file(record, object.add(h.sites_offset as usize), size_of::<Site>() * h.sites_count as usize)?;
        c.code_used = code_end;
        Some(placed)
    }
}
/// K2: enters the host's decode `i` of the system instruction at `rip` (`guest_bytes` long, from `source_gpa`) in the
/// cache for `context` as a [`SYSTEM`] record (no code); `None` when the metadata arena or the block index is full.
pub fn place_system(rip: u64, context: u64, source_gpa: [u64; 2], guest_bytes: u32, i: &System) -> Option<&'static Block> {
    // `code` at the fill point keeps the records in address order ([`block_at`]); a size of 0 holds no PC.
    let code = control().code_used as u32;
    let links = [LinkPoint::NONE; MAX_LINK_POINTS];
    let record = Block { rip, context, checked: control().epoch, source_gpa, next: [0; 2], lnext: [0; 2], code, code_size: 0, sites_count: 0, guest_bytes, number: 0, flags: SYSTEM, dispatch_exit: 0, rep_exits: 0, links };
    unsafe { file(record, (i as *const System).cast(), size_of::<System>()) }
}
/// The decode a [`SYSTEM`] record holds.
pub fn system_of(b: &Block) -> System {
    unsafe { (b as *const Block).add(1).cast::<System>().read() }
}
const _: () = assert!(size_of::<Block>() % align_of::<System>() == 0);
/// Files `record` (its number assigned here) with `tail_bytes` from `tail` after it (sites, or a decode) in the metadata
/// arena, the block index and L2; `None` when either is full.
unsafe fn file(record: Block, tail: *const u8, tail_bytes: usize) -> Option<&'static Block> {
    let c = control();
    let meta = c.meta_used.next_multiple_of(align_of::<Block>() as u64);
    let meta_end = meta + (size_of::<Block>() + tail_bytes) as u64;
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
                *summary(area::CODE_PAGES).add(page / 16 / 64) |= 1 << (page / 16 % 64);
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
        for r in [record.rip >> 21, record.rip.wrapping_add(u64::from(record.guest_bytes).max(1) - 1) >> 21] {
            let (w, bit) = region(r);
            c.regions[w] |= bit;
        }
        copy_record(at.add(1).cast(), tail, tail_bytes);
        index(n).write(meta as u32);
        let mut i = hash(record.rip, record.context);
        while l2(i).read()[1] != 0 {
            i = (i + 1) & (area::L2_ENTRIES - 1);
        }
        l2(i).write([record.rip, n as u64 + 1]);
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
        relocate(SLOT as u64, dispatch_exit, DISPATCH_MISS);
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
/// its computed exit (at `dispatch_exit`, 0 for none) becomes `b` to the dispatcher. (The placeholder was checked at
/// the reply.)
fn relocate(code: u64, dispatch_exit: u64, to: usize) {
    if dispatch_exit != 0 {
        let at = code + dispatch_exit;
        unsafe { ((area::CODE_WRITE_VA + at) as *mut u32).write_volatile(branch(at, to as u64)) };
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
    /// No room for their records: nothing linked.
    Full,
    /// `source` has no link point to the target, or one holds a word the cache did not put there.
    Bad,
}
/// Links every link point of `source` to `target.rip` (in the same context) to `target`: its first word becomes `b`
/// to the target's entry, recorded with the original word. Link points already branching there stay.
pub fn link(source: &Block, target: &Block) -> Link {
    let c = control();
    let points = || source.links.iter().filter(|p| p.offset != 0 && p.target == target.rip);
    if source.context != target.context || points().next().is_none() {
        return Link::Bad;
    }
    let entry = u64::from(target.code) + u64::from(ENTRY_OFFSET);
    let mut todo = 0;
    for p in points() {
        let at = u64::from(source.code) + u64::from(p.offset);
        if unlinked(source, p) {
            todo += 1;
        } else if word(at) != branch(at, entry) {
            return Link::Bad;
        }
    }
    if c.links as usize + todo > area::MAX_LINKS {
        return Link::Full;
    }
    for p in points().filter(|p| unlinked(source, p)) {
        let at = u64::from(source.code) + u64::from(p.offset);
        let original = word(at);
        rewrite(at, branch(at, entry));
        unsafe { record(c.links as usize).write(LinkRecord { source: source.number, offset: p.offset, original, target: target.number }) };
        unsafe { (*block_ptr(target.number as usize)).flags |= INBOUND };
        c.links += 1;
    }
    Link::Linked(todo as u64)
}
/// Restores every linked point's original word and drops every record (the invalidation of all blocks at a flush).
/// `None` if a record does not describe its word (it is not left as is).
pub fn unlink_all() -> Option<u64> {
    let c = control();
    for i in 0..c.links as usize {
        unlink(unsafe { record(i).read() })?;
    }
    let n = c.links;
    c.links = 0;
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
        post(nibble(m.fetch_and(!(0xf << shift), SeqCst), shift) & !own(), gpa);
    }
    invalidate(gpa)
}
/// This vCPU wrote `gpa`'s page through the RAM alias (a split store's part, a walker's A/D update): a peer that
/// marked the page after [`written`] cleared it, or after a check found it unmarked, may have been given the old
/// bytes. Posts the page to every such peer and clears their bits (this vCPU marks no page while it writes one).
pub fn rechecked(gpa: u64) {
    if let Some((m, shift)) = code_page(gpa) {
        unsafe { asm!("dmb ish") };
        let peers = 0xf & !own();
        if nibble(m.load(SeqCst), shift) & peers != 0 {
            post(nibble(m.fetch_and(!(u64::from(peers) << shift), SeqCst), shift) & peers, gpa);
        }
    }
}
/// Posts `gpa`'s page to the vCPUs whose bits are set in `peers` and kicks them.
fn post(peers: u8, gpa: u64) {
    let page = (gpa >> 12) as usize;
    for v in (0..MAX_VCPUS).filter(|v| peers & 1 << v != 0) {
        record_word(v, Some(page / 64)).fetch_or(1 << (page % 64), SeqCst);
        summary_word(v, page / 64 / 64).fetch_or(1 << (page / 64 % 64), SeqCst);
        record_word(v, None).fetch_or(PAGES, SeqCst);
        kick(v);
    }
}
/// Rings vCPU `v`'s doorbell (SGI [`KICK_SGI`] to Aff0 = `v`, its MPIDR): it stops at its next block boundary and
/// drains its record. The `dsb` makes this vCPU's record updates visible before the interrupt.
pub fn kick(v: usize) {
    let c = control();
    let sgi = KICK_SGI << 24 | 1 << v;
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
/// A guest INVLPG (or a #PF) of the linear range `[start, start + bytes)` (K1; inside one 2 MiB region): every live
/// block of this vCPU with guest bytes in it is no longer current (`checked` 0, global or not): out of L1, every link
/// into it undone; it stays in L2 and is found again only after a fetch walk ([`lookup`]). Its code-page bits stay: they
/// only say it may hold translations. Blocks are looked for only when a 2 MiB region the range touches may hold one
/// ([`Control::regions`]), then on the linear map's chains of the range's pages ([`area::LINEAR_HEADS`]).
pub fn invalidate_linear(start: u64, bytes: u64) -> Option<u64> {
    let c = control();
    let held = |r: u64| {
        let (w, bit) = region(r);
        c.regions[w] & bit != 0
    };
    if !held(start >> 21) && !held(start.wrapping_add(bytes - 1) >> 21) {
        return Some(0);
    }
    let mut inbound = false;
    for page in start >> 12..=start.wrapping_add(bytes - 1) >> 12 {
        on_linear_page(page, |n| inbound |= cut_one(n, false));
    }
    if inbound { prune() } else { Some(0) }
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
/// link records undoes every link into a block no longer current or dead and out of a dead one (links out of a block
/// only not current stay: they run only once it is entered, which needs a fetch walk first). Dead blocks leave L2 (as
/// [`lookup`] skips them). Returns the links undone; `None` if a record does not describe its word.
fn cut(doomed: impl Iterator<Item = usize>, dead: bool) -> Option<u64> {
    let mut prune_needed = false;
    for n in doomed {
        prune_needed |= cut_one(n, dead);
    }
    if prune_needed { prune() } else { Some(0) }
}
/// [`cut`]'s work on block `n` but the link records: whether they need a pass (a link may enter it, or it is dead and
/// links may leave it).
fn cut_one(n: usize, dead: bool) -> bool {
    let c = control();
    let b = *block(n);
    if b.flags & DEAD != 0 || (!dead && b.checked == 0) {
        return false;
    }
    let linked = b.flags & INBOUND != 0 || (dead && b.links.iter().any(|p| p.offset != 0));
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
        if unsafe { l1_owner(t, i).read() } == b.number + 1 {
            empty_l1(t, i);
            let at = l1_entry(t, i);
            synchronize_code(area::CODE_WRITE_VA + at, area::CODE_VA + at, 4);
        }
    }
    linked
}
/// Undoes every link whose target is no longer current or dead, or whose source is dead, and drops its record.
fn prune() -> Option<u64> {
    let c = control();
    let mut unlinked = 0;
    let mut i = 0;
    while i < c.links {
        let r = unsafe { record(i as usize).read() };
        let target = block(r.target as usize);
        if current(target) && target.flags & DEAD == 0 && block(r.source as usize).flags & DEAD == 0 {
            i += 1;
            continue;
        }
        unlink(r)?;
        unlinked += 1;
        c.links -= 1;
        unsafe { record(i as usize).write(record(c.links as usize).read()) };
    }
    Some(unlinked)
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
    let b = block(lo.checked_sub(1)?);
    (offset < u64::from(b.code) + u64::from(b.code_size)).then_some(b)
}

/// Marks `gpa`'s page as holding this vCPU's translations, before its bytes are copied for a block: from here on a
/// write to the page posts it to this vCPU. Returns the vCPUs that may hold a writable shadow of it, read after the
/// mark (a writable install sets its bit before it reads the marks, so it either sees this mark or is returned here).
pub fn mark(gpa: u64) -> u8 {
    if code_page(gpa).is_none() {
        return 0;
    }
    set_own(area::CODE_PAGES, (gpa >> 12) as usize);
    writable(gpa)
}
/// This vCPU's mark of `gpa`'s page is still set (no write cleared it since [`mark`]); pages outside guest RAM count as
/// marked (no code is fetched from them).
pub fn marked(gpa: u64) -> bool {
    code_page(gpa).is_none_or(|(m, shift)| nibble(m.load(SeqCst), shift) & own() != 0)
}
/// The vCPUs that may hold a writable shadow of `gpa`'s page.
pub fn writable(gpa: u64) -> u8 {
    let page = (gpa >> 12) as usize;
    if page >= pages() {
        return 0;
    }
    let (m, shift) = mask(area::WRITABLE_PAGES, page);
    nibble(m.load(SeqCst), shift)
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
    code_page(gpa).is_some_and(|(m, shift)| nibble(m.load(SeqCst), shift) != 0)
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
    clear_own(area::WRITABLE_PAGES);
    resets(vcpu()).fetch_add(1, SeqCst);
}

/// Takes this vCPU's pending invalidations: swaps `pending` to 0, then each summary word and each stale word it names
/// (a word that loads 0 is skipped unwritten: a bit set after that load comes with a later `pending`, as its producer
/// sets the stale bit, then the summary bit, then `pending`), and invalidates each listed
/// page ([`invalidate`], whose `None` this returns) unless the whole cache goes anyway. Returns the pending bits;
/// the caller flushes on [`FLUSH`] and resets its shadows on [`SHADOWS`].
pub fn drain() -> Option<u64> {
    let pending = record_word(vcpu(), None).swap(0, SeqCst);
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
