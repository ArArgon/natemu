//! The vCPU's resident code cache (`natemu_abi::execution::cache`): host-admitted
//! blocks placed by sequential fill into the code area, their records (key, code
//! range, access sites, guest-physical source pages) in the metadata arena, lookup
//! by (guest RIP, context) through L1 and L2, native PC → block, whole-cache
//! flush, block linking (patching a link point into a branch to its target's entry
//! and restoring it), per-page invalidation of the blocks translated from a written
//! page, the uncached single-step block in the slot, and this vCPU's side of the
//! global area shared by all vCPUs: its bits in the code-page and writable masks,
//! the invalidation records it posts to and drains, and its shadow-reset counter.
//! Every global update is an atomic read-modify-write (SeqCst). The code area's head
//! holds the stop pad, the slot and the dispatcher (`gates.S` `dispatch_template`), which probes L1 directly.
use super::*;
use natemu_abi::code_object::{Header, Site};
use core::mem::align_of;
use core::sync::atomic::{AtomicU8, AtomicU64, Ordering::SeqCst};
use natemu_abi::execution::{
    ENTRY_OFFSET, GUEST_PAGES, LinkPoint, MAX_LINK_POINTS, STATE_VA, STOP_BL, State,
    cache::{self as area, Control, LinkRecord, Record, SLOT, SLOT_BYTES, SLOT_VA, SLOT_WRITE_VA},
};
pub use natemu_abi::execution::cache::{Block, CODE_VA, DEAD, DISPATCH, DISPATCH_MISS, FLUSH, MAX_VCPUS, PAD_VA, PAGES, SHADOWS, STEP};

/// This vCPU's number (its state record's; checked below `MAX_VCPUS` at start).
fn vcpu() -> usize {
    unsafe { (&raw const (*(STATE_VA as *const State)).vcpu).read_volatile() as usize }
}
/// This vCPU's bit in the mask bytes.
fn own() -> u8 {
    1 << vcpu()
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
fn control() -> &'static mut Control {
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
/// Mask byte of `page` in the mask at `mask` (code or writable pages).
fn mask(mask: usize, page: usize) -> &'static AtomicU8 {
    unsafe { AtomicU8::from_ptr(global::<u8>(mask).wrapping_add(page)) }
}
/// The code-page mask of guest-physical `gpa`; `None` outside guest RAM.
fn code_page(gpa: u64) -> Option<&'static AtomicU8> {
    let page = (gpa >> 12) as usize;
    (page < GUEST_PAGES).then(|| mask(area::CODE_PAGES, page))
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
/// vCPU `v`'s shadow-reset counter.
fn resets(v: usize) -> &'static AtomicU64 {
    unsafe { AtomicU64::from_ptr(global::<u64>(area::RESETS).wrapping_add(v)) }
}

fn hash(rip: u64, context: [u64; 2]) -> usize {
    let h = rip ^ context[0].rotate_left(23) ^ context[1].rotate_left(47);
    (h.wrapping_mul(0x9e37_79b9_7f4a_7c15) >> 48) as usize & (area::L2_ENTRIES - 1)
}

