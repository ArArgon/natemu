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
use core::mem::align_of;
use core::sync::atomic::{AtomicU64, Ordering::SeqCst};
use natemu_abi::code_object::{Header, Reloc, Site};
pub use natemu_abi::execution::cache::{
    ARMED, Block, CODE_VA, DEAD, DISPATCH, DISPATCH_BYTES, DISPATCH_MISS, DISPATCH_VA, FLUSH, GLOBAL,
    INBOUND, INSTANCE, LARGE, LOCAL_INBOUND, MAX_VCPUS, OFF0, OFF1, PAGES, PIC, PIC_LEAD,
    PIC_TEST_AND, PIC_TEST_BODY, PIC_TEST_FORGE, PIC_TEST_FORGE_HALF, PIC_TEST_FORGE_NONCANONICAL,
    PIC_TEST_STR, PIC_TEST_SVC, PIC_TEST_LOOKUP, PIC_TEST_TAIL, PIC_TEST_TAILS, PIC_TEST_TRAMPOLINE, RELOCATABLE, REUSE_CHECK, REUSE_OFF, SHADOWS, STEP, DROPS, DROP_SLOTS,
    STI_FIRST, SYSTEM, TRANSLATION, UNSHAREABLE,
};
use natemu_abi::execution::cache::{MAX_RELOCS, RELOCS_SHIFT};
use natemu_abi::execution::{
    LinkPoint, MAX_LINK_POINTS, STATE_VA, STOP_WORD, State, System,
    cache::{
        self as area, Control, CpuMask, LinkRecord, MASK_PAGES_PER_WORD, Record, SLOT, SLOT_BYTES,
        SLOT_VA, SLOT_WRITE_VA,
    },
};
use natemu_abi::interp::Insn;
use natemu_abi::pic::{self, count};

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
/// Code placement alignment.
const CODE_ALIGN: u64 = 16;
/// The bytes of an admitted object's code that are placed: those before FEX's tail (its `JITCodeTail` data words after
/// `code_region_end`, which no generated code and no resident path reads).
pub fn placed_size(h: &Header) -> u32 {
    h.code_region_end.min(h.code_size) & !3
}
/// `r` packed for a block of `size` placed bytes ([`natemu_abi::execution::pack_reloc`]); a literal in the
/// dropped tail (the tail's RIP) is not placed, so nothing relocates it (`Some(None)`).
pub fn pack_placed(r: &Reloc, size: u32) -> Option<Option<u32>> {
    if r.offset >= size && r.kind == natemu_abi::code_object::RELOC_GUEST_RIP_LITERAL {
        return Some(None);
    }
    natemu_abi::execution::pack_reloc(r, size)
}
/// Hot/cold layout: a block's words up to its first exit are copied, their branches out
/// re-encoded, into a dense hot stream where it is entered; its whole code (`Block::code`) stays below in its segment and
/// is what every exit, link, stop, site and resume names ([`split`]).
#[cfg(feature = "pic-count")]
compile_error!("pic-count is not available: the hot/cold layout does not mirror its counting stubs (the study's stubs would have to be placed in the hot stream as well)");
/// Hot/cold: where block's hot copy ends ([`natemu_abi::execution::body_end`]: its body's end, its exit words and check
/// stubs' first words being where it stops; `word` reads its code as admitted, `stub` says whether a word is a check stub's
/// first) and the bytes it takes in the hot zone past its first word (which is the block before's last): the trampolines of
/// the exits after its body, its words from its entry to that end, and an exit's trampoline right after it. (0, 0): not split.
fn split_point(
    word: impl Fn(u64) -> u32,
    links: &[LinkPoint; MAX_LINK_POINTS],
    dispatch_exit: u32,
    size: u32,
    stub: impl Fn(u64) -> bool,
) -> (u32, u64) {
    let (e, n) = natemu_abi::execution::exit_offsets(links, dispatch_exit);
    if n == 0 {
        return (0, 0);
    }
    let exit = |o: u64| e[..n].contains(&(o as u32));
    // M2 task 4: the run's end a block's top test for an open x87 run branches to ([`run_tested`]) stands behind the body's
    // exits and is no site: cold, as the check stubs (it runs when x87 code jumped to SSE code, and then goes on in the
    // block's code below).
    let run_end = natemu_abi::execution::run_test_end(&word, u64::from(size));
    let stub = |o: u64| stub(o) || run_end == Some(o);
    let Some(h) = natemu_abi::execution::body_end(word, u64::from(size), |o| exit(o) || stub(o)) else {
        return (0, 0);
    };
    if h >= u64::from(size) {
        return (0, 0);
    }
    let after = e[..n].iter().filter(|&&x| u64::from(x) > h).count() as u64;
    (h as u32, 4 * after + h)
}
/// Where a block of `size` bytes goes and where its hot copy ([`natemu_abi::execution::cache::SegFill::reserve`]); `None`: the
/// area is full.
fn reserve(size: u64, hot: u64) -> Option<(u64, u64)> {
    let c = control();
    let r = c.seg.reserve(&mut c.code_used, c.code_bytes, size, hot);
    if r.is_none() {
        c.full[0] += 1;
    }
    r
}
/// Hot/cold: makes the hot copy of the block `b` (its record as it will be filed: `code`, `hot`, `hot_size`, links and
/// computed exit; its code placed and relocated), each branch going where its target is now (a word of its body or its first
/// exit: the hot copy's; another exit: that exit's trampoline; anything else: its code), and synchronizes it. False (nothing
/// entered: the words written are above the hot fill point) when a word has no moved form.
fn split(b: &mut Block) -> bool {
    b.hot_body = b.hot_body_for();
    let (code, h, body) = (u64::from(b.code), u64::from(b.hot_size), b.hot_body());
    // (Word `h` of the body is written only by an exit's trampoline: a branch to a non-exit word there has no moved form.)
    let (lost, exit_at_h) = (core::cell::Cell::new(false), b.trampolines().any(|(e, _)| e == code + h));
    let map = |t: u64| {
        if t >= code + 4 && t <= code + h {
            lost.set(lost.get() || (t == code + h && !exit_at_h));
            return body + (t - code);
        }
        b.trampolines().find(|&(e, _)| e == t).map_or(t, |(_, at)| at)
    };
    // (The word before `h` falling through into a word that is no exit would run what the hot zone held before: a stub's first
    // word reached by falling through is a malformed block, so it is placed whole and its code stays as admitted.)
    // (An empty body — no word copied, no exit at its end — has nothing to enter: placed whole as well.)
    if !exit_at_h && (h == u64::from(natemu_abi::execution::ENTRY_OFFSET) || natemu_abi::execution::falls_through(word(code + h - 4))) {
        return false;
    }
    for o in (u64::from(natemu_abi::execution::ENTRY_OFFSET)..h).step_by(4) {
        let Some(w) = natemu_abi::execution::moved_word(word(code + o), code + o, body + o, map) else {
            return false;
        };
        unsafe { ((area::CODE_WRITE_VA + body + o) as *mut u32).write_volatile(w) };
    }
    if lost.get() {
        return false;
    }
    let mut patch = [0; MAX_LINK_POINTS];
    for (e, at) in b.trampolines() {
        unsafe { ((area::CODE_WRITE_VA + at) as *mut u32).write_volatile(natemu_abi::execution::trampoline_word(word(e), e, at)) };
        if let Some(k) = b.links.iter().position(|p| p.offset != 0 && code + u64::from(p.offset) == e) {
            patch[k] = at as u32;
        }
    }
    b.patch = patch;
    let from = u64::from(b.hot) + 4;
    synchronize_code(area::CODE_WRITE_VA + from, area::CODE_VA + from, (body + h + 4 - from) as usize);
    // M2 task 4: the run's end of a top x87 run test is cold; its return goes to the hot copy's third word, not the cold
    // code's (whose exits no link patches: each arrival with a run open, x87 code jumping or returning to SSE code,
    // would leave through the linker). The one word of a block's code the resident changes besides its link points
    // ([`run_return`]: a copy restores it, the reuse check knows it).
    if let Some(e) = natemu_abi::execution::run_test_end(|o| word(code + o), u64::from(b.code_size)) {
        // (Only the word fexc emits there: any other stays, and the block is then no source of a copy, [`copy`].)
        if word(code + e + 36) == natemu_abi::execution::run_return_word(e) {
            rewrite(code + e + 36, branch(code + e + 36, body + u64::from(natemu_abi::execution::ENTRY_OFFSET) + 8));
        }
    }
    true
}
/// M2 task 4 and reuse: for a block that begins with the test for an open x87 run, the offset of its run end's return
/// word, the word as admitted (`b` to the block's third word) and, if the block is split, the word [`split`] put there
/// (`b` to its hot copy's third word).
pub fn run_return(b: &Block) -> Option<(u64, u32, Option<u32>)> {
    let code = u64::from(b.code);
    let e = natemu_abi::execution::run_test_end(|o| word(code + o), u64::from(b.code_size))?;
    let at = code + e + 36;
    let hot = (b.hot_size != 0).then(|| branch(at, b.hot_body() + u64::from(natemu_abi::execution::ENTRY_OFFSET) + 8));
    Some((e + 36, natemu_abi::execution::run_return_word(e), hot))
}
fn link_at(b: &Block, offset: u64) -> u64 {
    b.link_at(offset as u32)
}
/// The word a link from code-area offset `at` to `to` writes: `b`, or `nop` when `to` is the next word (a split block's
/// trampoline with its target's hot copy right after it).
fn link_word(at: u64, to: u64) -> u32 {
    if to == at + 4 { NOP } else { branch(at, to) }
}
const NOP: u32 = 0xd503_201f;
fn admitted_at(b: &Block, p: &LinkPoint) -> u32 {
    b.admitted_at(p)
}
/// The native offset in `b` (its code's) of `pc`, a PC [`block_at`] found in it: in its hot copy (a trampoline stands for
/// its exit word) or its code.
pub fn native_offset(b: &Block, pc: u64) -> u64 {
    let x = pc.wrapping_sub(area::CODE_VA);
    if !in_hot(b, x) {
        return pc.wrapping_sub(b.code_va());
    }
    match b.trampolines().find(|&(_, t)| t == x) {
        Some((e, _)) => e - u64::from(b.code),
        None => x - b.hot_body(),
    }
}
/// Code-area offset `x` is in `b`'s hot copy (its trampolines and body words).
fn in_hot(b: &Block, x: u64) -> bool {
    b.hot_size != 0 && u64::from(b.hot) + 4 <= x && x < b.hot_body() + u64::from(b.hot_size) + 4
}

/// A placed block's access sites (they follow its record), sorted by native offset (checked by the host).
pub fn sites(b: &Block) -> &'static [Site] {
    let at = (b as *const Block).wrapping_add(1).cast::<Site>();
    unsafe { core::slice::from_raw_parts(at, b.sites_count as usize) }
}
const _: () = assert!(size_of::<Block>() % align_of::<Site>() == 0);
/// A [`RELOCATABLE`] block's packed guest-RIP relocations (perf chunk 10; they follow its sites).
pub fn relocs(b: &Block) -> &'static [u32] {
    let at = sites(b).as_ptr_range().end.cast::<u32>();
    let n = if b.flags & RELOCATABLE != 0 {
        b.flags >> RELOCS_SHIFT & MAX_RELOCS
    } else {
        0
    };
    unsafe { core::slice::from_raw_parts(at, n as usize) }
}
const _: () = assert!(size_of::<Site>() % 4 == 0);

pub(crate) fn lookup_area<T>(offset: usize) -> *mut T {
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
/// Physical-page dependency, published before a guest page-table load. The host sees all prior
/// marks after the resident's I/O acknowledgement; no cache/shadow reset clears them, only a
/// host window that writes the page (every resident then flushes all translations).
/// Already-marked pages cost one load (see [`area::mark_table_page`] for the ordering argument).
pub fn table_read(gpa: u64) {
    let page = (gpa >> 12) as usize;
    if page < natemu_abi::execution::MAX_GUEST_PAGES {
        // SAFETY: TABLE_PAGES covers MAX_GUEST_PAGES bits in the live global area; the word is aligned.
        let word = unsafe { AtomicU64::from_ptr(global::<u64>(area::TABLE_PAGES).add(page / 64)) };
        area::mark_table_page(word, 1 << (page % 64));
    }
}
/// The word of the mask at global offset `at` (code or writable pages) holding `page`'s peer bits, and their shift.
fn mask(at: usize, page: usize) -> (&'static AtomicU64, u32) {
    let (word, shift) = area::mask_location(page);
    (
        unsafe { AtomicU64::from_ptr(global::<u64>(at).wrapping_add(word)) },
        shift,
    )
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
    lookup_area(if mask == area::CODE_PAGES {
        area::CODE_SUMMARY
    } else {
        area::WRITABLE_SUMMARY
    })
}
/// Sets this vCPU's bit of `page` in the mask at `mask`, noting its word in the summary.
fn set_own(mask_at: usize, page: usize) {
    let (m, shift) = mask(mask_at, page);
    m.fetch_or(u64::from(own()) << shift, SeqCst);
    unsafe {
        *summary(mask_at).add(page / MASK_PAGES_PER_WORD / 64) |=
            1 << (page / MASK_PAGES_PER_WORD % 64)
    };
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
            let w = unsafe {
                AtomicU64::from_ptr(
                    global::<u64>(mask_at).wrapping_add(k * 64 + bits.trailing_zeros() as usize),
                )
            };
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
    c.seg.reset(area::HEAD_BYTES as u64);
    for (p, used) in c.peak.iter_mut().zip([c.code_used, c.meta_used, c.blocks]) {
        *p = (*p).max(used);
    }
    (
        c.code_used,
        c.meta_used,
        c.blocks,
        c.links,
        c.links_susp,
        c.links_cr3,
        c.links_tlb,
        c.links_local,
    ) = (area::HEAD_BYTES as u64, 0, 0, 0, 0, 0, 0, 0);
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
    (page.wrapping_mul(0x9e37_79b9_7f4a_7c15) >> (64 - area::LINEAR_BUCKETS.trailing_zeros()))
        as usize
}
fn lhead(bucket: usize) -> *mut u32 {
    lookup_area::<u32>(area::LINEAR_HEADS).wrapping_add(bucket)
}
/// The linear pages of a block's first and last guest byte (all of its bytes: a block's source is at most two pages).
fn lpages(b: &Block) -> [u64; 2] {
    [
        b.rip >> 12,
        b.rip.wrapping_add(u64::from(b.guest_bytes).max(1) - 1) >> 12,
    ]
}
/// Calls `f` with every current block ([`current`]) with a guest byte on linear page `page`, from the page's bucket chain.
/// Every block met that is dead or not current, `f`'s cuts included, leaves the chain, marked [`OFF0`] or [`OFF1`] for it
/// ([`relist`] puts a live one back when a revalidation makes it current), so a chain holds the current blocks and those
/// that stopped being current since its last walk. Exact: a block not current is entered only after a fetch walk, which
/// revalidates it (and relists it) or places another, so nothing an invalidation must reach is off its chains (invariant I:
/// every live current block is on each of its chains). `f` must not kill blocks.
fn on_linear_page(page: u64, mut f: impl FnMut(usize)) {
    let c = control();
    let h = lbucket(page);
    let mut prev: Option<(usize, usize)> = None;
    let mut cur = unsafe { lhead(h).read() };
    #[cfg(feature = "link-check")]
    let mut steps = 0;
    while cur != 0 {
        // (`link-check`: a chain holds each block at most once, so a longer walk is a cycle: halt rather than hang.)
        #[cfg(feature = "link-check")]
        {
            steps += 1;
            if steps > c.blocks {
                crate::execution::halt_native([u64::from(line!()), h as u64, u64::from(cur)], natemu_abi::execution::stop::LINK_CHECK)
            }
        }
        let n = cur as usize - 1;
        let b = *block(n);
        let pages = lpages(&b);
        let k = usize::from(lbucket(pages[0]) != h);
        let next = b.lnext[k];
        c.scanned += 1;
        #[cfg(feature = "perf")]
        {
            c.scanned_instances += u64::from(b.flags & INSTANCE != 0);
            c.scanned_stale += u64::from(b.flags & DEAD == 0 && !current(&b));
        }
        if b.flags & DEAD == 0 && current(&b) && pages.contains(&page) {
            f(n);
        }
        // (Read again: `f` may have cut it.)
        let now = block(n);
        if now.flags & DEAD == 0 && current(now) {
            prev = Some((n, k));
        } else {
            match prev {
                None => unsafe { lhead(h).write(next) },
                Some((m, j)) => unsafe { (*block_ptr(m)).lnext[j] = next },
            }
            unsafe { (*block_ptr(n)).flags |= [OFF0, OFF1][k] };
            #[cfg(feature = "perf")]
            {
                c.chain_dropped += 1;
            }
        }
        cur = next;
    }
}
/// Puts block `n`, just made current by a revalidation, back on each linear chain a walk dropped it from ([`OFF0`],
/// [`OFF1`]; [`on_linear_page`]), at the bucket's head. The bit guards the push: a block pushed onto a chain it is on would
/// make a cycle.
fn relist(n: usize) {
    let b = unsafe { &mut *block_ptr(n) };
    let pages = lpages(b);
    for (k, off) in [OFF0, OFF1].into_iter().enumerate() {
        if b.flags & off != 0 {
            let h = lbucket(pages[k]);
            b.lnext[k] = unsafe { lhead(h).read() };
            unsafe { lhead(h).write(n as u32 + 1) };
            b.flags &= !off;
            #[cfg(feature = "perf")]
            {
                control().relisted += 1;
            }
        }
    }
}
/// `link-check` builds, after every INVLPG or #PF cut and at the start and end of every TLB flush: the linear chains hold
/// exactly the (block, chain) pairs whose [`OFF0`]/[`OFF1`] bit is clear (dead blocks included, translations never), each
/// chain ending within [`Control::blocks`] steps; no live current block has a bit set (invariant I); and a live block off a
/// chain has no L1 entry and no active link from another page into it (invariant L: none outside the suspended segment).
/// Halts `stop::LINK_CHECK` otherwise (the line, then what failed).
#[cfg(feature = "link-check")]
#[track_caller]
fn check_chains() {
    let line = u64::from(core::panic::Location::caller().line());
    let halt = |a: u64, b: u64| -> ! { crate::execution::halt_native([line, a, b], natemu_abi::execution::stop::LINK_CHECK) };
    let c = control();
    // (A finite chain holds each pair once: its successor follows from the pair. So pairs met, all with a clear bit, and as many as the
    // index's pairs with a clear bit, are those pairs.)
    let mut met = 0;
    for h in 0..area::LINEAR_BUCKETS {
        let (mut cur, mut steps) = (unsafe { lhead(h).read() }, 0);
        while cur != 0 {
            let n = cur as usize - 1;
            steps += 1;
            if steps > c.blocks || n as u64 >= c.blocks {
                halt(h as u64, u64::from(cur))
            }
            let b = block(n);
            let pages = lpages(b);
            let k = usize::from(lbucket(pages[0]) != h);
            if lbucket(pages[k]) != h || b.flags & ([OFF0, OFF1][k] | TRANSLATION) != 0 {
                halt(h as u64, n as u64)
            }
            met += 1;
            cur = b.lnext[k];
        }
    }
    let mut pairs = 0;
    for n in 0..c.blocks as usize {
        let b = block(n);
        if b.flags & TRANSLATION != 0 {
            continue;
        }
        let [b0, b1] = lpages(b).map(lbucket);
        if b0 == b1 && b.flags & OFF1 != 0 {
            halt(1 << 32 | n as u64, u64::from(b.flags))
        }
        pairs += u64::from(b.flags & OFF0 == 0) + u64::from(b0 != b1 && b.flags & OFF1 == 0);
        if b.flags & DEAD != 0 || b.flags & (OFF0 | OFF1) == 0 {
            continue;
        }
        if current(b) {
            halt(2 << 32 | n as u64, u64::from(b.flags))
        }
        // (Only tables with a context: a global flush or a reset leaves the others' entries, unreachable, until a context takes one
        // and `select_l1` clears it.)
        let (w0, w1) = area::l1_slots_of(b.rip);
        if (0..area::L1_TABLES)
            .filter(|&t| c.l1_contexts[t] != area::NO_CONTEXT)
            .any(|t| [w0, w1].iter().any(|&i| l1_block(t, i) == b.number + 1))
        {
            halt(3 << 32 | n as u64, 0)
        }
        let mut at = b.inbound;
        for _ in 0..=area::MAX_LINKS {
            if at == 0 {
                break;
            }
            let i = u64::from(at) - 1;
            if i >= c.links_susp && i < c.links_tlb {
                halt(4 << 32 | n as u64, i)
            }
            at = unsafe { record(i as usize).read() }.next_in;
        }
        if at != 0 {
            halt(5 << 32 | n as u64, u64::from(at))
        }
    }
    if met != pairs {
        halt(6 << 32 | met, pairs)
    }
}
/// The reverse map's head of guest-physical page `page` ([`area::PAGE_BLOCKS`]).
fn head(page: usize) -> *mut u32 {
    lookup_area::<u32>(area::PAGE_BLOCKS).wrapping_add(page)
}
/// Slot `i` of L1 table `t`'s owner: the block it enters, number + 1 (0: empty), and the flags [`area::L1_FLAGS`]
/// ([`area::L1_LISTED`]; [`area::L1_NEXT`] in way 0's word only): every raw read of it masks or keeps them.
fn l1_owner(t: usize, i: usize) -> *mut u32 {
    lookup_area::<u32>(area::L1_OWNERS).wrapping_add(t * area::L1_ENTRIES + i)
}
/// The block slot `i` of L1 table `t` enters, number + 1 (0: empty).
fn l1_block(t: usize, i: usize) -> u32 {
    unsafe { l1_owner(t, i).read() & !area::L1_FLAGS }
}
/// Slot `k` of L1 table `t`'s list ([`area::L1_LISTS`]).
fn l1_list(t: usize, k: usize) -> *mut u32 {
    lookup_area::<u32>(area::L1_LISTS).wrapping_add(t * area::L1_ENTRIES + k)
}
/// Data tables: the dispatcher reads its tables as data ([`area::DL1`]). An empty
/// entry's target is the dispatcher's miss: [`area::DL1_EMPTY`] passes the computed exit's canonical check (it compares bits
/// 63 and 47 only), so a guest jump to that very address matches the marker and must miss like any other (review, defect 1).
fn dl1(t: usize, i: usize) -> *mut [u64; 2] {
    lookup_area::<[u64; 2]>(area::DL1).wrapping_add(t * area::L1_ENTRIES + i)
}
/// Publish through the resident-only lookup alias. EL0 sees these two tags read-only. The
/// target word never changes; a coincidental tag match and an ordinary miss reach the same gate.
pub fn publish_segment_view(view: [u64; 2]) {
    for (slot, tag) in area::SEGMENT_SLOTS.into_iter().zip(view) {
        let word = dl1(area::SEGMENT_TABLE, slot).cast::<u64>();
        unsafe {
            if word.read() != tag {
                word.write(tag);
            }
        }
    }
}
/// Empties slot `i` of L1 table `t` (it stays on its table's list if it was, and keeps the set's next-victim bit).
fn empty_l1(t: usize, i: usize) {
    if area::segment_slot(t, i) {
        return; // permanently unowned, even when a context clear scans the complete table
    }
    unsafe { l1_owner(t, i).write(l1_owner(t, i).read() & area::L1_FLAGS) };
    unsafe { dl1(t, i).write([area::DL1_EMPTY, area::CODE_VA + area::DISPATCH_MISS as u64]) };
}
/// Empties every entry of L1 table `t` and its list.
fn clear_l1(t: usize) {
    for i in 0..area::L1_ENTRIES {
        empty_l1(t, i);
        unsafe { l1_owner(t, i).write(0) };
    }
    control().l1_listed[t] = 0;
}
/// Code-area offset of dispatcher slot `k` ([`area::DISPATCH`]; slot 0 is the dispatcher itself).
fn slot_at(k: usize) -> u64 {
    (area::DISPATCH + k * area::DISPATCH_SLOT_BYTES) as u64
}
/// Fills the code area's head once, before any block runs: the dispatcher's code (slot 0, whose `and` the bound slots
/// copy and which falls through to the miss) and every other slot unbound (`b` to the miss). False if the dispatcher is
/// not of that form.
pub fn fill_head(dispatcher: &[u8]) -> bool {
    unsafe {
        copy_record(
            (area::CODE_WRITE_VA + area::DISPATCH as u64) as *mut u8,
            dispatcher.as_ptr(),
            dispatcher.len(),
        )
    };
    // The dispatcher's form: `and x1, x0, #L1_SET_MASK`, three `nop`s, the miss's `svc`.
    let form = word(slot_at(0)) == 0x9240_0001 | (area::L1_SET_BITS - 1) << 10
        && (1..4).all(|k| word(slot_at(0) + 4 * k) == 0xd503_201f)
        && dispatcher.len() == 20;
    let c = control();
    for k in 1..area::DISPATCH_SLOTS {
        let words = slot_words(k, None);
        unsafe {
            copy_record(
                (area::CODE_WRITE_VA + slot_at(k)) as *mut u8,
                words.as_ptr().cast(),
                area::DISPATCH_SLOT_BYTES,
            )
        };
        c.slot_contexts[k] = area::NO_CONTEXT;
    }
    // (Slot 0's own lookup and return forms: the miss's, as an unbound slot's; its first words are the dispatcher's.)
    let words = slot_words(0, None);
    unsafe {
        copy_record(
            (area::CODE_WRITE_VA + slot_at(0) + area::SLOT_LOOKUP as u64) as *mut u8,
            words[area::SLOT_LOOKUP / 4..].as_ptr().cast(),
            area::DISPATCH_SLOT_BYTES - area::SLOT_LOOKUP,
        )
    };
    (c.slot_contexts[0], c.l1_slots) = (area::NO_CONTEXT, [0; area::L1_TABLES]);
    for t in 0..area::L1_TABLES {
        for i in 0..area::L1_ENTRIES {
            let tag = if area::segment_slot(t, i) { 0 } else { area::DL1_EMPTY };
            unsafe { dl1(t, i).write([tag, area::CODE_VA + area::DISPATCH_MISS as u64]) };
        }
    }
    synchronize_code(
        area::CODE_WRITE_VA + area::DISPATCH as u64,
        area::DISPATCH_VA,
        area::DISPATCH_BYTES,
    );
    c.l1_table == 0 && form
}
use area::{BLR_X1, BR_X1};
/// Slot `k`'s words (its [`area::DISPATCH_SLOT_BYTES`]), naming L1 table `t` or missing (`None`): the three forms
/// ([`area::SLOT_ORDINARY`], [`area::SLOT_LOOKUP`], [`area::SLOT_RETURN`]) of one probe ([`area::slot_form_words`]).
fn slot_words(k: usize, t: Option<usize>) -> [u32; area::DISPATCH_SLOT_BYTES / 4] {
    let at = slot_at(k);
    let probe = t.map(|t| {
        // The tables of `t`'s class lie a whole number of pages from x28, after its 2 MiB or before it
        // ([`area::dl1_after`]): `add x2, x28, #pages, lsl #12`, or `sub`.
        let va = control().guest_state_va;
        let page = (va >> 12 & 511) as u32;
        // (One data table's pages.)
        const TABLE_PAGES: u32 = (area::L1_ENTRIES * 16 / 4096) as u32;
        let reach = |after: u32, before: u32| {
            (if area::dl1_after(va) { 0x9140_0000 | (512 - page + after) << 10 } else { 0xd140_0000 | (page + before) << 10 }) | 28 << 5 | 2
        };
        let miss = |k: usize| 0xb500_0001 | ((area::DISPATCH_MISS as u64).wrapping_sub(at + 4 * k as u64) as u32 >> 2 & 0x7_ffff) << 5;
        // Data tables: `and x1, x0, #mask; add x2, x28, #table; add x2, x2, x1, lsl #5; ldr x1, [x2]; eor x1, x1,
        // x0; cbnz x1, way 1; ldr x1, [x2, #8]; br x1; way 1: ldr x1, [x2, #16]; eor x1, x1, x0; cbnz x1, miss; ldr x1,
        // [x2, #24]; br x1`.
        [
            word(slot_at(0)),
            {
                let k = (t % area::L1_SPACE_TABLES) as u32;
                reach(TABLE_PAGES * k, 512 - TABLE_PAGES * k)
            },
            0x8b01_1442,
            0xf940_0041,
            0xca00_0021,
            0xb500_0061,
            0xf940_0441,
            BR_X1,
            0xf940_0841,
            0xca00_0021,
            miss(10),
            0xf940_0c41,
            BR_X1,
        ]
    });
    area::slot_form_words(at, probe)
}
/// Perf chunk 12: slot `k` names L1 table `t` (the dispatcher's words with its own `adrp`), or misses (`None`).
fn write_slot(k: usize, t: Option<usize>) {
    let at = slot_at(k);
    let words = slot_words(k, t);
    unsafe {
        copy_record(
            (area::CODE_WRITE_VA + at) as *mut u8,
            words.as_ptr().cast(),
            size_of_val(&words),
        )
    };
    synchronize_code(
        area::CODE_WRITE_VA + at,
        area::CODE_VA + at,
        size_of_val(&words),
    );
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
/// exact as the current one's: a TLB flush, an invalidation or a reset reaches them all. A context takes only a table of
/// its privilege class ([`area::l1_user_table`]): each shadow space maps only its class's tables, so a slot's table is
/// always mapped where its blocks run and user code can read no supervisor context's entries.
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
            let user = context & 3 == 3;
            let t = (0..area::L1_TABLES)
                .filter(|&t| area::l1_user_table(t) == user)
                .min_by_key(|&t| c.l1_used[t])
                .unwrap_or(0);
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
    // (PIC study test [`PIC_TEST_SVC`]: none, so every computed exit and checked link enters the resident, which then
    // enters blocks armed after their links are made.)
    if control().l1_contexts[t] != block.context || control().pic_test == PIC_TEST_SVC {
        #[cfg(feature = "dispatch-study")]
        {
            control().l1_counts[area::L1_SKIPPED] += 1;
        }
        return;
    }
    let (w0, w1) = area::l1_slots_of(rip);
    let (o0, o1) = unsafe { (l1_owner(t, w0).read(), l1_owner(t, w1).read()) };
    let want = block.number + 1;
    // The way: one holding the block already (so it is never in both), else an empty one, else the victim by the set's
    // fill order. Which one is a hint for performance; the written slot's owner precedes its words, as ever.
    let f = area::l1_fill_set(t, w1, o0, o1, want);
    let i = [w0, w1][f.way];
    #[cfg(feature = "dispatch-study")]
    if f.victim {
        control().l1_counts[area::L1_EVICTED] += 1;
    }
    // A conflict the 16-bit direct-mapped index did not have: the victim's RIP differs from this one in bit 15 alone, read
    // from its table entry, not through its block.
    #[cfg(feature = "perf")]
    if f.victim && (unsafe { dl1(t, i).read()[0] } >> 15 & 1 != 0) != (rip >> 15 & 1 != 0) {
        control().l1_counts[area::L1_NEW_CONFLICT] += 1;
    }
    let flip = if f.victim { area::L1_NEXT } else { 0 };
    let mut listed = [o0, o1][f.way] & area::L1_LISTED;
    if listed == 0 && block.flags & GLOBAL == 0 {
        let c = control();
        unsafe { l1_list(t, c.l1_listed[t] as usize).write(i as u32) };
        (c.l1_listed[t], listed) = (c.l1_listed[t] + 1, area::L1_LISTED);
    }
    // Way 0's word carries the set's next-victim bit (flipped by a replacement); way 1's replacement leaves way 0's code.
    if f.way == 0 {
        unsafe { l1_owner(t, w0).write(listed | (o0 & area::L1_NEXT ^ flip) | want) };
    } else {
        unsafe { l1_owner(t, w1).write(listed | want) };
        if f.victim {
            unsafe { l1_owner(t, w0).write(o0 ^ area::L1_NEXT) };
        }
    }
    unsafe { dl1(t, i).write([rip, code_of(block).full_entry()]) };
}

/// Dispatch study (`dispatch-study` builds only: its loads and stores cost measurable time in the `perf` gate builds):
/// counts a dispatcher miss for `rip` in code `context` by what the current L1 table's entry at its index holds
/// ([`area::MISS_EMPTY`] and the rest).
#[cfg(feature = "dispatch-study")]
pub fn count_miss(rip: u64, context: u64) {
    let c = control();
    let t = c.l1_table as usize;
    let (w0, w1) = area::l1_slots_of(rip);
    let owners = [l1_block(t, w0), l1_block(t, w1)];
    c.l1_counts[if c.l1_contexts[t] != context {
        area::MISS_CONTEXT
    } else if owners.iter().any(|&o| o != 0 && block(o as usize - 1).rip == rip) {
        area::MISS_SAME
    } else if owners.contains(&0) {
        area::MISS_EMPTY
    } else {
        area::MISS_OTHER
    }] += 1;
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
/// Perf chunk 22 (`perf` accounting; the PIC study's miss split): the first live block cached for (`rip`, `context`),
/// current or not, with no walk.
#[cfg(any(feature = "perf", feature = "pic-count"))]
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
        if key == rip
            && b.rip == rip
            && b.context == context
            && b.flags & DEAD == 0
            && (!current_only || current(b))
        {
            return Some(b);
        }
        i = (i + 1) & (area::L2_ENTRIES - 1);
    }
}
/// What a revalidation says of a block ([`lookup`]'s `mapped`): its source pages are where its RIP is fetched from now
/// (`Yes`: the flags, [`GLOBAL`] and [`LARGE`] as the translations found them), they are not (`No`), or the answer is not
/// known without a fetch walk (`Unknown`: the page record of the fast miss path lacks a page; only that path says it).
pub enum Mapped {
    Yes(u32),
    No,
    Unknown,
}
/// The block cached for (`rip`, `context`) whose source pages are where `rip` is fetched from now: a current one, or one
/// `mapped` finds so (it walks the fetch of each source page's linear page: `Yes(flags)` when each maps to the block's
/// page, [`GLOBAL`] and [`LARGE`] as the walk found them; the walk may write an A bit, so the block is looked at again).
/// Blocks of other address spaces at the same RIP stay for when their mapping returns. `Unknown` ends the search with
/// `None` (the caller owes the walk).
pub fn lookup(
    rip: u64,
    context: u64,
    mut mapped: impl FnMut(&Block) -> Mapped,
) -> Option<&'static Block> {
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
            let walked = match mapped(&b) {
                Mapped::Unknown => return None,
                Mapped::Yes(flags) => Some(flags),
                Mapped::No => None,
            };
            let c = control();
            // PIC study stage 3: shared code is for non-global mappings only: an instance whose walk finds a global one
            // goes (its address then gets an absolute block).
            if b.flags & INSTANCE != 0
                && walked.is_some_and(|w| w & GLOBAL != 0)
                && block(n).flags & DEAD == 0
            {
                cut_one(n, true);
                c.remapped += 1;
                continue; // (its L2 entry is removed: entry i holds the next one now)
            }
            if let Some(walked) = walked.filter(|_| block(n).flags & DEAD == 0) {
                c.revalidated += 1;
                crate::study::revalidated(b.checked, walked & GLOBAL != 0);
                unsafe { (*block_ptr(n)).checked = c.epoch };
                unsafe {
                    (*block_ptr(n)).flags =
                        (*block_ptr(n)).flags & !(GLOBAL | LARGE) | walked & (GLOBAL | LARGE)
                };
                // (Current again: back on the linear chains a walk dropped it from, so the next invalidation of its pages
                // finds it.)
                relist(n);
                if walked & LARGE != 0 {
                    mark_large(&b);
                }
                // Relink plan: the links the last TLB event suspended into it are patched again (after the walk: it may
                // have killed their sources; after the flags: a global target's records belong to the TLB segment).
                #[cfg(not(feature = "pic-count"))]
                if block(n).inbound != 0 && c.links_susp != 0 {
                    relink(n);
                }
                #[cfg(feature = "pic-count")]
                if user(b.context) {
                    frame_met(block(n));
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
    #[cfg(feature = "link-check")]
    {
        check_l1_lists();
        check_out_slots();
        check_chains();
        check_tails();
    }
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
                }
            }
            let mut kept = 0;
            for k in 0..listed {
                let i = unsafe { l1_list(t, k).read() } as usize;
                if l1_block(t, i) != 0 {
                    unsafe { l1_list(t, kept).write(i as u32) };
                    kept += 1;
                } else {
                    unsafe { l1_owner(t, i).write(l1_owner(t, i).read() & area::L1_NEXT) };
                }
            }
            c.l1_listed[t] = kept as u64;
        }
    }
    #[cfg(feature = "link-check")]
    check_l1_lists();
    // A CR3 load leaves global blocks current: only the links into non-global ones are looked at.
    let scope = if global { TLB } else { CR3 };
    // Relink plan: the links are undone but their records kept (suspended); `pic-count` builds (their links go through
    // counting stubs) drop them as before.
    #[cfg(not(feature = "pic-count"))]
    {
        let undone = suspend(scope);
        #[cfg(feature = "link-check")]
        {
            check_out_slots();
            check_chains();
        }
        return undone;
    }
    #[cfg(feature = "pic-count")]
    prune_tlb(scope)
}
/// `link-check` builds, at the start of every TLB flush (indirect plan step 1): every block's appended words are what
/// [`append`] and [`link`] write, decoded here: each entry `bl` to the lookup form of its context's slot, or `b` to the miss
/// (none bound at its placement, a computed exit's; or a link point's not linked since), then `blr x1` exactly for a call
/// of its hint and `br x1` otherwise, then `b` to the miss; a computed exit with them `b` to them when its first word is
/// that `bl`, else to the miss (a word an armed stop patched is judged by its word before). A shared block's checked link
/// branches to its point's words. Halts `stop::LINK_CHECK` with the line, the block and the word's offset otherwise.
#[cfg(feature = "link-check")]
#[track_caller]
fn check_tails() {
    let line = u64::from(core::panic::Location::caller().line());
    let c = control();
    let armed = &c.armed_words[..(c.armed as usize).min(c.armed_words.len())];
    let word = |at: u64| armed.iter().find(|w| u64::from(w[0]) == at && word(at) == STOP_WORD).map_or_else(|| word(at), |w| w[1]);
    let target = |at: u64| at.wrapping_add((((word(at) << 6) as i32 >> 6) as i64 * 4) as u64);
    let bad = |n: usize, at: u64| -> ! { crate::execution::halt_native([line, n as u64, at], natemu_abi::execution::stop::LINK_CHECK) };
    for n in 0..c.blocks as usize {
        let b = block(n);
        let slot = (1..area::DISPATCH_SLOTS).find(|&k| c.slot_contexts[k] == b.context).map(slot_at);
        for p in [None].into_iter().chain((0..MAX_LINK_POINTS).map(Some)) {
            let Some(at) = b.tail(p) else { continue };
            let linked = match word(at) >> 26 {
                0x25 => slot.is_some_and(|k| target(at) == k + area::SLOT_LOOKUP as u64),
                0x05 if target(at) == area::DISPATCH_MISS as u64 => false,
                _ => bad(n, at),
            };
            if word(at + 4) != if b.tail_calls(p) { BLR_X1 } else { BR_X1 } {
                bad(n, at + 4)
            }
            if word(at + 8) >> 26 != 0x05 || target(at + 8) != area::DISPATCH_MISS as u64 {
                bad(n, at + 8)
            }
            match p {
                None => {
                    let exit = u64::from(b.code) + u64::from(b.dispatch_exit);
                    if word(exit) >> 26 != 0x05 || target(exit) != if linked { at } else { area::DISPATCH_MISS as u64 } {
                        bad(n, exit)
                    }
                }
                Some(k) => {
                    // (Its `svc` word, when a checked link patched it: `b` to these words, with their `bl` bound.)
                    let svc = u64::from(b.code) + svc_slot(b, &b.links[k]);
                    if word(svc) >> 26 == 0x05 && b.flags & DEAD == 0 && (target(svc) != at || !linked) {
                        bad(n, svc)
                    }
                }
            }
        }
    }
}
/// `link-check` builds, at the start and the end of every TLB flush: every outbound slot of every block names a live record of its
/// source and point (none a suspended one), and every live record is named by its source's slot for its point. Halts
/// `stop::LINK_CHECK` with the line, the block (or record) and the slot otherwise. (Relink plan: suspended records are named by none.)
#[cfg(feature = "link-check")]
#[track_caller]
fn check_out_slots() {
    let line = u64::from(core::panic::Location::caller().line());
    let c = control();
    for n in 0..c.blocks as usize {
        for k in 0..MAX_LINK_POINTS {
            let slot = u64::from(block(n).out[k]);
            let named = slot
                .checked_sub(1)
                .filter(|&i| i >= c.links_susp && i < c.links)
                .map(|i| unsafe { record(i as usize).read() });
            if slot != 0 && !named.is_some_and(|r| r.source as usize == n && r.point() == k) {
                crate::execution::halt_native([line, n as u64, slot], natemu_abi::execution::stop::LINK_CHECK)
            }
        }
    }
    for i in c.links_susp..c.links {
        let r = unsafe { record(i as usize).read() };
        if unsafe { *out_slot(&r) } != i as u32 + 1 {
            crate::execution::halt_native([line, i, u64::from(r.source)], natemu_abi::execution::stop::LINK_CHECK)
        }
    }
}
/// `link-check` builds, at the start and the end of every TLB flush (a CR3 load compacts the lists, which restores the
/// invariant a missed kill broke, so the start is the check that sees it): each table's list is at most the slots long,
/// names each slot at most once, and an owner word has [`area::L1_LISTED`] set exactly when its slot is on the list.
/// Halts `stop::LINK_CHECK` with the line, the table and the slot otherwise.
#[cfg(feature = "link-check")]
#[track_caller]
fn check_l1_lists() {
    let c = control();
    let line = u64::from(core::panic::Location::caller().line());
    for t in 0..area::L1_TABLES {
        let listed = c.l1_listed[t] as usize;
        let mut bad = (listed > area::L1_ENTRIES).then_some(0);
        // Each listed slot's bit is taken off as it is met: a clear one is met twice (or never was set).
        for k in 0..listed.min(area::L1_ENTRIES) {
            let i = unsafe { l1_list(t, k).read() } as usize;
            if i >= area::L1_ENTRIES {
                bad = Some(i);
                continue;
            }
            let o = unsafe { l1_owner(t, i).read() };
            if o & area::L1_LISTED == 0 {
                bad = Some(i);
            }
            unsafe { l1_owner(t, i).write(o & !area::L1_LISTED) };
        }
        // What is left set is on no list.
        for i in 0..area::L1_ENTRIES {
            if unsafe { l1_owner(t, i).read() } & area::L1_LISTED != 0 {
                bad = Some(i);
            }
        }
        for k in 0..listed.min(area::L1_ENTRIES) {
            let i = unsafe { l1_list(t, k).read() } as usize;
            if i < area::L1_ENTRIES {
                unsafe { l1_owner(t, i).write(l1_owner(t, i).read() | area::L1_LISTED) };
            }
        }
        if let Some(i) = bad {
            crate::execution::halt_native([line, t as u64, i as u64], natemu_abi::execution::stop::LINK_CHECK)
        }
    }
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
    exit_forms: u64,
) -> Option<&'static Block> {
    let c = control();
    let size = placed_size(h);
    let exit_forms = exit_forms as u32 & area::FORMS;
    let tail = tail_view(0, size, 0, links, dispatch_exit as u32, rep_exits as u32, exit_forms).tail_bytes();
    // Perf chunk 10: a template keeps its guest-RIP relocations (packed after its sites); one that cannot be packed is none.
    let reloc = |i: u32| unsafe {
        object
            .add(h.relocs_offset as usize + i as usize * size_of::<Reloc>())
            .cast::<Reloc>()
            .read_unaligned()
    };
    let packed = || {
        (0..h.relocs_count)
            .filter_map(move |i| pack_placed(&reloc(i), size)?)
    };
    let relocatable = relocatable
        && c.reuse != REUSE_OFF
        && h.relocs_count <= MAX_RELOCS
        && (0..h.relocs_count)
            .all(|i| pack_placed(&reloc(i), size).is_some());
    let count = if relocatable {
        packed().count() as u32
    } else {
        0
    };
    let staged = |o: u64| unsafe { object.add(h.code_offset as usize + o as usize).cast::<u32>().read_unaligned() };
    let staged_sites = unsafe { core::slice::from_raw_parts(object.add(h.sites_offset as usize).cast::<Site>(), h.sites_count as usize) };
    let (h_at, want) = split_point(staged, &links, dispatch_exit as u32, size, |o| {
        staged_sites.iter().any(|s| natemu_abi::code_object::kind::is_stub_kind(s.kind) && u64::from(s.native_offset) == o)
    });
    let (code, hot) = reserve(u64::from(size + tail), want)?;
    let code_end = code + u64::from(size + tail);
    let mut record = Block {
        rip: h.guest_rip,
        context,
        checked: c.epoch,
        source_gpa,
        next: [0; 2],
        lnext: [0; 2],
        code: code as u32,
        code_size: size,
        sites_count: h.sites_count,
        guest_bytes: h.guest_bytes,
        number: 0,
        flags: walked & (GLOBAL | LARGE)
            | if relocatable {
                RELOCATABLE | count << RELOCS_SHIFT
            } else {
                0
            }
            | if sti_first { STI_FIRST } else { 0 },
        dispatch_exit: dispatch_exit as u32,
        rep_exits: rep_exits as u32,
        links,
        shared: 0,
        inbound: 0,
        out: [0; MAX_LINK_POINTS],
        hot: 0,
        hot_size: 0,
        hot_body: 0,
        patch: [0; MAX_LINK_POINTS],
        exit_forms,
    };
    unsafe {
        // Copied above the fill point first: nothing refers to it until the record is filed.
        copy_record(
            (area::CODE_WRITE_VA + code) as *mut u8,
            object.add(h.code_offset as usize),
            size as usize,
        );
        relocate(code, dispatch_exit, dispatcher_for(context));
        append(&record, dispatcher_for(context));
        point_probes(code, staged_sites.iter().copied(), context);
        if hot != u64::MAX {
            (record.hot, record.hot_size) = (hot as u32, h_at);
            if !split(&mut record) {
                (record.hot, record.hot_size, record.hot_body, record.patch) = (0, 0, 0, [0; MAX_LINK_POINTS]);
            }
        }
        let placed = file(
            record,
            object.add(h.sites_offset as usize),
            size_of::<Site>() * h.sites_count as usize,
            4 * count as usize,
        )?;
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
        // PIC study counts (`pic-count` builds only).
        #[cfg(feature = "pic-count")]
        placed_pic(
            placed,
            core::slice::from_raw_parts(object.add(h.code_offset as usize), size as usize),
        );
        synchronize_code(area::CODE_WRITE_VA + code, area::CODE_VA + code, (size + tail) as usize);
        tails_counted(placed);
        c.code_used = code_end;
        if placed.hot_size != 0 {
            c.seg.hot_used = placed.hot_body() + u64::from(placed.hot_size);
        }
        Some(placed)
    }
}
/// PIC study counter `k` ([`count`]).
fn counted(k: usize) {
    control().pic_counts[k] += 1;
}
/// A user (CPL 3) context ([`natemu_abi::execution::Cpu::code_context`]'s CPL bits).
#[cfg_attr(not(feature = "pic-count"), allow(dead_code))]
pub(crate) fn user(context: u64) -> bool {
    context & 3 == 3
}
/// PIC study (`pic-count` builds), a block placed from the host's answer (`admitted`, its code as admitted): counted by
/// what sharing could do with it. Its first frame and linear page are noted ([`frame_met`]).
#[cfg(feature = "pic-count")]
fn placed_pic(b: &Block, admitted: &[u8]) {
    let u = user(b.context);
    if u {
        counted(count::HOST);
        frame_met(b);
    }
    if b.flags & RELOCATABLE == 0 {
        if u {
            counted(count::NOT_TEMPLATE);
        }
        return;
    }
    let k = if b.flags & GLOBAL != 0 {
        count::GLOBAL
    } else {
        pic::eligible(admitted, relocs(b), &b.links, b.rip, bits32(b.context))
            .err()
            .unwrap_or(count::ELIGIBLE)
    };
    counted(if u { k } else { count::KERNEL_TEMPLATES });
}
/// PIC study stage 3: whether templates of `context` are shared ([`Control::pic`]: user code, or every privilege level).
pub fn shares(context: u64) -> bool {
    natemu_abi::execution::cache::shares(control().pic, context)
}
/// The code of `context` is 32-bit code ([`natemu_abi::execution::context_bits32`]).
pub fn bits32(context: u64) -> bool {
    natemu_abi::execution::context_bits32(context)
}
/// PIC study stage 3, the lazy rewrite: the shared translation of the template `t` (absolute, live, not [`UNSHAREABLE`]),
/// made when a second address first needs its code (a template with no position-independent form, or a global mapping, is
/// marked [`UNSHAREABLE`] then, and `None`): `t`'s code copied [`PIC_LEAD`] bytes past the fill point's boundary with its link
/// points restored (first and `svc` words) and its computed exit relocated, rewritten ([`pic::rewrite`]), the full
/// entry's words before it, under the check its literal slots poisoned ([`poison`]), then the static proof against `t`'s
/// admitted code ([`pic::proved`]; `t`'s words with its link points restored). Filed as a [`TRANSLATION`] with `t`'s
/// sites and relocations (the hint table names it from now on); `t` stays as it is, absolute and valid at its address.
/// `None` (nothing filed): the code area or the arena is full, or the rewrite or its proof refused (counted
/// [`count::UNPROVED`], a resident fault the check halts on).
pub fn share(t: &Block) -> Option<&'static Block> {
    // (M2 task 4: a block whose code the resident changed, [`split`]'s re-pointed return: 32-bit code with the x87 run
    // test, left unshared.)
    if run_tested(t) {
        unsafe { (*block_ptr(t.number as usize)).flags |= UNSHAREABLE };
        return None;
    }
    let c = control();
    let start = c.code_used.next_multiple_of(CODE_ALIGN);
    let code = start + u64::from(PIC_LEAD);
    // (Indirect plan step 1: its words appended after its code, a shared translation's for its link points too.)
    let view = |links| tail_view(code, t.code_size, PIC, links, t.dispatch_exit, t.rep_exits, t.exit_forms);
    let code_end = code + u64::from(t.code_size) + u64::from(view(t.links).tail_bytes());
    if code_end > c.code_bytes {
        c.full[0] += 1;
        return None;
    }
    let (from, size) = (u64::from(t.code), t.code_size as usize);
    // `t`'s link points' words as admitted: (offset, first word) and (its `svc` word's offset, `svc #LINK`).
    let mut admitted = [(u64::MAX, 0u32); 2 * MAX_LINK_POINTS];
    for (k, p) in t.links.iter().enumerate().filter(|(_, p)| p.offset != 0) {
        admitted[2 * k] = (u64::from(p.offset), p.word);
        admitted[2 * k + 1] = (svc_slot(t, p), pic::SVC_LINK);
    }
    // (The probing split stubs' branches to their shared probes, as admitted: the placeholder, pointed again below.)
    // (Only they branch into the probes: every other branch of a block goes inside it, to a block or to the dispatcher.)
    let probe_branch = |o: u64| probe_branch(from + o);
    let original = |o: usize| {
        if probe_branch(o as u64) {
            return natemu_abi::execution::DISPATCH_PLACEHOLDER;
        }
        admitted
            .iter()
            .find(|&&(at, _)| at == o as u64)
            .map_or_else(|| word(from + o as u64), |&(_, w)| w)
    };
    let mut links = t.links;
    unsafe {
        let at = core::slice::from_raw_parts_mut((area::CODE_WRITE_VA + code) as *mut u8, size);
        copy_record(
            at.as_mut_ptr(),
            (area::CODE_WRITE_VA + from) as *const u8,
            size,
        );
        for &(o, w) in admitted.iter().filter(|(o, _)| *o != u64::MAX) {
            at[o as usize..o as usize + 4].copy_from_slice(&w.to_le_bytes());
        }
        for o in sites(t).iter().map(|s| u64::from(s.native_offset) + 4).filter(|&o| o + 4 <= size as u64 && probe_branch(o)) {
            at[o as usize..o as usize + 4].copy_from_slice(&natemu_abi::execution::DISPATCH_PLACEHOLDER.to_le_bytes());
        }
        // (Its first need decides: no position-independent form, or a global mapping, and it is never asked again.)
        let bits32 = bits32(t.context);
        if t.flags & GLOBAL != 0 || pic::eligible(at, relocs(t), &t.links, t.rip, bits32).is_err() {
            (*block_ptr(t.number as usize)).flags |= UNSHAREABLE;
            return None;
        }
        if pic::rewrite(at, relocs(t), &mut links, t.rip, bits32).is_err() {
            counted(count::UNPROVED);
            return None;
        }
        relocate(code, u64::from(t.dispatch_exit), dispatcher_for(t.context));
        append(&view(links), dispatcher_for(t.context));
        ((area::CODE_WRITE_VA + code - 16) as *mut [u32; 4]).write_volatile([
            0,
            0,
            0,
            pic::AND_PAGE,
        ]);
        poison(t, at);
        let rewritten = |o: usize| u32::from_le_bytes(at[o..o + 4].try_into().unwrap_or([0; 4]));
        if !pic::proved(
            original,
            rewritten,
            size,
            relocs(t),
            &links,
            t.rip,
            t.dispatch_exit,
            bits32,
        ) {
            counted(count::UNPROVED);
            return None;
        }
        point_probes(code, sites(t).iter().copied(), t.context);
    }
    let record = Block {
        checked: 0,
        next: [0; 2],
        lnext: [0; 2],
        code: code as u32,
        number: 0,
        flags: TRANSLATION
            | PIC
            | t.flags & (RELOCATABLE | MAX_RELOCS << RELOCS_SHIFT | STI_FIRST | LARGE),
        links,
        shared: 0,
        inbound: 0,
        out: [0; MAX_LINK_POINTS],
        hot: 0,
        hot_size: 0,
        hot_body: 0,
        patch: [0; MAX_LINK_POINTS],
        ..*t
    };
    let tail = size_of::<Site>() * t.sites_count as usize + 4 * relocs(t).len();
    let placed = unsafe { file(record, sites(t).as_ptr().cast(), tail, 0)? };
    synchronize_code(
        area::CODE_WRITE_VA + start,
        area::CODE_VA + start,
        (code_end - start) as usize,
    );
    c.code_used = code_end;
    tails_counted(placed);
    counted(count::SHARES);
    Some(placed)
}
/// PIC study stage 3: an [`INSTANCE`] of translation `t` at `rip` (same half and page offset; `source_gpa` its frames now,
/// `t`'s, checked by the caller; `walked` as for [`place`]): `t`'s link-point targets moved to `rip`, no code. `Ok(None)`
/// when full (as [`place`]); `Err`: perf chunk 10's `PLACE` refusal (counted [`count::INSTANCE_REFUSED`]): another half or
/// page offset, a page within the rewrite's reach of a canonical edge ([`pic::reaches`]), a block end not canonical, a
/// global mapping (shared code is for non-global mappings only: a CR3 load ends every instance's currency). 32-bit code:
/// the block below 4 GiB, as a copy's ([`natemu_abi::execution::in_32_bits`]: its 32-bit form wraps there exactly), its
/// targets moved modulo 2^32 ([`pic::moved`]).
pub fn instance(
    t: &Block,
    rip: u64,
    source_gpa: [u64; 2],
    walked: u32,
) -> Result<Option<&'static Block>, usize> {
    let c = control();
    let bits32 = bits32(t.context);
    let place = if bits32 {
        natemu_abi::execution::in_32_bits(rip, t.guest_bytes)
    } else {
        pic::reaches(rip & !0xfff)
            && rip
                .checked_add(t.guest_bytes.into())
                .is_some_and(natemu_abi::execution::canonical)
    };
    if (t.rip ^ rip) & (1 << 63 | 0xfff) != 0 || !place || walked & GLOBAL != 0 {
        counted(count::INSTANCE_REFUSED);
        return Err(natemu_abi::execution::refusal::PLACE);
    }
    let mut links = t.links;
    for p in links.iter_mut().filter(|p| p.offset != 0) {
        p.target = pic::moved(p.target, t.rip, rip, bits32);
    }
    // `code` at the fill point keeps the records in address order ([`block_at`]); a size of 0 holds no PC.
    let record = Block {
        rip,
        context: t.context,
        checked: c.epoch,
        source_gpa,
        next: [0; 2],
        lnext: [0; 2],
        code: c.code_used as u32,
        code_size: 0,
        sites_count: 0,
        guest_bytes: t.guest_bytes,
        number: 0,
        flags: INSTANCE | walked & LARGE | t.flags & STI_FIRST,
        dispatch_exit: 0,
        rep_exits: t.rep_exits,
        links,
        shared: t.number,
        inbound: 0,
        out: [0; MAX_LINK_POINTS],
        hot: 0,
        hot_size: 0,
        hot_body: 0,
        patch: [0; MAX_LINK_POINTS],
        exit_forms: 0,
    };
    let placed = unsafe { file(record, core::ptr::null(), 0, 0) };
    if placed.is_some() {
        counted(count::INSTANCES);
    }
    #[cfg(feature = "pic-count")]
    if let Some(p) = placed.filter(|p| user(p.context)) {
        frame_met(p);
    }
    Ok(placed)
}
/// PIC study stage 3: the word at offset `o` of `t`'s code with its link points' words as admitted (their first words and,
/// for every link point, `svc #LINK` after the move): what its static proof reads ([`pic::proved`]) while links patch it.
pub fn unlinked_word(t: &Block, o: usize) -> u32 {
    let o = o as u64;
    for p in t.links.iter().filter(|p| p.offset != 0) {
        if o == u64::from(p.offset) {
            return p.word;
        }
        if o == svc_slot(t, p) {
            return pic::SVC_LINK;
        }
    }
    // (A probing split stub's branch, which the resident points at its shared probe: the placeholder it was admitted as.)
    if probe_branch(u64::from(t.code) + o) {
        return natemu_abi::execution::DISPATCH_PLACEHOLDER;
    }
    word(u64::from(t.code) + o)
}
/// The word at code-area offset `at` is a `b` into the shared probes: a probing split stub's second word as the resident
/// points it ([`point_probes`]; every other branch of a block goes inside it, to a block or to the dispatcher).
pub fn probe_branch(at: u64) -> bool {
    word(at) & 0xfc00_0000 == 0x1400_0000 && (area::PROBES as u64..area::HEAD_BYTES as u64).contains(&branch_to(at))
}
/// The block whose code `b` runs: an [`INSTANCE`]'s translation, else `b`.
pub fn code_of(b: &Block) -> &'static Block {
    if b.flags & INSTANCE != 0 {
        block(b.shared as usize)
    } else {
        unsafe { &*(b as *const Block) }
    }
}
/// PIC study stage 3: the address the running block `b` (its code's, [`block_at`]) runs for: a [`PIC`] translation's, the
/// page base slot plus its page offset (the instance it was entered for; the resident checks the slot only where it links,
/// `accepted`); any other block's, its own.
pub fn running_rip(b: &Block) -> u64 {
    if b.flags & PIC == 0 {
        return b.rip;
    }
    let slot = unsafe {
        (natemu_abi::execution::FRAME_VA as *const u64)
            .byte_add(natemu_abi::execution::frame::PAGE_BASE as usize)
            .read_volatile()
    };
    // (32-bit code's adds build its addresses modulo 2^32, whatever the slot's high half: a forged slot's too.)
    let rip = slot & !0xfff | b.rip & 0xfff;
    if bits32(b.context) { rip & 0xffff_ffff } else { rip }
}
/// PIC study stage 2, under the check ([`REUSE_CHECK`]): a position-independent block's literal slots (the tail's RIP,
/// left as the template's in its copies) hold a non-canonical value, so code that read one would be caught by the
/// suites (the tail-literal test: none does).
fn poison(b: &Block, code: &mut [u8]) {
    if control().reuse != REUSE_CHECK {
        return;
    }
    for &r in relocs(b)
        .iter()
        .filter(|&&r| r & natemu_abi::execution::RELOC_MOVE == 0)
    {
        let at = (r >> 6) as usize * 4;
        code[at..at + 8].copy_from_slice(&POISON.to_le_bytes());
    }
}
/// The crash tripwire ([`Control::trip`]): one user-mode fault at `rip` in block `b` (the code that ran: a shared
/// translation's when shared).
pub fn trip(b: &Block, rip: u64, vector: u64, cr2: u64, cr3: u64) {
    let c = control();
    let targets = b
        .links
        .iter()
        .enumerate()
        .fold(0, |t, (k, p)| t | (p.target & 0xffff) << (16 * k));
    let (flags, code_rip) = (u64::from(b.flags) | u64::from(b.shared) << 32, b.rip);
    let slot = unsafe {
        (natemu_abi::execution::FRAME_VA as *const u64)
            .byte_add(natemu_abi::execution::frame::PAGE_BASE as usize)
            .read_volatile()
    };
    c.trip[c.trip_count as usize % c.trip.len()] =
        [rip, vector, cr2, flags, code_rip, slot, cr3, targets];
    c.trip_count += 1;
}
/// [`poison`]'s value.
pub const POISON: u64 = 0x8000_dead_beef_0000;
/// PIC study (`pic-count` builds): a user block's first frame met at its first linear page in this TLB epoch (one address space): counted
/// ([`count::ALIASES`]) when the frame was last met at another linear page in the same epoch ([`area::PIC_FRAMES`]).
#[cfg(feature = "pic-count")]
pub fn frame_met(b: &Block) {
    let c = control();
    let frame = b.source_gpa[0] >> 12;
    let i = (frame.wrapping_mul(0x9e37_79b9_7f4a_7c15)
        >> (64 - area::PIC_FRAME_ENTRIES.trailing_zeros())) as usize;
    let at = lookup_area::<[u64; 2]>(area::PIC_FRAMES).wrapping_add(i);
    let now = [(frame + 1) | (c.epoch & 0xffff_ffff) << 32, b.rip >> 12];
    let [key, page] = unsafe { at.read() };
    if key == now[0] && page != now[1] {
        counted(count::ALIASES);
    }
    unsafe { at.write(now) };
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
    let fits = b.flags & (DEAD | SYSTEM | STEP | RELOCATABLE) == RELOCATABLE
        && b.source_gpa[0] == gpa
        && b.context == context;
    (fits && (b.rip ^ rip) & (1 << 63 | 0xfff) == 0).then_some(b)
}
/// The hint table's entry for (`gpa`'s frame, `rip`'s half and page offset, `context`).
fn hint(gpa: u64, rip: u64, context: u64) -> *mut u32 {
    let key = (gpa & !0xfff | rip & 0xfff) ^ (rip >> 63) << 62 ^ context.rotate_left(29);
    let i = (key.wrapping_mul(0x9e37_79b9_7f4a_7c15) >> (64 - area::HINT_ENTRIES.trailing_zeros()))
        as usize;
    lookup_area::<u32>(area::HINTS).wrapping_add(i)
}
/// Perf chunk 10: places a copy of template `b` for `rip` (same half and page offset; `source_gpa` its frames now, the
/// first `b`'s, the second checked by the caller; `walked` as for [`place`]), its code rewritten for `rip` by the shared
/// re-check ([`natemu_abi::execution::relocate_code`]) above the fill point, then filed as an ordinary block (with `b`'s
/// sites and relocations). `Ok(None)` when full (as [`place`]); `Err(kind)`: the re-check refused (nothing filed).
pub fn copy(
    b: &Block,
    rip: u64,
    source_gpa: [u64; 2],
    walked: u32,
) -> Result<Option<&'static Block>, usize> {
    let c = control();
    if b.flags & PIC != 0 {
        return Err(natemu_abi::execution::refusal::PLACE); // a translation is shared, not copied ([`instance`])
    }
    // M2 task 4 and reuse: a block that begins with the test for an open x87 run has one word the resident may have
    // changed besides its link points, its run end's return ([`split`]): the copy gets it as admitted ([`run_return`]).
    // Any other word there is no block the resident made: not copied.
    let run = run_return(b);
    if run.is_some_and(|(o, admitted, split)| {
        let w = word(u64::from(b.code) + o);
        w != admitted && Some(w) != split
    }) {
        return Err(natemu_abi::execution::refusal::PLACE);
    }
    let (h_at, want) = split_point(|o| word(u64::from(b.code) + o), &b.links, b.dispatch_exit, b.code_size, |o| {
        sites(b).iter().any(|s| natemu_abi::code_object::kind::is_stub_kind(s.kind) && u64::from(s.native_offset) == o)
    });
    let view = tail_view(0, b.code_size, 0, b.links, b.dispatch_exit, b.rep_exits, b.exit_forms);
    let Some((code, hot)) = reserve(u64::from(b.code_size + view.tail_bytes()), want) else {
        return Ok(None);
    };
    let code_end = code + u64::from(b.code_size + view.tail_bytes());
    let mut links = b.links;
    unsafe {
        copy_record(
            (area::CODE_WRITE_VA + code) as *mut u8,
            (area::CODE_WRITE_VA + u64::from(b.code)) as *const u8,
            b.code_size as usize,
        );
        let at = core::slice::from_raw_parts_mut(
            (area::CODE_WRITE_VA + code) as *mut u8,
            b.code_size as usize,
        );
        // (Its link points' `svc` words as admitted, as `share` restores them: a checked link patches one there;
        // `relocate_code` restores the first words.)
        for p in b.links.iter().filter(|p| p.offset != 0) {
            let o = svc_slot(b, p) as usize;
            at[o..o + 4].copy_from_slice(&pic::SVC_LINK.to_le_bytes());
        }
        if let Some((o, admitted, _)) = run {
            at[o as usize..o as usize + 4].copy_from_slice(&admitted.to_le_bytes());
        }
        let bits32 = natemu_abi::execution::context_bits32(b.context);
        natemu_abi::execution::relocate_code_mode(at, relocs(b), &mut links, b.rip, rip, b.guest_bytes, bits32)?;
        relocate(code, u64::from(b.dispatch_exit), dispatcher_for(b.context));
        append(&Block { code: code as u32, ..view }, dispatcher_for(b.context));
        point_probes(code, sites(b).iter().copied(), b.context);
        synchronize_code(
            area::CODE_WRITE_VA + code,
            area::CODE_VA + code,
            (code_end - code) as usize,
        );
    }
    let mut record = Block {
        rip,
        checked: c.epoch,
        source_gpa,
        next: [0; 2],
        lnext: [0; 2],
        code: code as u32,
        number: 0,
        flags: walked & (GLOBAL | LARGE)
            | b.flags & (RELOCATABLE | MAX_RELOCS << RELOCS_SHIFT | STI_FIRST | UNSHAREABLE),
        links,
        inbound: 0,
        out: [0; MAX_LINK_POINTS],
        hot: 0,
        hot_size: 0,
        hot_body: 0,
        patch: [0; MAX_LINK_POINTS],
        ..*b
    };
    if hot != u64::MAX {
        (record.hot, record.hot_size) = (hot as u32, h_at);
        if !split(&mut record) {
            (record.hot, record.hot_size, record.hot_body, record.patch) = (0, 0, 0, [0; MAX_LINK_POINTS]);
        }
    }
    let tail = size_of::<Site>() * b.sites_count as usize + 4 * relocs(b).len();
    let placed = unsafe { file(record, sites(b).as_ptr().cast(), tail, 0) };
    if let Some(p) = placed {
        tails_counted(p);
        c.code_used = code_end;
        if let Some(p) = placed.filter(|p| p.hot_size != 0) {
            c.seg.hot_used = p.hot_body() + u64::from(p.hot_size);
        }
    }
    #[cfg(feature = "pic-count")]
    if let Some(p) = placed {
        if user(p.context) {
            counted(count::COPIES);
            if p.flags & UNSHAREABLE == 0 {
                counted(count::COPIES_COVERED);
            }
            frame_met(p);
        } else {
            counted(count::KERNEL_COPIES);
        }
    }
    Ok(placed)
}
/// Cross-vCPU reuse (chunk 3): places a block for `rip` under `context` from pool entry `e` (published by the host: its code as
/// admitted, at `e.rip` of the same half and page offset; `source_gpa` and `walked` as for [`copy`]), its code copied above the
/// fill point and rewritten for `rip` by the same re-check as [`copy`] ([`natemu_abi::execution::relocate_code`]), its
/// computed exit relocated to `context`'s dispatcher slot, then filed as the ordinary template a host-placed one is ([`place`]):
/// its sites and sorted relocations after it, [`RELOCATABLE`] (the pool holds templates only). `Ok(None)` when full (as
/// [`place`]); `Err(kind)`: the re-check refused (nothing filed).
pub fn copy_pooled(
    e: &natemu_abi::pool::Entry,
    rip: u64,
    context: u64,
    source_gpa: [u64; 2],
    walked: u32,
) -> Result<Option<&'static Block>, usize> {
    let c = control();
    // (The code as admitted starts with its header word, `udf #tail offset`; up to there.)
    let head = e.code.get(..4).map_or(u32::MAX, |w| u32::from_le_bytes([w[0], w[1], w[2], w[3]]));
    let len = if head % 4 == 0 && (head as usize) <= e.code.len() {
        head as usize
    } else {
        e.code.len()
    };
    let code_size = len as u64;
    let (h_at, want) = split_point(
        |o| e.code.get(o as usize..o as usize + 4).map_or(0, |w| u32::from_le_bytes([w[0], w[1], w[2], w[3]])),
        &e.links,
        e.dispatch_exit,
        len as u32,
        |o| (0..e.sites_count()).any(|i| natemu_abi::code_object::kind::is_stub_kind(e.site(i).kind) && u64::from(e.site(i).native_offset) == o),
    );
    let exit_forms = e.exit_forms & area::FORMS;
    let view = tail_view(0, len as u32, 0, e.links, e.dispatch_exit, e.rep_exits, exit_forms);
    let Some((code, hot)) = reserve(code_size + u64::from(view.tail_bytes()), want) else {
        return Ok(None);
    };
    let code_end = code + code_size + u64::from(view.tail_bytes());
    let count = e.relocs_count();
    // (decode bounds the count by `MAX_RELOCS`.)
    let mut relocs = [0u32; MAX_RELOCS as usize];
    let mut kept = 0;
    for i in 0..count {
        let r = e.reloc(i);
        // (A literal in the dropped tail is not placed.)
        if r & natemu_abi::execution::RELOC_MOVE != 0 || (r >> 6) as usize * 4 + 8 <= len {
            relocs[kept] = r;
            kept += 1;
        }
    }
    let count = kept;
    let mut links = e.links;
    unsafe {
        copy_record(
            (area::CODE_WRITE_VA + code) as *mut u8,
            e.code.as_ptr(),
            len,
        );
        let at = core::slice::from_raw_parts_mut((area::CODE_WRITE_VA + code) as *mut u8, len);
        let bits32 = natemu_abi::execution::context_bits32(context);
        natemu_abi::execution::relocate_code_mode(at, &relocs[..count], &mut links, e.rip, rip, e.guest_bytes, bits32)?;
        relocate(code, u64::from(e.dispatch_exit), dispatcher_for(context));
        append(&Block { code: code as u32, ..view }, dispatcher_for(context));
        point_probes(code, (0..e.sites_count()).map(|i| e.site(i)), context);
        synchronize_code(area::CODE_WRITE_VA + code, area::CODE_VA + code, (code_end - code) as usize);
    }
    let mut record = Block {
        rip,
        context,
        checked: c.epoch,
        source_gpa,
        next: [0; 2],
        lnext: [0; 2],
        code: code as u32,
        code_size: len as u32,
        sites_count: e.sites_count() as u32,
        guest_bytes: e.guest_bytes,
        number: 0,
        flags: walked & (GLOBAL | LARGE)
            | RELOCATABLE
            | (count as u32) << RELOCS_SHIFT
            | if e.sti_first { STI_FIRST } else { 0 },
        dispatch_exit: e.dispatch_exit,
        rep_exits: e.rep_exits,
        links,
        shared: 0,
        inbound: 0,
        out: [0; MAX_LINK_POINTS],
        hot: 0,
        hot_size: 0,
        hot_body: 0,
        patch: [0; MAX_LINK_POINTS],
        exit_forms,
    };
    if hot != u64::MAX {
        (record.hot, record.hot_size) = (hot as u32, h_at);
        if !split(&mut record) {
            (record.hot, record.hot_size, record.hot_body, record.patch) = (0, 0, 0, [0; MAX_LINK_POINTS]);
        }
    }
    let tables = e.tables();
    let placed = unsafe { file(record, tables.as_ptr(), tables.len(), 0) };
    if let Some(p) = placed {
        tails_counted(p);
        c.code_used = code_end;
        if let Some(p) = placed.filter(|p| p.hot_size != 0) {
            c.seg.hot_used = p.hot_body() + u64::from(p.hot_size);
        }
    }
    Ok(placed)
}
/// K2: enters the host's decode `i` of the system instruction at `rip` (`guest_bytes` long, from `source_gpa`; `walked` as
/// for [`place`]) in the cache for `context` as a [`SYSTEM`] record (no code); `None` when the metadata arena or the
/// block index is full.
pub fn place_system(
    rip: u64,
    context: u64,
    source_gpa: [u64; 2],
    walked: u32,
    guest_bytes: u32,
    i: &System,
) -> Option<&'static Block> {
    // `code` at the fill point keeps the records in address order ([`block_at`]); a size of 0 holds no PC.
    let code = control().code_used as u32;
    let links = [LinkPoint::NONE; MAX_LINK_POINTS];
    let record = Block {
        rip,
        context,
        checked: control().epoch,
        source_gpa,
        next: [0; 2],
        lnext: [0; 2],
        code,
        code_size: 0,
        sites_count: 0,
        guest_bytes,
        number: 0,
        flags: SYSTEM | walked & (GLOBAL | LARGE),
        dispatch_exit: 0,
        rep_exits: 0,
        links,
        shared: 0,
        inbound: 0,
        out: [0; MAX_LINK_POINTS],
        hot: 0,
        hot_size: 0,
        hot_body: 0,
        patch: [0; MAX_LINK_POINTS],
        exit_forms: 0,
    };
    unsafe { file(record, (i as *const System).cast(), size_of::<System>(), 0) }
}
/// The decode a [`SYSTEM`] record holds.
pub fn system_of(b: &Block) -> System {
    unsafe { (b as *const Block).add(1).cast::<System>().read() }
}
const _: () = assert!(size_of::<Block>() % align_of::<System>() == 0);