/// Empties the cache: bump pointers, L1, L2, block index, link records and this vCPU's code-page bits.
/// Called with no generated code live and the state banked (links already undone, see [`unlink_all`]).
pub fn reset() {
    let c = control();
    (c.code_used, c.meta_used, c.blocks, c.links) = (area::HEAD_BYTES as u64, 0, 0, 0);
    clear_l1();
    unsafe { ptr::write_bytes(lookup_area::<u8>(area::L2), 0, area::L2_ENTRIES * 16) };
    for page in 0..GUEST_PAGES {
        mask(area::CODE_PAGES, page).fetch_and(!own(), SeqCst);
    }
}
/// Code-area offset of L1 entry `i`.
fn l1_entry(i: usize) -> u64 {
    (area::L1_CODE + i * area::L1_ENTRY_BYTES) as u64
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
/// Empties L1 entry `i` (its first word branches to the dispatcher's miss); the caller synchronizes the code.
fn empty_l1(i: usize) {
    let at = l1_entry(i);
    unsafe { ((area::CODE_WRITE_VA + at) as *mut u32).write_volatile(branch(at, area::DISPATCH_MISS as u64)) };
}
fn clear_l1() {
    for i in 0..area::L1_ENTRIES {
        empty_l1(i);
    }
    let bytes = area::L1_ENTRIES * area::L1_ENTRY_BYTES;
    synchronize_code(area::CODE_WRITE_VA + l1_entry(0), area::CODE_VA + l1_entry(0), bytes);
}

/// Fills the code area's head once, before any block runs: the stop pad's `ret` (the rest of its granule stays zero,
/// `udf`) and the dispatcher's code.
pub fn fill_head(dispatcher: &[u8]) {
    unsafe {
        ((area::CODE_WRITE_VA + area::PAD as u64) as *mut u32).write_volatile(0xd65f_03c0); // ret
        copy_record((area::CODE_WRITE_VA + area::DISPATCH as u64) as *mut u8, dispatcher.as_ptr(), dispatcher.len());
    }
    synchronize_code(area::CODE_WRITE_VA + area::PAD as u64, PAD_VA, 4);
    synchronize_code(area::CODE_WRITE_VA + area::DISPATCH as u64, area::DISPATCH_VA, dispatcher.len());
}

/// L1 is keyed by RIP alone, so it holds entries of one context: switching clears it.
pub fn select_l1(context: [u64; 2]) {
    let c = control();
    if c.l1_context != context {
        clear_l1();
        c.l1_context = context;
    }
}
pub fn fill_l1(rip: u64, block: &Block) {
    let at = l1_entry((rip & area::L1_MASK) as usize);
    let words = l1_words(at, rip, block.entry() - area::CODE_VA);
    unsafe { copy_record((area::CODE_WRITE_VA + at) as *mut u8, words.as_ptr().cast(), size_of_val(&words)) };
    synchronize_code(area::CODE_WRITE_VA + at, area::CODE_VA + at, area::L1_ENTRY_BYTES);
}

/// The block cached for (`rip`, `context`).
pub fn lookup(rip: u64, context: [u64; 2]) -> Option<&'static Block> {
    let mut i = hash(rip, context);
    loop {
        let [key, number] = unsafe { l2(i).read() };
        if number == 0 {
            return None;
        }
        let b = block(number as usize - 1);
        if key == rip && b.rip == rip && b.context == context && b.flags & DEAD == 0 {
            return Some(b);
        }
        i = (i + 1) & (area::L2_ENTRIES - 1);
    }
}

/// Places the admitted object at `object` (header `h`) for (`h.guest_rip`, `context`) with its guest-physical
/// source pages and link points, relocates its stop `bl` and its computed exit's `b` (at `dispatch_exit`, 0 for none;
/// the reply was checked to hold the placeholders there), and enters it in L2. `None` when the code area, the metadata arena or the
/// block index is full (the caller flushes and places again).
pub fn place(
    h: &Header,
    object: *const u8,
    context: [u64; 2],
    source_gpa: [u64; 2],
    links: [LinkPoint; MAX_LINK_POINTS],
    dispatch_exit: u64,
) -> Option<&'static Block> {
    let c = control();
    let code = c.code_used.next_multiple_of(CODE_ALIGN);
    let code_end = code + u64::from(h.code_size);
    let meta = c.meta_used.next_multiple_of(align_of::<Block>() as u64);
    let sites = size_of::<Site>() * h.sites_count as usize;
    let meta_end = meta + (size_of::<Block>() + sites) as u64;
    if code_end > c.code_bytes || meta_end > area::META_BYTES as u64 || c.blocks as usize >= area::MAX_BLOCKS {
        return None;
    }
    let record = Block {
        rip: h.guest_rip,
        context,
        source_gpa,
        code: code as u32,
        code_size: h.code_size,
        sites_count: h.sites_count,
        guest_bytes: h.guest_bytes,
        number: c.blocks as u32,
        flags: 0,
        links,
    };
    unsafe {
        let at = (area::META_VA + meta) as *mut Block;
        at.write(record);
        copy_record(at.add(1).cast(), object.add(h.sites_offset as usize), sites);
        copy_record((area::CODE_WRITE_VA + code) as *mut u8, object.add(h.code_offset as usize), h.code_size as usize);
        relocate(code, dispatch_exit);
        synchronize_code(area::CODE_WRITE_VA + code, area::CODE_VA + code, h.code_size as usize);
        let n = c.blocks as usize;
        index(n).write(meta as u32);
        let mut i = hash(record.rip, context);
        while l2(i).read()[1] != 0 {
            i = (i + 1) & (area::L2_ENTRIES - 1);
        }
        l2(i).write([record.rip, n as u64 + 1]);
        (c.code_used, c.meta_used, c.blocks) = (code_end, meta_end, n as u64 + 1);
        Some(&*at)
    }
}