/// M2 Part 2: files the boot-time interpreter's record `i` of the instruction at linear address `linear`, under `key`
/// ([`area::INTERPRETED`] with [`SYSTEM`]: no code, no source page, never in L1; validated by its bytes, so its currency is
/// never looked at), or replaces the record filed under that key in place (a stale one: a key has one record). `None` when
/// the metadata arena or the block index is full.
pub fn place_interpreted(linear: u64, key: u64, i: &Insn) -> Option<()> {
    if let Some(b) = interpreted_at(linear, key) {
        // Only the tail changes: the first filing's `guest_bytes`, its linear chains and its regions stay as that record's
        // length set them. Harmless: what those steer (an INVLPG or a write making records not current, a cut) ends at
        // most in this record's currency, which nothing reads for an interpreter record, and every execution checks
        // the new record's own bytes ([`interpreted_at`]'s caller validates `bytes[..length]` against memory).
        // SAFETY: the record's tail is its `Insn` (filed below); the arena is this vCPU's and writable.
        unsafe { block_ptr(b.number as usize).add(1).cast::<Insn>().write(*i) };
        return Some(());
    }
    let record = Block {
        rip: linear,
        context: key,
        checked: control().epoch,
        source_gpa: [u64::MAX; 2],
        next: [0; 2],
        lnext: [0; 2],
        code: control().code_used as u32,
        code_size: 0,
        sites_count: 0,
        guest_bytes: u32::from(i.length),
        number: 0,
        flags: SYSTEM | area::INTERPRETED,
        dispatch_exit: 0,
        rep_exits: 0,
        links: [LinkPoint::NONE; MAX_LINK_POINTS],
        shared: 0,
        inbound: 0,
        out: [0; MAX_LINK_POINTS],
        hot: 0,
        hot_size: 0,
        hot_body: 0,
        patch: [0; MAX_LINK_POINTS],
        exit_forms: 0,
    };
    unsafe { file(record, (i as *const Insn).cast(), size_of::<Insn>(), 0) }.map(|_| ())
}
/// M2 Part 2: the interpreter's live record for (`linear`, `key`), current or not (its bytes decide). Its own probe of L2,
/// so the translated blocks' lookups stay as they were; no code context equals an interpreted key, so it finds only
/// interpreter records.
pub fn interpreted_at(linear: u64, key: u64) -> Option<&'static Block> {
    let mut i = hash(linear, key);
    loop {
        let [k, number] = unsafe { l2(i).read() };
        if number == 0 {
            return None;
        }
        let b = block(number as usize - 1);
        if k == linear && b.rip == linear && b.context == key && b.flags & (DEAD | area::INTERPRETED) == area::INTERPRETED {
            return Some(b);
        }
        i = (i + 1) & (area::L2_ENTRIES - 1);
    }
}
/// The record an interpreter record holds ([`place_interpreted`]).
pub fn interpreted_of(b: &Block) -> Insn {
    unsafe { (b as *const Block).add(1).cast::<Insn>().read() }
}
const _: () = assert!(size_of::<Block>() % align_of::<Insn>() == 0);
/// Files `record` (its number assigned here) with `tail_bytes` from `tail` after it (sites, or a decode) and `extra` bytes
/// the caller writes after them (a template's relocations) in the metadata arena, the block index and L2 (and, when
/// [`RELOCATABLE`], the hint table); `None` when either is full.
unsafe fn file(
    record: Block,
    tail: *const u8,
    tail_bytes: usize,
    extra: usize,
) -> Option<&'static Block> {
    let c = control();
    let meta = c.meta_used.next_multiple_of(align_of::<Block>() as u64);
    let meta_end = meta + (size_of::<Block>() + tail_bytes + extra) as u64;
    if meta_end > area::META_BYTES as u64 || c.blocks as usize >= area::MAX_BLOCKS {
        c.full[if meta_end > area::META_BYTES as u64 {
            1
        } else {
            2
        }] += 1;
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
                *summary(area::CODE_PAGES).add(page / MASK_PAGES_PER_WORD / 64) |=
                    1 << (page / MASK_PAGES_PER_WORD % 64);
            }
        }
        // (PIC study stage 3: a translation is current nowhere, only its instances: not on the linear chains, not in L2.)
        let current = record.flags & TRANSLATION == 0;
        let mut lnext = [0; 2];
        if current {
            let [b0, b1] = lpages(&record).map(lbucket);
            lnext[0] = lhead(b0).read();
            lhead(b0).write(n as u32 + 1);
            if b1 != b0 {
                lnext[1] = lhead(b1).read();
                lhead(b1).write(n as u32 + 1);
            }
        }
        at.write(Block {
            number: n as u32,
            next,
            lnext,
            hot: if record.hot_size != 0 { record.hot } else { c.seg.hot_used as u32 },
            ..record
        });
        if current {
            mark_regions(&mut c.regions, &record);
            if record.flags & LARGE != 0 {
                mark_large(&record);
            }
        }
        if tail_bytes != 0 {
            copy_record(at.add(1).cast(), tail, tail_bytes);
        }
        index(n).write(meta as u32);
        if current {
            let mut i = hash(record.rip, record.context);
            while l2(i).read()[1] != 0 {
                i = (i + 1) & (area::L2_ENTRIES - 1);
            }
            l2(i).write([record.rip, n as u64 + 1]);
        }
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
        shared: 0,
        inbound: 0,
        out: [0; MAX_LINK_POINTS],
        hot: 0,
        hot_size: 0,
        hot_body: 0,
        patch: [0; MAX_LINK_POINTS],
        exit_forms: 0,
    };
    unsafe {
        let at = lookup_area::<Block>(area::STEP_RECORD);
        at.write(record);
        copy_record(at.add(1).cast(), object.add(h.sites_offset as usize), sites);
        copy_record(
            SLOT_WRITE_VA as *mut u8,
            object.add(h.code_offset as usize),
            h.code_size as usize,
        );
        relocate(SLOT as u64, dispatch_exit, DISPATCH_MISS as u64);
        // (A step unit's split stubs are `brk`s, fexc_step_unit; pointed all the same, should one probe.)
        point_probes(SLOT as u64, self::sites(&*at).iter().copied(), context);
        synchronize_code(SLOT_WRITE_VA, SLOT_VA, h.code_size as usize);
        Some(&*at)
    }
}

/// `bytes` of code written at `write` (a writable alias) become visible to instruction fetch at `exec` (its executable alias).
fn synchronize_code(write: u64, exec: u64, bytes: usize) {
    let ctr = control().ctr_el0;
    unsafe {
        if ctr & CTR_IDC == 0 {
            let dline = 4u64 << ((ctr >> 16) & 15);
            let mut at = 0;
            while at < bytes as u64 + (write & (dline - 1)) {
                asm!("dc cvau, {0}", in(reg) (write & !(dline - 1)) + at);
                at += dline;
            }
            asm!("dsb ish");
        } else {
            asm!("dsb ishst");
        }
        if ctr & CTR_DIC == 0 {
            let iline = 4u64 << (ctr & 15);
            let mut at = 0;
            while at < bytes as u64 + (exec & (iline - 1)) {
                asm!("ic ivau, {0}", in(reg) (exec & !(iline - 1)) + at);
                at += iline;
            }
        }
        asm!("dsb ish", "isb");
    }
}
/// Reads CTR_EL0 once into [`Control::ctr_el0`] (resident start-up): every later code synchronization uses that copy, as a
/// read of the register at each one could trap to the host.
pub fn read_ctr_el0() {
    let ctr: u64;
    unsafe { asm!("mrs {0}, ctr_el0", out(reg) ctr) };
    control().ctr_el0 = ctr;
}
/// CTR_EL0.IDC = 1: a data cache clean to the point of unification is not required for instruction-to-data coherence, so
/// `dc cvau` is skipped and a store-ordering barrier suffices (as Linux's `caches_clean_inval_pou`); CTR_EL0.DIC = 1:
/// `ic ivau` is not required either (only `dsb ish; isb`).
const CTR_IDC: u64 = 1 << 28;
const CTR_DIC: u64 = 1 << 29;
/// Cleans the line at `a` to the point of unification, unless `ctr` says that is not needed (IDC).
fn clean_pou(ctr: u64, a: u64) {
    if ctr & CTR_IDC == 0 {
        unsafe { asm!("dc cvau, {0}", in(reg) a) };
    }
}
/// The barrier after the cleans: with IDC only the code stores must be ordered before the invalidates.
fn clean_barrier(ctr: u64) {
    unsafe {
        if ctr & CTR_IDC == 0 {
            asm!("dsb ish");
        } else {
            asm!("dsb ishst");
        }
    }
}
/// Invalidates the instruction line at `a` to the point of unification, unless `ctr` says that is not needed (DIC).
fn invalidate_pou(ctr: u64, a: u64) {
    if ctr & CTR_DIC == 0 {
        unsafe { asm!("ic ivau, {0}", in(reg) a) };
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
        // PIC study (`pic-count` builds only): through a counting stub ([`computed`]).
        #[cfg(feature = "pic-count")]
        let to = computed(to);
        unsafe { ((area::CODE_WRITE_VA + at) as *mut u32).write_volatile(branch(at, to)) };
    }
}
use area::tail_words;
/// Indirect plan step 1: writes the words `b` (its record as it will be filed, its code placed at `b.code` and relocated,
/// not yet synchronized) has appended after its code ([`Block::tail`]), for the slot at `slot` ([`dispatcher_for`] its
/// context), and makes its computed exit's `b` go to its own words when it has them and a slot (with none it stays `b` to
/// the miss, [`relocate`]). Always every entry, whatever was in the code area there before (a checked link reaches a link
/// point's entry only once [`link`] made it, after rewriting its `bl` for the slot then).
fn append(b: &Block, slot: u64) {
    for p in [None].into_iter().chain((0..MAX_LINK_POINTS).map(Some)) {
        let Some(at) = b.tail(p) else {
            continue;
        };
        let words = tail_words(at, slot, b.tail_calls(p));
        unsafe { ((area::CODE_WRITE_VA + at) as *mut [u32; 3]).write_volatile(words) };
        if p.is_none() && slot != area::DISPATCH_MISS as u64 {
            let exit = u64::from(b.code) + u64::from(b.dispatch_exit);
            unsafe { ((area::CODE_WRITE_VA + exit) as *mut u32).write_volatile(branch(exit, at)) };
        }
    }
}
/// Indirect plan step 1's counters for a block just filed with appended words: its computed exit's (as a jump or a call)
/// and its link points'.
fn tails_counted(b: &Block) {
    if b.tail(None).is_some() {
        counted(if b.tail_calls(None) { count::TAIL_CALLS } else { count::TAILS });
    }
    for _ in (0..MAX_LINK_POINTS).filter(|&k| b.tail(Some(k)).is_some()) {
        counted(count::TAIL_POINTS);
    }
}
/// [`Block::tail`]'s view of a block about to be placed at `code` (`code_size` bytes) with these exits, `flags` its kind
/// ([`PIC`] or not).
fn tail_view(code: u64, code_size: u32, flags: u32, links: [LinkPoint; MAX_LINK_POINTS], dispatch_exit: u32, rep_exits: u32, exit_forms: u32) -> Block {
    Block {
        rip: 0,
        context: 0,
        checked: 0,
        source_gpa: [u64::MAX; 2],
        next: [0; 2],
        lnext: [0; 2],
        code: code as u32,
        code_size,
        sites_count: 0,
        guest_bytes: 0,
        number: 0,
        flags,
        dispatch_exit,
        rep_exits,
        links,
        shared: 0,
        inbound: 0,
        out: [0; MAX_LINK_POINTS],
        hot: 0,
        hot_size: 0,
        hot_body: 0,
        patch: [0; MAX_LINK_POINTS],
        exit_forms,
    }
}