/// Places the admitted object at `object` as the single-step block in the slot: uncached (not in the index, L1 or L2,
/// never linked) and marking no source page, so the write it steps through completes (it drops the page's blocks
/// itself, see the runtime's `code_written`). `None` if it does not fit the slot or the record area.
pub fn place_step(
    h: &Header,
    object: *const u8,
    context: [u64; 2],
    source_gpa: [u64; 2],
    links: [LinkPoint; MAX_LINK_POINTS],
    dispatch_exit: u64,
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
        source_gpa,
        code: SLOT as u32,
        code_size: h.code_size,
        sites_count: h.sites_count,
        guest_bytes: h.guest_bytes,
        number: u32::MAX,
        flags: STEP,
        links,
    };
    unsafe {
        let at = lookup_area::<Block>(area::STEP_RECORD);
        at.write(record);
        copy_record(at.add(1).cast(), object.add(h.sites_offset as usize), sites);
        copy_record(SLOT_WRITE_VA as *mut u8, object.add(h.code_offset as usize), h.code_size as usize);
        relocate(SLOT as u64, dispatch_exit);
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
/// Relocates the placeholders of the block just copied to code-area offset `code` (before its code is synchronized):
/// its stop check becomes `bl` to the stop pad, its computed exit (at `dispatch_exit`, 0 for none) `b` to the
/// dispatcher. (The placeholders were checked at the reply.)
fn relocate(code: u64, dispatch_exit: u64) {
    let write = |at: u64, w: u32| unsafe { ((area::CODE_WRITE_VA + at) as *mut u32).write_volatile(w) };
    let at = code + u64::from(STOP_BL);
    write(at, 0x8000_0000 | branch(at, area::PAD as u64)); // bl
    if dispatch_exit != 0 {
        let at = code + dispatch_exit;
        write(at, branch(at, area::DISPATCH as u64));
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
    if let Some(m) = code_page(gpa) {
        post(m.swap(0, SeqCst) & !own(), gpa);
    }
    invalidate(gpa)
}
/// This vCPU wrote `gpa`'s page through the RAM alias (a split store's part, a walker's A/D update): a peer that
/// marked the page after [`written`] cleared it, or after a check found it unmarked, may have been given the old
/// bytes. Posts the page to every such peer and clears their bits (this vCPU marks no page while it writes one).
pub fn rechecked(gpa: u64) {
    if let Some(m) = code_page(gpa) {
        unsafe { asm!("dmb ish") };
        if m.load(SeqCst) & !own() != 0 {
            post(m.fetch_and(own(), SeqCst) & !own(), gpa);
        }
    }
}
/// Posts `gpa`'s page to the vCPUs whose bits are set in `peers` and kicks them.
fn post(peers: u8, gpa: u64) {
    let page = (gpa >> 12) as usize;
    for v in (0..MAX_VCPUS).filter(|v| peers & 1 << v != 0) {
        record_word(v, Some(page / 64)).fetch_or(1 << (page % 64), SeqCst);
        record_word(v, None).fetch_or(PAGES, SeqCst);
        kick(v);
    }
}
/// Asks the host to stop vCPU `v` at its next block entry (it then drains its record); returns at once.
pub fn kick(v: usize) {
    unsafe { asm!("dsb sy", "hvc #{imm}", imm = const natemu_abi::execution::KICK_HVC, in("x0") v) };
}

/// Invalidates every live block of this vCPU translated from `gpa`'s page (the page is about to be written, or was):
/// each leaves L1 and L2, every link into or out of it is undone and its record dropped, and it is marked dead. Then
/// this vCPU's bit for the page is cleared. Returns the links undone; `None` if a record does not describe its word
/// (as [`unlink_all`]).
// ponytail: scans every block and record; a reverse map (page → blocks, block → records) if profiles show it.
pub fn invalidate(gpa: u64) -> Option<u64> {
    let unlinked = kill(|b| b.source_gpa.iter().any(|&g| g != u64::MAX && g >> 12 == gpa >> 12))?;
    if let Some(mask) = code_page(gpa) {
        mask.fetch_and(!own(), SeqCst);
    }
    Some(unlinked)
}
/// Invalidates the live block `b` alone (as [`invalidate`], its pages' marks untouched): a block placed from a page
/// whose mark this vCPU lost while it was fetched.
pub fn discard(b: &Block) -> Option<u64> {
    let number = b.number;
    kill(|x| x.number == number)
}
/// Invalidates (as [`invalidate`]) every live block of this vCPU with guest bytes in the linear range
/// `[start, start + bytes)` (a guest INVLPG). Its code-page bits stay: they only say it may hold translations.
pub fn invalidate_linear(start: u64, bytes: u64) -> Option<u64> {
    kill(|b| b.rip.wrapping_sub(start) < bytes || start.wrapping_sub(b.rip) < u64::from(b.guest_bytes))
}
/// Marks every live block `doomed` selects dead: out of L1 and L2, every link into or out of it undone and its record
/// dropped. Returns the links undone; `None` if a record does not describe its word.
fn kill(doomed: impl Fn(&Block) -> bool) -> Option<u64> {
    let c = control();
    let mut unlinked = 0;
    for n in 0..c.blocks as usize {
        let b = block(n);
        if b.flags & DEAD != 0 || !doomed(b) {
            continue;
        }
        unsafe { (*block_ptr(n)).flags |= DEAD };
        let i = (b.rip & area::L1_MASK) as usize;
        let at = l1_entry(i);
        let held = |k: usize| unsafe { ((area::CODE_WRITE_VA + at) as *const u32).add(k).read_volatile() };
        if l1_words(at, b.rip, u64::from(b.code) + u64::from(ENTRY_OFFSET)).iter().enumerate().all(|(k, &w)| held(k) == w) {
            empty_l1(i);
            synchronize_code(area::CODE_WRITE_VA + at, area::CODE_VA + at, 4);
        }
        let mut i = 0;
        while i < c.links {
            let r = unsafe { record(i as usize).read() };
            if r.source as usize == n || r.target as usize == n {
                unlink(r)?;
                unlinked += 1;
                c.links -= 1;
                unsafe { record(i as usize).write(record(c.links as usize).read()) };
            } else {
                i += 1;
            }
        }
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
/// never the head's pad or dispatcher).
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
    let Some(m) = code_page(gpa) else { return 0 };
    m.fetch_or(own(), SeqCst);
    writable(gpa)
}
/// This vCPU's mark of `gpa`'s page is still set (no write cleared it since [`mark`]); pages outside guest RAM count as
/// marked (no code is fetched from them).
pub fn marked(gpa: u64) -> bool {
    code_page(gpa).is_none_or(|m| m.load(SeqCst) & own() != 0)
}
/// The vCPUs that may hold a writable shadow of `gpa`'s page.
pub fn writable(gpa: u64) -> u8 {
    let page = (gpa >> 12) as usize;
    if page < GUEST_PAGES { mask(area::WRITABLE_PAGES, page).load(SeqCst) } else { 0 }
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
    code_page(gpa).is_some_and(|m| m.load(SeqCst) != 0)
}
/// Records that this vCPU shadows `gpa`'s page writable.
pub fn shadowed_writable(gpa: u64) {
    let page = (gpa >> 12) as usize;
    if page < GUEST_PAGES {
        mask(area::WRITABLE_PAGES, page).fetch_or(own(), SeqCst);
    }
}
/// This vCPU's shadows were all removed (and the TLB invalidated): clears its writable bits, then counts the reset
/// (a peer waiting for this vCPU to drop its writable shadows waits for the counter to change).
pub fn shadows_reset() {
    for page in 0..GUEST_PAGES {
        mask(area::WRITABLE_PAGES, page).fetch_and(!own(), SeqCst);
    }
    resets(vcpu()).fetch_add(1, SeqCst);
}

/// Takes this vCPU's pending invalidations: swaps `pending` to 0, then each stale word, and invalidates each listed
/// page ([`invalidate`], whose `None` this returns) unless the whole cache goes anyway. Returns the pending bits;
/// the caller flushes on [`FLUSH`] and resets its shadows on [`SHADOWS`].
pub fn drain() -> Option<u64> {
    let pending = record_word(vcpu(), None).swap(0, SeqCst);
    if pending & PAGES != 0 {
        for i in 0..GUEST_PAGES / 64 {
            let mut bits = record_word(vcpu(), Some(i)).swap(0, SeqCst);
            while bits != 0 && pending & FLUSH == 0 {
                invalidate(((i * 64 + bits.trailing_zeros() as usize) as u64) << 12)?;
                bits &= bits - 1;
            }
        }
    }
    Some(pending)
}