/// The shared probes of page-crossing stores ([`natemu_abi::execution::probe`]), once, before any block runs: every
/// probe's words in the code area's head.
pub fn fill_probes() {
    use natemu_abi::execution::probe;
    for i in 0..probe::THUNKS as u32 {
        let w = probe::words(i);
        unsafe { copy_record((area::CODE_WRITE_VA + probe::offset(i)) as *mut u8, w.as_ptr().cast(), size_of_val(&w)) };
    }
    synchronize_code(area::CODE_WRITE_VA + area::PROBES as u64, area::PROBES_VA, area::PROBES_BYTES);
}
/// Points the probing split stubs of the block just copied to code-area offset `code` (before its code is synchronized),
/// whose sites are `sites` and code context `context`, at their shared probes: each such stub's second word (the
/// admitted placeholder, or a branch from where the block was before) becomes `b` to its store's probe, which the site
/// names ([`natemu_abi::execution::probe::index`]; admission checked there is one). A split stub that is a `brk` stays.
fn point_probes(code: u64, sites: impl Iterator<Item = Site>, context: u64) {
    use natemu_abi::execution::probe;
    let bits32 = natemu_abi::execution::context_bits32(context);
    for s in sites.filter(|s| s.kind == natemu_abi::code_object::kind::SPLIT) {
        let at = code + u64::from(s.native_offset);
        if probe::adr_x0_target(word(at), at).is_none() {
            continue;
        }
        let Some(i) = probe::index(bits32, s.base_reg, u32::from(s.width)) else {
            crate::execution::halt_native([at, u64::from(s.base_reg), u64::from(s.width)], natemu_abi::execution::stop::SITE_MISMATCH)
        };
        unsafe { ((area::CODE_WRITE_VA + at + 4) as *mut u32).write_volatile(probe::branch_word(at + 4, i)) };
    }
}
/// Arms a precise stop in `b` (the block the doorbell interrupted; live, or the step block): every exit's word (each
/// link point's first word, linked or not, and the computed exit's `b`) becomes [`STOP_WORD`], recorded with its word
/// for [`disarm`]. Every path out of a block passes one of them or enters the resident (`svc`, a stub, an abort):
/// branches inside a block go forward and none enters a link point after its first word (admission). False (nothing
/// patched) when a stop is armed already: every resident entry disarms first, so a second arm is a resident bug.
pub fn arm(b: &Block) -> bool {
    arm_words(b, true)
}
/// PIC study stage 2 test ([`area::PIC_TEST_SVC`]): [`arm`]'s words of `b` but only the `svc` words of its links there;
/// false if none (nothing armed).
pub fn arm_svc(b: &Block) -> bool {
    arm_words(b, false) && control().armed != 0
}
fn arm_words(b: &Block, all: bool) -> bool {
    let c = control();
    if c.armed != 0 {
        return false;
    }
    // PIC study stage 2: a link point linked in its `svc` word ([`link`]) is left there too, by code interrupted after the
    // point's first word: that word is armed as well.
    let base = u64::from(b.code);
    let svc = |p: &LinkPoint| {
        let at = svc_slot(b, p);
        (word(base + at) != pic::SVC_LINK).then_some(at as u32)
    };
    let points = b.links.iter().filter(|p| p.offset != 0);
    let exits = points
        .clone()
        .map(|p| p.offset)
        .filter(|_| all)
        .chain(points.filter_map(svc))
        .chain((all && b.dispatch_exit != 0).then_some(b.dispatch_exit));
    for offset in exits {
        let at = u64::from(b.code) + u64::from(offset);
        // (Hot/cold: a split block's exit word and its trampoline both: code resumed in its whole code passes the first.)
        let t = link_at(b, u64::from(offset));
        for at in [at, t].into_iter().take(if t == at { 1 } else { 2 }) {
            c.armed_words[c.armed as usize] = [at as u32, word(at)];
            c.armed += 1;
            unsafe { ((area::CODE_WRITE_VA + at) as *mut u32).write_volatile(STOP_WORD) };
        }
    }
    synchronize_words(c);
    true
}
/// Restores the words [`arm`] patched (each must still hold [`STOP_WORD`]) and empties the record: at every resident
/// entry from generated code, before anything reads or rewrites code. Returns the restored code-area offsets (the
/// first `n`), or `None` (nothing restored) when the record is malformed.
pub fn disarm() -> Option<([u32; ARMED], usize)> {
    let c = control();
    let n = c.armed as usize;
    let mut at = [0; ARMED];
    if n == 0 {
        return Some((at, 0));
    }
    let words = c.armed_words.get(..n)?;
    if words.iter().any(|&[o, _]| {
        u64::from(o) >= c.code_bytes || o % 4 != 0 || word(u64::from(o)) != STOP_WORD
    }) {
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
    synchronize_offsets(c.armed_words[..c.armed as usize].iter().map(|&[o, _]| u64::from(o)));
}
/// Words written through the writable alias at code-area `offsets` (in any order; the iterator is walked twice) become visible
/// to instruction fetch: every line cleaned, one barrier, every line invalidated, a barrier and `isb`. The one place a batch of
/// word writes is synchronized (arming and disarming a stop, relinking); the CTR_EL0 shortcuts are [`clean_pou`],
/// [`clean_barrier`] and [`invalidate_pou`].
fn synchronize_offsets(offsets: impl Iterator<Item = u64> + Clone) {
    let ctr = control().ctr_el0;
    for o in offsets.clone() {
        clean_pou(ctr, area::CODE_WRITE_VA + o);
    }
    clean_barrier(ctr);
    for o in offsets {
        invalidate_pou(ctr, area::CODE_VA + o);
    }
    unsafe { asm!("dsb ish", "isb") };
}
/// The link point's words are its admitted form: the first word of its RIP move, and the `svc #LINK` after it (PIC study
/// stage 2: a link into a [`PIC`] block may patch that word instead, [`link`]).
fn unlinked(b: &Block, p: &LinkPoint) -> bool {
    let base = u64::from(b.code);
    word(link_at(b, u64::from(p.offset))) == admitted_at(b, p)
        && word(base + u64::from(p.offset)) == p.word
        && word(base + svc_slot(b, p)) == pic::SVC_LINK
}
/// PIC study stage 2: the native offset in block `b` of link point `p`'s `svc` word ([`pic::svc_slot`]).
pub fn svc_slot(b: &Block, p: &LinkPoint) -> u64 {
    pic::svc_slot(|o| word(u64::from(b.code) + o), p)
}

pub enum Link {
    /// This many link points now branch to the target.
    Linked(u64),
    /// `source` has no link point to the target, or one holds a word the cache did not put there.
    Bad,
}
/// M2 task 4, the second entry: whether `code` (a block with code) begins with the test for an open x87 run (`ldrb w0,
/// [x28, #X87_RUN]; cbnz w0, <the run's end, behind the body>`: fexc puts it there, and only there, in a unit of 32-bit code
/// that has no x87 instruction and an SSE instruction that needs the run closed; the host's code shape check pins the run's
/// end and that the block opens no run). Such a block opens no run, so behind its test none is open, to its every exit.
/// (The word after a conditional branch is a word the block reaches anyway: entering there needs no trust in the rest.)
fn run_tested(code: &Block) -> bool {
    code.code_size != 0
        && code.flags & (SYSTEM | STEP) == 0
        && natemu_abi::execution::run_test_end(|o| word(u64::from(code.code) + o), u64::from(code.code_size)).is_some()
}
/// Where a plain link from `source`'s code enters `code` (code-area offset): its body entry, or, from a block that leaves
/// with no x87 run open into a block that begins with the test for one, the word after the test's two ([`run_tested`]:
/// the test would find nothing). A property of the two blocks' words alone, so the same whenever the link is made, checked
/// or made again. Every other entry into a block runs its test: the dispatcher and L1, the resident's own entries, a link
/// through a dispatcher slot, a link from any other block.
fn link_entry(source: &Block, code: &Block) -> u64 {
    let skip = if run_tested(code_of(source)) && run_tested(code) { 8 } else { 0 };
    code.entry() - area::CODE_VA + skip
}
/// Links every link point of `source` to `target.rip` (in the same context) to `target`: its first word becomes `b`
/// to the target's entry, recorded with the original word in the segment of its kind ([`Control::links_tlb`]: local,
/// else by the target's [`GLOBAL`], which stays as it is while the target is current, that is while the link is there).
/// Link points already branching there stay, unless the record area is full: then links are dropped first.
pub fn link(source: &Block, target: &Block, at: u64) -> Link {
    let c = control();
    // (A shared source's link points name its translation's targets; `at` is the address it runs for.)
    let named = |p: &LinkPoint| {
        p.offset != 0 && pic::moved(p.target, source.rip, at, bits32(source.context)) == target.rip
    };
    let points = || source.links.iter().filter(move |p| named(p));
    if source.context != target.context || points().next().is_none() {
        return Link::Bad;
    }
    // PIC study stage 3, the link's form (section 3 of the plan): from a shared translation into an instance of a shared
    // one starting on the source's start frame, the move's first word becomes `b` to that translation's body (local:
    // right for every instance, the page base being the same); every other link out of shared code or into an instance
    // is checked: the move stays and the `svc` word becomes `b` to the source context's dispatcher slot, which probes L1 at
    // x0's own entry (the address the exit built: as current as any dispatcher hit, a miss is the dispatcher's; a slot
    // serves one context until a flush, missing while its context has no table). One fixed L1 entry would not do: L1 is
    // indexed by the address's low 16 bits and instances of one translation differ in bits 12-15, so it reaches only the
    // linking instance's entry (the plan's first form: every other instance missed into the resident, 727 M lookups
    // against 53 M on the slice). Absolute to absolute as before.
    let shared = source.flags & PIC != 0;
    let same_frames = |code: &Block, source: &Block| {
        code.source_gpa[0] == source.source_gpa[0]
            && (code.source_gpa[1] == u64::MAX || code.source_gpa[1] == source.source_gpa[1])
    };
    // (Under the check none: a local link runs its target's translation at the source's address with no instance there,
    // which the check would never compare; the slot's probe finds or makes one. Review of stage 3, finding 4c.)
    let local_ok = shared
        && (c.reuse != REUSE_CHECK || c.pic_check_local != 0)
        && c.pic_test != natemu_abi::execution::cache::PIC_TEST_NOLOCAL
        && target.rip >> 12 == at >> 12;
    // From shared code into an absolute template on its own page and frames (most links out of shared code: its
    // neighbours there have not needed a second address yet): the target is shared now (or its translation found) and
    // the link made local, so it never depends on an L1 entry. A template with no position-independent form stays
    // absolute (the link is checked).
    let now = (local_ok
        && target.flags & (INSTANCE | PIC | SYSTEM | STEP | DEAD | GLOBAL | UNSHAREABLE) == 0
        && target.flags & RELOCATABLE != 0
        && same_frames(target, source)
        && shares(target.context))
    .then(|| {
        template(target.rip, target.source_gpa[0], target.context)
            .filter(|t| t.flags & TRANSLATION != 0 && t.source_gpa == target.source_gpa)
            .or_else(|| share(target))
    })
    .flatten();
    let code = now.unwrap_or_else(|| code_of(target));
    let local_shared =
        local_ok && (target.flags & INSTANCE != 0 || now.is_some()) && same_frames(code, source);
    let checked = !local_shared && (shared || target.flags & INSTANCE != 0);
    // (Review of stage 3, finding 2: a REP string's self-exit is never checked-linked; a stop or interrupt at its `svc`
    // word would not see the REP boundary, which the resident finds by the link point's offset.)
    if checked && points().any(|p| source.rep_exit(u64::from(p.offset))) {
        return Link::Linked(0);
    }
    let entry = if checked {
        match dispatcher_for(source.context) {
            d if d == area::DISPATCH_MISS as u64 => return Link::Linked(0), // (no slot: no link)
            d => d,
        }
    } else {
        link_entry(source, code)
    };
    // (The record keeps the native offset; the word a plain link patches is [`link_at`]'s.)
    let offset_of = |p: &LinkPoint| if checked { svc_slot(source, p) } else { u64::from(p.offset) };
    let word_at = |p: &LinkPoint| {
        if checked {
            u64::from(source.code) + offset_of(p)
        } else {
            link_at(source, offset_of(p))
        }
    };
    // Indirect plan step 1: a checked link of shared code goes to its point's appended words (a branch of its own, whose
    // `bl` names the slot's lookup form, written below); any other checked link to the slot.
    let point_entry = |p: &LinkPoint| match source.links.iter().position(|q| q.offset == p.offset).and_then(|k| source.tail(Some(k))) {
        Some(t) if checked => t,
        _ => entry,
    };
    for p in points() {
        if !unlinked(source, p) && !links_to(word_at(p), point_entry(p)) {
            return Link::Bad;
        }
    }
    let cap = area::link_cap(c.link_cap);
    if area::links_full(c.links as usize, cap) {
        // Full (perf chunk 11): any link may go, its point then enters the linker again. Local links outlive their
        // targets' currency, so those into blocks not current go first (chunk 8); if that leaves less than a quarter
        // free, every link goes. Each such pass is followed by at least a quarter of the area's links: amortised O(1)
        // per link (before, a pass that freed nothing was repeated at every linker entry).
        c.reclaims += 1;
        // Suspended records first (relink plan): their words are restored already, so dropping them writes no code.
        let mut n = drop_suspended();
        if control().links as usize > area::reclaim_floor(cap) {
            let Some(local) = prune(LOCAL, true) else {
                return Link::Bad;
            };
            n += local;
            if control().links as usize > area::reclaim_floor(cap) {
                let Some(all) = unlink_all() else {
                    return Link::Bad;
                };
                n += all;
            }
        }
        control().reclaimed += n;
    }
    let (segment, inbound) = if checked {
        (CHECKED, 0)
    } else if local_shared || local(target, source) {
        (LOCAL, LOCAL_INBOUND)
    } else if target.flags & GLOBAL != 0 {
        (TLB, INBOUND)
    } else {
        (CR3, INBOUND)
    };
    // PIC study (`pic-count` builds only): the link's class, and the link enters the target through a counting stub
    // ([`stub`]).
    #[cfg(feature = "pic-count")]
    let class = pic::class(shared, at, code.flags & PIC != 0, target.rip);
    // an absolute link that is not local counts with the cross-page ones (every link a flush undoes).
    #[cfg(feature = "pic-count")]
    let class = if class == pic::Class::Absolute && segment != LOCAL { pic::Class::CrossPage } else { class };
    #[cfg(feature = "pic-count")]
    let to = if points().any(|p| unlinked(source, p)) {
        stub(entry, class)
    } else {
        entry
    };
    #[cfg(not(feature = "pic-count"))]
    let to = entry;
    let mut linked = 0;
    for (k, p) in source.links.iter().enumerate().filter(|&(_, p)| named(p) && unlinked(source, p)) {
        let at = word_at(p);
        let original = word(at);
        let w = match source.tail(Some(k)) {
            Some(t) if checked => {
                // (Its `bl` for the slot now: at placement the context may have had none.)
                let first = tail_words(t, entry, source.tail_calls(Some(k)))[0];
                if word(t) != first {
                    rewrite(t, first);
                }
                counted(count::TAIL_LINKS);
                // Always `b`, never `link_word`'s `nop`: a point's appended entry may follow its `svc` word directly (the
                // code's last word), and `unlink_word`, `svc_slot` and the host's audit read a checked link as a `b`.
                branch(at, t)
            }
            _ => link_word(at, to),
        };
        rewrite(at, w);
        insert(
            LinkRecord {
                source: source.number,
                offset_point: LinkRecord::pack(offset_of(p) as u32, k),
                original,
                target: if checked { area::CHECKED } else { code.number },
                prev_in: 0,
                next_in: 0,
            },
            segment,
        );
        if inbound != 0 {
            unsafe { (*block_ptr(code.number as usize)).flags |= inbound };
        }
        linked += 1;
    }
    if linked != 0 && checked {
        counted(count::CHECKED_LINKS);
    }
    // (Every build: the forms' counts the tests need, local and absolute into shared.)
    #[cfg(not(feature = "pic-count"))]
    if linked != 0 && (local_shared || checked && !shared) {
        counted(
            count::LINKS
                + if local_shared {
                    pic::Class::SamePage
                } else {
                    pic::Class::AbsoluteToShared
                } as usize,
        );
    }
    // (Study, `pic-count` builds: links by class, and a shared source's into an absolute block on its own page and
    // frame, which sharing the target at link time would make local.)
    #[cfg(feature = "pic-count")]
    if linked != 0 {
        counted(count::LINKS + class as usize);
        if shared
            && code.flags & PIC == 0
            && target.rip >> 12 == at >> 12
            && same_frames(code, source)
        {
            counted(count::SHAREABLE_TARGETS);
        }
    }
    Link::Linked(linked)
}
/// The destination of the `b` at code-area offset `at`, through a counting stub's (`pic-count` builds, [`stub`]).
fn destination(at: u64) -> u64 {
    let to = branch_to(at);
    #[cfg(feature = "pic-count")]
    if to >= area::HEAD_BYTES as u64 && stub_placed(to) && stub_load(word(to)) {
        return branch_to(to + 12);
    }
    to
}
/// The destination of the `b` at code-area offset `at`.
fn branch_to(at: u64) -> u64 {
    at.wrapping_add((((word(at) << 6) as i32 >> 6) as i64 * 4) as u64)
}
/// The word at `at` is a link into `entry`: `b` to it, or (`pic-count` builds) to a counting stub ending in one.
fn links_to(at: u64, entry: u64) -> bool {
    if word(at) == link_word(at, entry) {
        return true;
    }
    #[cfg(feature = "pic-count")]
    {
        let s = branch_to(at);
        if word(at) & 0xfc00_0000 == 0x1400_0000
            && stub_placed(s)
            && s >= area::HEAD_BYTES as u64
            && stub_load(word(s))
            && word(s + 12) == branch(s + 12, entry)
        {
            return true;
        }
    }
    false
}
/// PIC study (`pic-count` builds): the live block a counting stub holding native address `pc` enters ([`stub`]): `pc`
/// in no block, the stub's first word a counter load and its last a `b` to a live block's entry.
#[cfg(feature = "pic-count")]
pub fn stub_target(pc: u64) -> Option<Option<&'static Block>> {
    if block_at(pc).is_some() {
        return None;
    }
    let s = pc.checked_sub(area::CODE_VA)? & !15;
    if s < area::HEAD_BYTES as u64 || !stub_placed(s) || !stub_load(word(s)) {
        return None;
    }
    let to = branch_to(s + 12);
    // (A checked link's stub enters a dispatcher slot: the address is x0's.)
    if (area::DISPATCH as u64..area::HEAD_BYTES as u64).contains(&to) {
        return Some(None);
    }
    let b = block_at(area::CODE_VA + to)?;
    (b.flags & DEAD == 0 && [b.entry(), b.full_entry()].contains(&(area::CODE_VA + to)))
        .then_some(Some(b))
}
/// PIC study (`pic-count` builds): a stub at the code area's fill point counting the transitions of class `class` (but
/// [`pic::Class::Absolute`]) in a guest-state field no generated code uses (FEX's header, deferred-signal count, PF/AF
/// pair and call-return stack pointer; read by the host after the run), then entering `entry`: `ldr x1, [x28, #c]; add x1,
/// x1, #1; str x1, [x28, #c]; b entry` (x1 is dead at every exit: no static register, and the dispatcher writes it). The
/// code area holds it until the next flush; full: `entry` itself.
#[cfg(feature = "pic-count")]
fn stub(entry: u64, class: pic::Class) -> u64 {
    let field = match class {
        pic::Class::Absolute => return entry,
        pic::Class::SamePage => 0x0u32,
        pic::Class::CrossPage => 0x8,
        pic::Class::SharedToAbsolute => 0x10,
        pic::Class::AbsoluteToShared => 0xb0,
    };
    counting(entry, field, true)
}
/// PIC study (`pic-count` builds): the stub before the dispatcher slot (or miss) `to` that counts computed exits, in 32
/// bits (wrapping) in FEX's flag bytes 28-31, which no code uses (between the NZCV word and the x87 flags). One per
/// dispatcher entry, made once and kept ([`Control::computed_stubs`]): taken from the code area's top (blocks are placed
/// below it, so a block placed after can never overwrite it; the area is smaller by 16 bytes per stub).
#[cfg(feature = "pic-count")]
fn computed(to: u64) -> u64 {
    let c = control();
    let Some(k) = to
        .checked_sub(area::DISPATCH as u64)
        .map(|o| o as usize / 16)
        .filter(|&k| k < c.computed_stubs.len())
    else {
        return to;
    };
    if c.computed_stubs[k] == 0 {
        let at = (c.code_bytes - 16) & !15;
        // (The block being placed lies above the fill point, below its end: a margin no block reaches.)
        if at < c.code_used + (1 << 20) {
            return to;
        }
        let used = c.code_used;
        c.code_used = at;
        let s = counting(to, natemu_abi::execution::frame::FLAGS + 28, false);
        (c.code_used, c.code_bytes) = (used, at);
        if s == to {
            return to;
        }
        c.computed_stubs[k] = s as u32;
    }
    u64::from(c.computed_stubs[k])
}
/// A counting stub may be at code-area offset `s`: below the fill point (a link's), or above the area's end (a computed
/// exit's, [`computed`]).
#[cfg(feature = "pic-count")]
fn stub_placed(s: u64) -> bool {
    let c = control();
    s + 16 <= c.code_used || s >= c.code_bytes
}
/// The first word of a counting stub: `ldr x1` or `ldr w1, [x28, #c]`.
#[cfg(feature = "pic-count")]
fn stub_load(w: u32) -> bool {
    w & 0xbfc0_03ff == 0xb940_0381
}
#[cfg(feature = "pic-count")]
fn counting(entry: u64, field: u32, wide: bool) -> u64 {
    let c = control();
    let at = c.code_used.next_multiple_of(16);
    if at + 16 > c.code_bytes {
        return entry;
    }
    let (ldr, add, str, scale) = if wide {
        (0xf940_0000, 0x9100_0421, 0xf900_0000, 8)
    } else {
        (0xb940_0000, 0x1100_0421, 0xb900_0000, 4)
    };
    let imm = field / scale << 10 | 28 << 5 | 1;
    let words = [ldr | imm, add, str | imm, branch(at + 12, entry)];
    unsafe {
        copy_record(
            (area::CODE_WRITE_VA + at) as *mut u8,
            words.as_ptr().cast(),
            16,
        )
    };
    synchronize_code(area::CODE_WRITE_VA + at, area::CODE_VA + at, 16);
    c.code_used = at + 16;
    at
}
/// Perf chunk 8: a link from `source` into `target` is local when every linear page of `target` is a page of `source`
/// translated from the same guest-physical page. It may then stay while `target` is not current: `source` runs only
/// under a translation of its pages that is valid now (it was entered current, or by a local link from a block that
/// was), and `target` reads only those pages, through the same frames, in the same context (CPL and gating).
fn local(target: &Block, source: &Block) -> bool {
    let pages = |b: &Block| {
        [
            (b.rip >> 12, b.source_gpa[0]),
            ((b.rip >> 12).wrapping_add(1), b.source_gpa[1]),
        ]
    };
    pages(target)
        .iter()
        .all(|&(p, g)| g == u64::MAX || pages(source).contains(&(p, g)))
}
/// The link records' segments ([`Control::links_susp`], `links_cr3`, `links_tlb`, `links_local`, `links`), by which events look
/// at them. [`SUSP`]: links a TLB event undid, records kept (their words are restored: only a death, a reclaim, a cut of the
/// target or the flush look at them, and [`relink`] when the target is current again). First, so a suspension moves no record.
const SUSP: usize = 0;
const CR3: usize = 1;
const TLB: usize = 2;
const LOCAL: usize = 3;
const CHECKED: usize = 4;
fn ends(c: &mut Control) -> [&mut u64; 5] {
    [
        &mut c.links_susp,
        &mut c.links_cr3,
        &mut c.links_tlb,
        &mut c.links_local,
        &mut c.links,
    ]
}
/// The inbound lists ([`LinkRecord::next_in`]): record `i` is named `i + 1` (0: none), so a list survives records moving.
/// A checked link is on none (its `target` is not a block).
fn listed(r: &LinkRecord) -> bool {
    r.target & area::CHECKED == 0
}
fn inbound_head(n: u32) -> *mut u32 {
    unsafe { &raw mut (*block_ptr(n as usize)).inbound }
}
/// Puts record `i` (already in place) at the front of its target's list.
fn list_add(i: usize) {
    unsafe {
        let r = record(i);
        let h = inbound_head((*r).target);
        ((*r).prev_in, (*r).next_in) = (0, *h);
        if *h != 0 {
            (*record(*h as usize - 1)).prev_in = i as u32 + 1;
        }
        *h = i as u32 + 1;
    }
}
/// Takes record `i` out of its target's list.
fn list_del(i: usize) {
    unsafe {
        let r = record(i).read();
        if !listed(&r) {
            return;
        }
        if r.prev_in != 0 {
            (*record(r.prev_in as usize - 1)).next_in = r.next_in;
        } else {
            *inbound_head(r.target) = r.next_in;
        }
        if r.next_in != 0 {
            (*record(r.next_in as usize - 1)).prev_in = r.prev_in;
        }
    }
}
/// The slot of block `source`'s [`Block::out`] for the link point of record `r`.
fn out_slot(r: &LinkRecord) -> *mut u32 {
    unsafe { &raw mut (*block_ptr(r.source as usize)).out[r.point()] }
}
/// `link-check` builds: halts `stop::LINK_CHECK` (with the line, the slot's value and the value it should hold) when an
/// outbound slot does not name the record it is said to; other builds leave it to the writes.
#[cfg(feature = "link-check")]
#[track_caller]
fn out_check(slot: *mut u32, named: u32) {
    let value = unsafe { *slot };
    if value != named {
        let line = u64::from(core::panic::Location::caller().line());
        crate::execution::halt_native([line, u64::from(value), u64::from(named)], natemu_abi::execution::stop::LINK_CHECK)
    }
}
/// Names record `i` (already in place) in its source's slot for its link point.
fn out_add(i: usize) {
    let slot = out_slot(unsafe { &*record(i) });
    #[cfg(feature = "link-check")]
    out_check(slot, 0);
    unsafe { *slot = i as u32 + 1 };
}
/// Clears record `i`'s slot in its source's [`Block::out`].
fn out_del(i: usize) {
    let slot = out_slot(unsafe { &*record(i) });
    #[cfg(feature = "link-check")]
    out_check(slot, i as u32 + 1);
    unsafe { *slot = 0 };
}
/// The record now at `to` was at `from` (another record's old place): its neighbours (or its target's head) name it
/// again, and so does its source's outbound slot (the one write that keeps [`Block::out`] right). A record that does not
/// move (`from == to`: a removed or stale one) is left alone.
fn list_moved(from: u64, to: u64) {
    if from == to {
        return;
    }
    // (Relink plan: a suspended record is named by no slot: its point's slot is empty or names a live record, which is another one.)
    let slot = out_slot(unsafe { &*record(to as usize) });
    if unsafe { *slot } == from as u32 + 1 {
        unsafe { *slot = to as u32 + 1 };
    }
    unsafe {
        let r = record(to as usize).read();
        if !listed(&r) {
            return;
        }
        let to = to as u32 + 1;
        if r.prev_in != 0 {
            (*record(r.prev_in as usize - 1)).next_in = to;
        } else {
            *inbound_head(r.target) = to;
        }
        if r.next_in != 0 {
            (*record(r.next_in as usize - 1)).prev_in = to;
        }
    }
}
/// The segment holding record `i`.
fn segment_of(i: u64) -> usize {
    let c = control();
    (SUSP..CHECKED).find(|&k| i < *ends(c)[k]).unwrap_or(CHECKED)
}
/// Appends `r` to segment `k`: each later segment's first record moves to that segment's end.
fn insert(r: LinkRecord, k: usize) {
    let mut ends = ends(control());
    let mut hole = *ends[CHECKED];
    for j in (k + 1..=CHECKED).rev() {
        let first = *ends[j - 1];
        unsafe { record(hole as usize).write(record(first as usize).read()) };
        list_moved(first, hole);
        hole = first;
    }
    unsafe { record(hole as usize).write(r) };
    out_add(hole as usize);
    if listed(&r) {
        list_add(hole as usize);
    }
    for e in &mut ends[k..] {
        **e += 1;
    }
}
/// Drops record `i` of segment `k`: its segment's last record fills it, then each later segment's last record fills the
/// slot so freed. `follow` (a record's index + 1, or 0) is kept pointing at its record if that moves.
fn remove(i: u64, k: usize, follow: &mut u32) {
    list_del(i as usize);
    if k != SUSP {
        out_del(i as usize);
    }
    let ends = ends(control());
    let mut hole = i;
    for e in ends.into_iter().skip(k) {
        *e -= 1;
        unsafe { record(hole as usize).write(record(*e as usize).read()) };
        list_moved(*e, hole);
        if hole != *e && *follow == *e as u32 + 1 {
            *follow = hole as u32 + 1;
        }
        hole = *e;
    }
}
/// Undoes the links on block `n`'s inbound list and drops their records: all of them, or (`all` false) only the
/// non-local ones (records before [`Control::links_tlb`]), which a block that stops being current loses (a local link may
/// stay: [`local`]). Returns the links
/// undone; `None` if a record does not describe its word.
fn unlink_inbound(n: usize, all: bool) -> Option<u64> {
    let mut undone = 0;
    let mut at = block(n).inbound;
    while at != 0 {
        let i = at as u64 - 1;
        let r = unsafe { record(i as usize).read() };
        at = r.next_in;
        if all || i < control().links_tlb {
            // (A suspended record's word was restored when it was suspended.)
            if i >= control().links_susp {
                unlink(r)?;
                undone += 1;
            }
            remove(i, segment_of(i), &mut at);
        }
    }
    Some(undone)
}
/// Undoes the links out of block `n` (dead) by its [`Block::out`] slots and drops their records. Returns the links undone;
/// `None` if a record does not describe its word. (Dropping a record moves others, which updates their slots, `n`'s own
/// included: each slot is read when its turn comes.)
fn unlink_outbound(n: usize) -> Option<u64> {
    let mut undone = 0;
    for k in 0..MAX_LINK_POINTS {
        let Some(i) = (block(n).out[k] as u64).checked_sub(1) else {
            continue;
        };
        // (Relink plan: no suspended record is named by a slot: `link-check` halts if one is; other builds skip the restore.)
        #[cfg(feature = "link-check")]
        if i < control().links_susp {
            crate::execution::halt_native([n as u64, k as u64, i], natemu_abi::execution::stop::LINK_CHECK)
        }
        if i >= control().links_susp {
            unlink(unsafe { record(i as usize).read() })?;
            undone += 1;
        }
        remove(i, segment_of(i), &mut 0);
    }
    Some(undone)
}
/// Restores every linked point's original word and drops every record (the invalidation of all blocks at a flush).
/// `None` if a record does not describe its word (it is not left as is).
pub fn unlink_all() -> Option<u64> {
    let c = control();
    for i in 0..c.links as usize {
        let r = unsafe { record(i).read() };
        // (Suspended records' words are restored already.)
        if i as u64 >= c.links_susp {
            unlink(r)?;
        }
        if listed(&r) {
            unsafe { *inbound_head(r.target) = 0 };
        }
        unsafe { *out_slot(&r) = 0 };
    }
    let n = c.links - c.links_susp;
    (c.links, c.links_susp, c.links_cr3, c.links_tlb, c.links_local) = (0, 0, 0, 0, 0);
    Some(n)
}
/// Restores the original word of the link point `r` describes, if it holds the branch `r` says, and makes it visible.
fn unlink(r: LinkRecord) -> Option<()> {
    let at = unlink_word(r)?;
    synchronize_offsets(core::iter::once(at));
    Some(())
}
/// [`unlink`]'s write alone: the word is restored but not yet visible to instruction fetch (the caller synchronizes the
/// code-area offset returned, [`synchronize_offsets`]). `None` (nothing written) when the word is not the branch `r` says.
/// (PIC study stage 2: a record of an `svc`-word link, its original word `svc #LINK`, names the target's full entry.)
fn unlink_word(r: LinkRecord) -> Option<u64> {
    let source = block(r.source as usize);
    let at = if r.target & area::CHECKED != 0 {
        u64::from(source.code) + u64::from(r.offset())
    } else {
        link_at(source, u64::from(r.offset()))
    };
    let restore = |at: u64| {
        unsafe { ((area::CODE_WRITE_VA + at) as *mut u32).write_volatile(r.original) };
        at
    };
    // (A checked link's word is `b` to a dispatcher slot, [`area::CHECKED`], or, from shared code, to its point's appended
    // words (indirect plan step 1).)
    if r.target & area::CHECKED != 0 {
        let slots = (area::DISPATCH + area::DISPATCH_SLOT_BYTES) as u64
            ..(area::DISPATCH + area::DISPATCH_BYTES) as u64;
        let to = destination(at);
        let into = word(at) & 0xfc00_0000 == 0x1400_0000
            && (slots.contains(&to) && to % area::DISPATCH_SLOT_BYTES as u64 == 0 || source.tail(Some(r.point())) == Some(to));
        return (into && r.original == pic::SVC_LINK).then(|| restore(at));
    }
    let entry = link_entry(source, block(r.target as usize));
    links_to(at, entry).then(|| restore(at))
}

/// This vCPU is about to write `gpa`'s page, which holds translations: clears every vCPU's code bit of the page at
/// once, posts the page to each other vCPU that had one (page bit, then `PAGES`, then a kick: it drops its blocks at
/// its next block entry) and invalidates this vCPU's own blocks from it ([`invalidate`]). A write through the RAM
/// alias is followed by [`rechecked`].
pub fn written(gpa: u64) -> Option<u64> {
    if let Some((m, shift)) = code_page(gpa) {
        post(
            peers(
                m.fetch_and(!(u64::from(CpuMask::MAX) << shift), SeqCst),
                shift,
            ) & !own(),
            gpa,
        );
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
            post(
                peers(m.fetch_and(!(u64::from(others) << shift), SeqCst), shift) & others,
                gpa,
            );
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
    for r in [
        b.rip >> 21,
        b.rip.wrapping_add(u64::from(b.guest_bytes).max(1) - 1) >> 21,
    ] {
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
    for r in [
        b.rip >> 21,
        b.rip.wrapping_add(u64::from(b.guest_bytes).max(1) - 1) >> 21,
    ] {
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
        && (c.large_count as usize > area::LARGE_REGIONS
            || c.large_list[..c.large_count as usize].contains(&r))
}
/// A guest INVLPG of `address`, or a #PF at it (K1; SDM Vol.3 4.10.4.1: the TLB entries for its page, global ones too,
/// and all of them for a page larger than 4 KiB): every current block of this vCPU with guest bytes on its 4 KiB page, and
/// every [`LARGE`] one with bytes in its 2 MiB region, is no longer current (`checked` 0, global or not): out of L1, every
/// link from another page into it undone; it stays in L2 and is found again only after a fetch walk ([`lookup`]). Other
/// blocks keep their translations (a 4 KiB TLB entry of another page is not one for this page). Its code-page bits
/// stay: they only say it may hold translations. Blocks are looked for only when the address's 2 MiB region may hold
/// one ([`Control::regions`]; for large blocks [`large_held`]), then on the linear map's chains of the pages
/// ([`area::LINEAR_HEADS`]), which the walks leave holding current blocks only ([`on_linear_page`]): a block not current is
/// already unreachable without a fetch walk.
pub fn invlpg(address: u64) -> Option<u64> {
    // Page-currency plan: the record of fetch-walk results loses the entries of the page and of its 2 MiB region, both classes.
    crate::execution::page_record::invalidated(address);
    let c = control();
    let r = address >> 21;
    let held = |regions: &[u64; 16]| {
        let (w, bit) = region(r);
        regions[w] & bit != 0
    };
    let mut undone = Some(0);
    // (Each block cut gives up its own non-local inbound links: the lists replace the scan of every record.)
    let mut cut = |n: usize| {
        if cut_one(n, false).is_some() {
            undone = undone.zip(unlink_inbound(n, false)).map(|(a, b)| a + b);
        }
        // (Only while every record so far described its word: a broken one halts with its own stop, `BAD_LINK`.)
        #[cfg(feature = "link-check")]
        if undone.is_some() {
            check_inbound(n, false);
        }
    };
    if held(&c.regions) {
        on_linear_page(address >> 12, &mut cut);
    }
    if large_held(c, r) {
        for page in r << 9..(r + 1) << 9 {
            on_linear_page(page, |n| {
                if block(n).flags & LARGE != 0 {
                    cut(n)
                }
            });
        }
    }
    #[cfg(feature = "link-check")]
    check_chains();
    undone
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
/// Kills (`dead`) the blocks numbered `doomed`, or makes them no longer current: each leaves L1 and undoes the links on
/// its inbound list ([`unlink_inbound`]: every link into a dead block, every link from another page into one no longer
/// current; links out of a block only not current stay: they run only once it is entered, which needs a fetch walk
/// first, and so do local links into it). A dead block's links out are undone by its own [`Block::out`] slots
/// ([`unlink_outbound`]: at most [`MAX_LINK_POINTS`] records, found with no pass over the area's records). Dead blocks leave
/// L2 (as [`lookup`] skips them). Returns the links undone; `None` if a record does not describe its word.
fn cut(doomed: impl Iterator<Item = usize>, dead: bool) -> Option<u64> {
    let mut undone = 0;
    for n in doomed {
        if cut_one(n, dead).is_some() {
            undone += unlink_inbound(n, dead)?;
        }
        // A dead block's own links out, from its outbound slots (cost: its own links, not the area's records).
        if dead && block(n).out != [0; MAX_LINK_POINTS] {
            undone += unlink_outbound(n)?;
        }
        #[cfg(feature = "link-check")]
        if dead {
            check_inbound(n, true);
        }
    }
    #[cfg(feature = "link-check")]
    if dead {
        check_no_dead_links();
    }
    Some(undone)
}
/// `link-check` builds, after a cut that killed blocks (a full scan of the records, as the pass over them that death once
/// made): no record names a dead block as its source or target, and every non-local record (before
/// [`Control::links_tlb`]) enters a block current now with [`INBOUND`] set (the premise of INVLPG's and a #PF's use of the
/// inbound lists, which the host checks at the end of a run). Halts `stop::LINK_CHECK` otherwise (the line, the record).
#[cfg(feature = "link-check")]
#[track_caller]
fn check_no_dead_links() {
    let line = u64::from(core::panic::Location::caller().line());
    let c = control();
    for i in 0..c.links as usize {
        let r = unsafe { record(i).read() };
        let dead = |n: u32| block(n as usize).flags & DEAD != 0;
        // (Relink plan: a suspended record's target is not current, and its source may be dead: its death leaves the record, named by
        // no outbound slot, for the target's next relink or a reclaim to drop.)
        let susp = (i as u64) < c.links_susp;
        let bad = dead(r.source) && !susp
            || r.target & area::CHECKED == 0
                && (dead(r.target)
                    || (i as u64) < c.links_tlb && {
                        let t = block(r.target as usize);
                        (!susp && !current(t)) || t.flags & INBOUND == 0
                    });
        if bad {
            crate::execution::halt_native([line, i as u64, u64::from(r.source)], natemu_abi::execution::stop::LINK_CHECK)
        }
    }
}
/// `link-check` builds: block `n`'s inbound list, after a cut (`dead`: after its death), holds no record before
/// [`Control::links_tlb`] (a dead block's: none at all). Halts `stop::LINK_CHECK` otherwise (or if the list does not
/// end within [`area::MAX_LINKS`] steps).
#[cfg(feature = "link-check")]
#[track_caller]
fn check_inbound(n: usize, dead: bool) {
    let mut at = block(n).inbound;
    for _ in 0..=area::MAX_LINKS {
        if at == 0 {
            return;
        }
        if dead || at as u64 - 1 < control().links_tlb {
            break;
        }
        at = unsafe { record(at as usize - 1).read() }.next_in;
    }
    let line = u64::from(core::panic::Location::caller().line());
    crate::execution::halt_native([line, u64::from(at), n as u64], natemu_abi::execution::stop::LINK_CHECK)
}
/// [`cut`]'s work on block `n` but the link records: `Some` if its inbound list may hold links to undo (links from other
/// pages may enter it, or it is dead and local links may); `None` also when it was dead already.
fn cut_one(n: usize, dead: bool) -> Option<()> {
    let c = control();
    let b = *block(n);
    if b.flags & DEAD != 0 || (!dead && b.checked == 0) {
        return None;
    }
    let scope = (b.flags & INSTANCE == 0 // (records name translations, never instances)
        && (b.flags & INBOUND != 0 || dead && b.flags & LOCAL_INBOUND != 0))
        .then_some(());
    unsafe { (*block_ptr(n)).flags &= !INBOUND };
    if dead {
        unsafe { (*block_ptr(n)).flags |= DEAD };
        c.killed += 1;
        l2_remove(n);
    } else {
        unsafe { (*block_ptr(n)).checked = 0 };
        c.unvalidated += 1;
    }
    // Both slots of the block's set, each keyed on its owner (a block is in at most one, but a duplicate would go too).
    let (w0, w1) = area::l1_slots_of(b.rip);
    for t in 0..area::L1_TABLES {
        for i in [w0, w1] {
            if l1_block(t, i) == b.number + 1 {
                empty_l1(t, i);
            }
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
        c.scanned += 1;
        let alive = block(r.source as usize).flags & DEAD == 0;
        let kept = if k == CHECKED {
            alive
        } else {
            let target = block(r.target as usize);
            // (A suspended record stays while both ends live: its target is not current, by design.)
            target.flags & DEAD == 0 && alive && (k == SUSP || (k == LOCAL && !strict) || current(target))
        };
        if kept {
            i += 1;
            continue;
        }
        // (A suspended record's word was restored when it was suspended: nothing to undo.)
        if k != SUSP {
            unlink(r)?;
            unlinked += 1;
        }
        remove(i, k, &mut 0);
    }
}
/// [`prune`] for a TLB event (`scope` [`CR3`] or [`TLB`], not strict): the same links undone, in one batch, and the records
/// compacted in one pass: the survivors of the scanned segments slide down over the dropped ones' places, then each later
/// segment's tail fills the gap, so a record moves at most once (`remove` moves up to one per later segment for every
/// record it drops, and a CR3 load drops nearly every record of its segment). The words' code synchronization is one
/// batch: the first pass restores every undone word and cleans its line, moving no record; one barrier; the second
/// invalidates each line as the records are compacted (a slot is written only after it was read), then the final barrier,
/// before the gap fill. Relink plan: only `pic-count` builds drop their records at a TLB event now (the others
/// [`suspend`] them: it keeps their records).
#[cfg(feature = "pic-count")]
fn prune_tlb(scope: usize) -> Option<u64> {
    let c = control();
    let ctr = c.ctr_el0;
    let old = ends(c).map(|e| *e);
    let kept = |r: &LinkRecord| {
        let target = block(r.target as usize);
        target.flags & DEAD == 0 && block(r.source as usize).flags & DEAD == 0 && current(target)
    };
    for i in 0..old[scope] {
        let r = unsafe { record(i as usize).read() };
        if !kept(&r) {
            clean_pou(ctr, area::CODE_WRITE_VA + unlink_word(r)?);
        }
    }
    clean_barrier(ctr);
    let (mut w, mut undone) = (0, 0);
    let mut new = [0; 5];
    for k in 0..=scope {
        for i in if k == 0 { 0 } else { old[k - 1] }..old[k] {
            let r = unsafe { record(i as usize).read() };
            if kept(&r) {
                if w != i {
                    unsafe { record(w as usize).write(r) };
                    list_moved(i, w);
                }
                w += 1;
            } else {
                // (Link records of these segments are never checked: the word is the source's code plus the offset.)
                invalidate_pou(ctr, area::CODE_VA + u64::from(block(r.source as usize).code) + u64::from(r.offset()));
                list_del(i as usize);
                out_del(i as usize);
                undone += 1;
            }
        }
        new[k] = w;
    }
    unsafe { asm!("dsb ish", "isb") };
    fill_gap(old, scope, w, &mut new);
    for (e, n) in ends(c).into_iter().zip(new) {
        *e = n;
    }
    Some(undone)
}
/// The segments up to `scope` were compacted to end at `w` (their new ends in `new`; `old` the ends before): each later
/// segment's tail fills the gap before it, and its new end is `new[j]`.
fn fill_gap(old: [u64; 5], scope: usize, w: u64, new: &mut [u64; 5]) {
    let gap = old[scope] - w;
    for j in scope + 1..5 {
        let (start, end) = (old[j - 1], old[j]);
        let m = gap.min(end - start);
        for t in 0..m {
            let (from, to) = (end - m + t, start - gap + t);
            unsafe { record(to as usize).write(record(from as usize).read()) };
            list_moved(from, to);
        }
        new[j] = old[j] - gap;
    }
}
/// A TLB event's links ([`CR3`] after a CR3 load, [`TLB`] too after a global flush): their words restored in one batch,
/// their records kept: the suspended segment ends where the scope does (a segment first in the area, so no record moves), and
/// the segments up to `scope` are empty. Relink plan section 3: after the epoch moved no record of the scanned segments names a
/// current block (a CR3 load: every target there is non-global and was current only in the old epoch; a global flush: no
/// block is current), and no record names a dead block or source (their deaths dropped them). Returns the links undone.
#[cfg(not(feature = "pic-count"))]
fn suspend(scope: usize) -> Option<u64> {
    let c = control();
    let ctr = c.ctr_el0;
    let (from, to) = (c.links_susp, *ends(c)[scope]);
    for i in from..to {
        let r = unsafe { record(i as usize).read() };
        // (A suspended record is named by no outbound slot: its source's point may be linked to another block meanwhile.)
        out_del(i as usize);
        // (`link-check`: the epoch moved, so no target of the scanned segments is current: the premise of suspending without looking.)
        #[cfg(feature = "link-check")]
        if current(block(r.target as usize)) {
            crate::execution::halt_native([i, u64::from(r.target), u64::from(r.source)], natemu_abi::execution::stop::LINK_CHECK)
        }
        clean_pou(ctr, area::CODE_WRITE_VA + unlink_word(r)?);
    }
    clean_barrier(ctr);
    for i in from..to {
        let r = unsafe { record(i as usize).read() };
        // (Link records of these segments are never checked: the word is the source's code plus the offset.)
        invalidate_pou(ctr, area::CODE_VA + link_at(block(r.source as usize), u64::from(r.offset())));
    }
    unsafe { asm!("dsb ish", "isb") };
    c.links_susp = to;
    if scope == TLB {
        c.links_cr3 = to;
    }
    c.suspended += to - from;
    Some(to - from)
}
/// Drops every suspended record (no code is written: their words are restored): out of their targets' lists, then the later
/// segments' tails fill the gap. Returns the records dropped.
fn drop_suspended() -> u64 {
    let c = control();
    let n = c.links_susp;
    for i in 0..n {
        list_del(i as usize);
    }
    let old = ends(c).map(|e| *e);
    let mut new = [0; 5];
    fill_gap(old, SUSP, 0, &mut new);
    for (e, v) in ends(c).into_iter().zip(new) {
        *e = v;
    }
    n
}
/// Relink plan section 3: block `n` was just revalidated (its `checked` and flags are written, its source pages' walk is done):
/// every suspended record on its inbound list whose source is alive and whose word is still in its admitted form is patched
/// again, `b` to `n`'s entry, as [`link`] would write it now (the pairing was proved when the record was made; `n` is current,
/// by this very revalidation), and moved out of the suspended segment (to the CR3 segment, or the TLB segment when the walk
/// found `n` global), with one code synchronization for the batch. A record whose source is dead, or whose word holds anything
/// else (the point was linked elsewhere meanwhile), is dropped. The walk keeps a cursor on the next record (`remove` and
/// [`resume`] keep it right as records move).
#[cfg(not(feature = "pic-count"))]
fn relink(n: usize) {
    let p = crate::perf::enter(crate::perf::RELINK);
    if control().armed != 0 {
        // (Every resident entry disarms first: a stop word could be overwritten here.)
        crate::execution::halt_native([n as u64, control().armed, 0], natemu_abi::execution::stop::BAD_LINK)
    }
    let global = block(n).flags & GLOBAL != 0;
    let mut batch = [0u32; 64];
    let mut held = 0;
    let mut at = block(n).inbound;
    while at != 0 {
        let i = u64::from(at) - 1;
        let r = unsafe { record(i as usize).read() };
        at = r.next_in;
        if r.target != n as u32 {
            // (A cursor a record move left stale would walk another block's list: halt rather than patch its sources.)
            crate::execution::halt_native([n as u64, i, u64::from(r.target)], natemu_abi::execution::stop::BAD_LINK)
        }
        if i >= control().links_susp {
            continue; // a live record of the block (the list holds them all)
        }
        let source = block(r.source as usize);
        let word_at = link_at(source, u64::from(r.offset()));
        let point = &source.links[r.point()];
        // (The record proves the pairing, as `link` required it: the point is the record's and names the target's RIP; else the
        // record is wrong, not stale.)
        if point.offset != r.offset() || point.target != block(n).rip {
            crate::execution::halt_native([n as u64, i, u64::from(r.source)], natemu_abi::execution::stop::BAD_LINK)
        }
        if source.flags & DEAD != 0 || !unlinked(source, point) {
            remove(i, SUSP, &mut at);
            control().relink_dropped += 1;
            continue;
        }
        unsafe { ((area::CODE_WRITE_VA + word_at) as *mut u32).write_volatile(link_word(word_at, link_entry(source, block(n)))) };
        batch[held] = word_at as u32;
        held += 1;
        if held == batch.len() {
            synchronize_offsets(batch[..held].iter().map(|&o| u64::from(o)));
            held = 0;
        }
        resume(i, r, global, &mut at);
        control().relinked += 1;
    }
    if held != 0 {
        synchronize_offsets(batch[..held].iter().map(|&o| u64::from(o)));
    }
    unsafe { (*block_ptr(n)).flags |= INBOUND };
    crate::perf::leave(p);
}
/// [`relink`]'s record move: the suspended record `r` at slot `i` (its word patched) goes to the front of its target's list in
/// the CR3 segment (the TLB segment if `global`), by boundary swaps, not [`insert`] (which moves each later segment's first record
/// to its end without fixing a cursor). The last suspended record fills slot `i`; the suspended segment shrinks by that slot,
/// which is now the CR3 segment's first; for a TLB destination the CR3 segment's last record fills that one in turn and the
/// slot it leaves is the TLB segment's first. `follow` (the walk's cursor: a record's index + 1) is kept pointing at its record.
#[cfg(not(feature = "pic-count"))]
fn resume(i: u64, r: LinkRecord, global: bool, follow: &mut u32) {
    list_del(i as usize);
    let c = control();
    let last = c.links_susp - 1;
    if last != i {
        unsafe { record(i as usize).write(record(last as usize).read()) };
        list_moved(last, i);
        if *follow == last as u32 + 1 {
            *follow = i as u32 + 1;
        }
    }
    c.links_susp = last;
    let mut slot = last;
    if global {
        let end = c.links_cr3 - 1;
        if end != last {
            unsafe { record(last as usize).write(record(end as usize).read()) };
            list_moved(end, last);
            if *follow == end as u32 + 1 {
                *follow = last as u32 + 1;
            }
        }
        c.links_cr3 = end;
        slot = end;
    }
    unsafe {
        record(slot as usize).write(LinkRecord {
            prev_in: 0,
            next_in: 0,
            ..r
        })
    };
    list_add(slot as usize);
    out_add(slot as usize);
}
/// Self-check: every link point of every placed block holds its admitted word (after [`unlink_all`]).
pub fn links_restored() -> bool {
    (0..control().blocks as usize).all(|n| {
        let b = block(n);
        b.flags & INSTANCE != 0
            || b.links
                .iter()
                .filter(|p| p.offset != 0)
                .all(|p| unlinked(b, p))
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
    let holds = |b: &Block| {
        u64::from(b.start()) <= offset && offset < u64::from(b.code) + u64::from(b.code_size)
            || in_hot(b, offset)
    };
    // Perf chunk 12: the block found at this word last ([`Control::pc_hints`]).
    let hint = (offset >> 2) as usize % area::PC_HINTS;
    let n = control().pc_hints[hint] as usize;
    if n < count && holds(block(n)) {
        return Some(block(n));
    }
    // The last record whose code starts at or below `offset`, or at or below `offset` + 4 (a [`PIC`] block's first word
    // is 4 bytes before its code): placement order is address order, and a record with no code of its own (a kept record,
    // an instance; any number back to back) has the fill point of its making, so it never lies between a block holding
    // `offset` and the last record at or below `offset`, nor after the block starting at `offset` + 4 among those at or
    // below it.
    let last = |x: u64| {
        let (mut lo, mut hi) = (0, count);
        while lo < hi {
            let mid = (lo + hi) / 2;
            if u64::from(block(mid).code) <= x {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        lo.checked_sub(1)
    };
    // Hot/cold: else the last record whose hot copy starts at or below (hot addresses are in placement order too; a record
    // not split has the hot fill point of its making, never below a split block's before it nor at or below a PC in it).
    let last_hot = |x: u64| {
        let (mut lo, mut hi) = (0, count);
        while lo < hi {
            let mid = (lo + hi) / 2;
            if u64::from(block(mid).hot) <= x {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        lo.checked_sub(1)
    };
    let n = [last(offset), last(offset + 4)]
        .into_iter()
        .chain([last_hot(offset.wrapping_sub(4))])
        .flatten()
        .find(|&n| holds(block(n)))?;
    let b = block(n);
    control().pc_hints[hint] = n as u32;
    Some(b)
}

/// Indirect plan step 1: the block, dead ones too, whose appended words ([`Block::tail`]) hold `pc`, with the exit they serve
/// (`None` the computed exit, `Some(k)` link point k) and the word's index in them. They follow its code, before the next
/// record's: placement order is address order, and a record with no code has the fill point of its making, past them.
pub fn tail_at(pc: u64) -> Option<(&'static Block, Option<usize>, u32)> {
    let offset = pc.checked_sub(area::CODE_VA).filter(|&o| o >= area::HEAD_BYTES as u64)?;
    let count = control().blocks as usize;
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
    b.tail_word(offset).map(|(p, w)| (b, p, w))
}

/// Indirect plan step 1 test ([`area::PIC_TEST_TAIL`]): the code-area offset of one exit's appended words of a block of
/// `context` among the newest records, a dead block's when one has them (`pick` chooses among its entries).
pub fn tail_for_test(context: u64, pick: u64) -> Option<u64> {
    let count = control().blocks as usize;
    let mut live = None;
    for n in (count.saturating_sub(1 << 14)..count).rev() {
        let b = block(n);
        if b.context != context || b.tail_entries() == 0 {
            continue;
        }
        let entries: [Option<u64>; MAX_LINK_POINTS + 1] =
            core::array::from_fn(|i| b.tail(if i == 0 { None } else { Some(i - 1) }));
        let at = entries.iter().flatten().cycle().nth(pick as usize % b.tail_entries() as usize).copied();
        if b.flags & DEAD != 0 {
            return at;
        }
        live = live.or(at);
    }
    live
}

/// Indirect plan step 1 test ([`area::PIC_TEST_TRAMPOLINE`]): the code-area offset of the hot trampoline of a live split
/// block of `context` whose computed exit has appended words, among the newest records.
pub fn trampoline_for_test(context: u64) -> Option<u64> {
    let count = control().blocks as usize;
    (count.saturating_sub(1 << 14)..count).rev().map(block).find_map(|b| {
        let exit = u64::from(b.code) + u64::from(b.dispatch_exit);
        let at = link_at(b, u64::from(b.dispatch_exit));
        (b.context == context && b.flags & DEAD == 0 && b.hot_size != 0 && b.tail(None).is_some() && at != exit).then_some(at)
    })
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
/// The reverse map of this vCPU's writable shadows (docs/writable-drop.md).
fn writers_table() -> &'static mut [[u64; 2]] {
    unsafe { core::slice::from_raw_parts_mut(lookup_area(area::WRITERS), natemu_abi::writers::ENTRIES) }
}
fn writer_epochs() -> natemu_abi::writers::Epochs {
    let [full, cr3, lossy] = control().writer_epochs;
    natemu_abi::writers::Epochs { full, cr3, lossy }
}
/// Records that this vCPU installed a writable shadow of `va` (in the space of `user`) onto `gpa`'s page; a full probe
/// window makes the map lossy until the next full reset.
pub fn writable_installed(user: bool, va: u64, gpa: u64) {
    use natemu_abi::writers::{count, insert};
    let e = writer_epochs();
    let c = control();
    c.writers[count::INSERTS] += 1;
    if !insert(writers_table(), &e, user, va & !4095, gpa >> 12) {
        c.writers[count::OVERFLOWS] += 1;
        c.writer_epochs[2] = e.full.wrapping_add(1);
    }
}
/// Every shadow of this vCPU went (a full reset): every entry of the map dies.
pub fn writers_reset() {
    control().writer_epochs[0] += 1;
}
/// A CR3 load removed every shadow but the kept half's: the other entries die.
pub fn writers_cr3() {
    control().writer_epochs[1] += 1;
}
/// Removes this vCPU's writable shadows of `gpa`'s page (both spaces, with their TLB entries) and then its writable bit
/// of the page; `false` (nothing done) when the map is lossy: the caller resets every shadow instead.
pub fn drop_writable(gpa: u64) -> Result<bool, natemu_abi::shadow::Error> {
    use natemu_abi::writers::{count, drop_page};
    let e = writer_epochs();
    let c = control();
    if e.lossy() {
        c.writers[count::LOSSY_RESETS] += 1;
        return Ok(false);
    }
    let mut failed = None;
    let n = drop_page(writers_table(), &e, gpa >> 12, |va| {
        if let Err(err) = crate::tables::unmap(va) {
            failed = Some(err);
        }
    });
    if let Some(err) = failed {
        return Err(err);
    }
    c.writers[count::DROPS] += 1;
    c.writers[count::UNMAPPED] += n as u64;
    // The unmaps' TLB invalidations completed (`dsb ish`) before the bit goes.
    let page = (gpa >> 12) as usize;
    if page < pages() {
        let (m, shift) = mask(area::WRITABLE_PAGES, page);
        m.fetch_and(!(u64::from(own()) << shift), SeqCst);
    }
    Ok(true)
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
/// Mapped device memory (docs/restructure/mapped-device-memory-plan.md): the region table in the global area, which only
/// the host writes ([`area::MappedRegions`], a seqlock).
pub fn mapped_regions() -> &'static area::MappedRegions {
    // SAFETY: the table lies in the live global area at its fixed offset, aligned; its fields are atomics.
    unsafe { &*global::<area::MappedRegions>(area::MAPPED) }
}
pub fn split_begin(gpa: [u64; 2], vcpus: usize, mut serve: impl FnMut()) {
    while split_word(0)
        .compare_exchange(0, vcpu() as u64 + 1, SeqCst, SeqCst)
        .is_err()
    {
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
/// Makes every other vCPU that may hold a shadow (its holding flag set) drop all of them ([`SHADOWS`], a kick) and waits
/// for each one's reset counter to move, calling `serve` meanwhile (two vCPUs shooting each other down both progress).
/// Each peer's counter is read before its flag (both SeqCst, after the caller's publication): a peer found with its flag
/// clear either holds nothing, or is between a reset (flag cleared, then the counter moved) and its next `holding()`,
/// after which it reads the published state anew. A vCPU that stopped for good reset first (`execution.rs`
/// `halt`): its flag is clear and it is never waited for. Halted peers are woken by the kick and counted by counter.
/// [`split_begin`]'s scan, kept as its own copy so the split lock's path is as before mapped device memory.
#[cold]
#[inline(never)]
fn shoot_peers(vcpus: usize, serve: &mut impl FnMut()) {
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
    // Let peers reset concurrently.
    while requested != 0 {
        let p = requested.trailing_zeros() as usize;
        while resets_of(p) == counts[p] {
            serve();
            core::hint::spin_loop();
        }
        requested &= requested - 1;
    }
}
/// Mapped device memory (docs/restructure/mapped-device-memory-plan.md): the region table moved during this vCPU's port or
/// device exit ([`area::MappedRegions`]' `seq` changed across it). Makes every peer that may hold a shadow built from the old
/// table drop all of its shadows and waits for each ([`shoot_peers`]; `serve` while waiting); the caller then resets its
/// own. No split lock is taken: concurrent remaps (every vCPU whose exit spanned the publish runs one) and split locks
/// wait for each other only through served shadow requests. After it and the caller's reset, no vCPU holds a shadow built
/// from a table older than the one this vCPU saw after its exit.
///
/// Ordering (a Dekker pairing with the install protocol, `mmio::mapped`: holding flag store, `fence(SeqCst)`, table
/// read): the publication (the host's seqlock stores, made before this vCPU's exit returned, or seen through `seq`) is
/// followed here by `fence(SeqCst)`, then each peer's flag is loaded (SeqCst). Either this load sees a peer's flag, so the
/// peer is asked and its shadows go, or the peer's table read follows the publication and sees the new table.
#[cold]
#[inline(never)]
pub fn remap_shootdown(vcpus: usize, mut serve: impl FnMut()) {
    core::sync::atomic::fence(SeqCst);
    shoot_peers(vcpus, &mut serve);
    control().remaps += 1;
}
pub fn split_end() {
    unsafe { asm!("dsb ish") };
    split_word(1).store(0, SeqCst);
    split_word(2).store(0, SeqCst);
    split_word(0).store(0, SeqCst);
}
/// While waiting on a split lock: takes only peers' shadow requests (the bits of [`SHADOWS`], drop all shadows, and [`DROPS`],
/// drop pages' writable shadows from the drop slots; the caller serves them), leaving the rest of the record (code
/// invalidations, a flush) to the next resident entry, as a block of this vCPU may be live here (its abort handler resumes it).
pub fn take_shadows() -> u64 {
    use natemu_abi::execution::cache::DROPS;
    let word = record_word(vcpu(), None);
    if word.load(SeqCst) & (SHADOWS | DROPS) == 0 {
        return 0;
    }
    word.fetch_and(!(SHADOWS | DROPS), SeqCst) & (SHADOWS | DROPS)
}
/// vCPU `v`'s drop slot `i` (`Record::drops`).
fn drop_word(v: usize, i: usize) -> &'static AtomicU64 {
    let r = global::<Record>(area::RECORDS).wrapping_add(v);
    unsafe { AtomicU64::from_ptr((&raw mut (*r).drops).cast::<u64>().wrapping_add(i)) }
}
/// Asks vCPU `v` to drop its writable shadows of `gpa`'s page ([`DROPS`], then a kick): the slot claimed and its value,
/// which `v` replaces by 0 once it holds none; `None` when every slot is taken (the caller asks for a full reset).
pub fn ask_drop(v: usize, gpa: u64) -> Option<(usize, u64)> {
    use natemu_abi::execution::cache::{DROPS, DROP_SLOTS};
    let value = (gpa >> 12) + 1;
    let i = (0..DROP_SLOTS).find(|&i| drop_word(v, i).compare_exchange(0, value, SeqCst, SeqCst).is_ok());
    let Some(i) = i else {
        control().writers[natemu_abi::writers::count::SLOTS_FULL] += 1;
        return None;
    };
    record_word(v, None).fetch_or(DROPS, SeqCst);
    kick(v);
    Some((i, value))
}
/// Whether vCPU `v`'s slot `i` still holds `value` (the request is not served yet).
pub fn drop_waiting(v: usize, (i, value): (usize, u64)) -> bool {
    drop_word(v, i).load(SeqCst) == value
}
/// This vCPU's drop slot `i`: the page asked for (`gpa`), if any.
pub fn drop_asked(i: usize) -> Option<(u64, u64)> {
    let value = drop_word(vcpu(), i).load(SeqCst);
    (value != 0).then(|| (value, (value - 1) << 12))
}
/// Slot `i`'s request (`value`) is served: this vCPU holds no writable shadow of its page.
pub fn drop_served(i: usize, value: u64) {
    let _ = drop_word(vcpu(), i).compare_exchange(value, 0, SeqCst, SeqCst);
    control().writers[natemu_abi::writers::count::SERVED] += 1;
}
/// Another vCPU holds the split lock over `gpa`'s page.
pub fn split_held(gpa: u64) -> bool {
    let page = (gpa >> 12) + 1;
    split_word(0).load(SeqCst) != vcpu() as u64 + 1
        && (split_word(1).load(SeqCst) == page || split_word(2).load(SeqCst) == page)
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
                let mut bits = if stale.load(SeqCst) == 0 {
                    0
                } else {
                    stale.swap(0, SeqCst)
                };
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
