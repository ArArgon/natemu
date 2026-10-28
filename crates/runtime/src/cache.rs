//! The vCPU's resident code cache (`natemu_abi::execution::cache`): host-admitted
//! blocks placed by sequential fill into the code area, their records (key, code
//! range, access sites, guest-physical source pages) in the metadata arena, lookup
//! by (guest RIP, context) through L1 and L2, native PC → block, whole-cache
//! flush, the code-page masks shared between owners and this vCPU's invalidation
//! queue. The fast dispatcher (`gates.S` `dispatch_fast`) probes L1 directly.
use super::*;
use natemu_abi::code_object::{Header, Site};
use core::mem::align_of;
use natemu_abi::execution::{ENTRY_OFFSET, GUEST_PAGES, cache::{self as area, Control, Queue}};

/// This owner's vCPU: its bit in the code-page masks and its queue.
// ponytail: one vCPU; slice 3 reads it from the owner's record.
const VCPU: usize = 0;
/// Code placement alignment (a cache line).
const CODE_ALIGN: u64 = 64;

/// A placed block's record in the metadata arena; its sites follow it.
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Block {
    pub rip: u64,
    pub context: [u64; 2],
    /// Guest-physical source pages (`u64::MAX` = unused).
    pub source_gpa: [u64; 2],
    /// Offset of the FEX block (its 4-byte `JITCodeHeader` first) in the code area, and its size.
    pub code: u32,
    pub code_size: u32,
    pub sites_count: u32,
    pub guest_bytes: u32,
}
impl Block {
    pub fn code_va(&self) -> u64 {
        area::CODE_VA + u64::from(self.code)
    }
    pub fn entry(&self) -> u64 {
        self.code_va() + u64::from(ENTRY_OFFSET)
    }
    /// Its access sites, sorted by native offset (checked by the host).
    pub fn sites(&self) -> &'static [Site] {
        let at = (self as *const Self).wrapping_add(1).cast::<Site>();
        unsafe { core::slice::from_raw_parts(at, self.sites_count as usize) }
    }
}
const _: () = assert!(size_of::<Block>() % align_of::<Site>() == 0);

fn lookup_area<T>(offset: usize) -> *mut T {
    (area::LOOKUP_VA + offset as u64) as *mut T
}
fn control() -> &'static mut Control {
    unsafe { &mut *lookup_area(area::CONTROL) }
}
fn l1(i: usize) -> *mut [u64; 2] {
    lookup_area::<[u64; 2]>(area::L1).wrapping_add(i)
}
/// {guest RIP, block number + 1}; 0 marks an empty slot.
fn l2(i: usize) -> *mut [u64; 2] {
    lookup_area::<[u64; 2]>(area::L2).wrapping_add(i)
}
fn index(n: usize) -> *mut u32 {
    lookup_area::<u32>(area::INDEX).wrapping_add(n)
}
fn block(n: usize) -> &'static Block {
    unsafe { &*((area::META_VA + u64::from(*index(n))) as *const Block) }
}
/// The code-page mask of guest-physical `gpa` (shared between owners); `None` outside guest RAM.
fn code_page(gpa: u64) -> Option<*mut u8> {
    let page = (gpa >> 12) as usize;
    (page < GUEST_PAGES).then(|| (area::GLOBAL_VA as *mut u8).wrapping_add(area::CODE_PAGES + page))
}
fn queue() -> *mut Queue {
    ((area::GLOBAL_VA + area::QUEUES as u64) as *mut Queue).wrapping_add(VCPU)
}

fn hash(rip: u64, context: [u64; 2]) -> usize {
    let h = rip ^ context[0].rotate_left(23) ^ context[1].rotate_left(47);
    (h.wrapping_mul(0x9e37_79b9_7f4a_7c15) >> 48) as usize & (area::L2_ENTRIES - 1)
}

/// Empties the cache: bump pointers, L1, L2, block index and this vCPU's code-page bits.
/// Called with no generated code live and the state banked.
pub fn reset() {
    let c = control();
    (c.code_used, c.meta_used, c.blocks) = (0, 0, 0);
    clear_l1();
    unsafe { ptr::write_bytes(lookup_area::<u8>(area::L2), 0, area::L2_ENTRIES * 16) };
    for page in 0..GUEST_PAGES {
        let mask = (area::GLOBAL_VA as *mut u8).wrapping_add(area::CODE_PAGES + page);
        // ponytail: plain read-modify-write while one vCPU exists; an atomic bit clear (ldclrb) with several.
        unsafe { mask.write_volatile(mask.read_volatile() & !(1 << VCPU)) };
    }
}
fn clear_l1() {
    for i in 0..area::L1_ENTRIES {
        // A RIP whose low bits are not `i` never matches slot `i`.
        unsafe { l1(i).write([i as u64 ^ 1, 0]) };
    }
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
    unsafe { l1((rip & area::L1_MASK) as usize).write([rip, block.entry()]) };
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
        if key == rip && b.rip == rip && b.context == context {
            return Some(b);
        }
        i = (i + 1) & (area::L2_ENTRIES - 1);
    }
}

/// Places the admitted object at `object` (header `h`) for (`h.guest_rip`, `context`) with its guest-physical
/// source pages, and enters it in L2. `None` when the code area, the metadata arena or the block index is full
/// (the caller flushes and places again).
pub fn place(h: &Header, object: *const u8, context: [u64; 2], source_gpa: [u64; 2]) -> Option<&'static Block> {
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
    };
    unsafe {
        let at = (area::META_VA + meta) as *mut Block;
        at.write(record);
        copy_record(at.add(1).cast(), object.add(h.sites_offset as usize), sites);
        copy_record((area::CODE_WRITE_VA + code) as *mut u8, object.add(h.code_offset as usize), h.code_size as usize);
        synchronize_code(code, h.code_size as usize);
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

/// Code written at `offset` through the writable alias becomes visible to instruction fetch through the executable one.
fn synchronize_code(offset: u64, bytes: usize) {
    unsafe {
        let ctr: u64;
        asm!("mrs {0}, ctr_el0", out(reg) ctr);
        let (dline, iline) = (4u64 << ((ctr >> 16) & 15), 4u64 << (ctr & 15));
        let (start, end) = (offset, offset + bytes as u64);
        let mut at = start & !(dline - 1);
        while at < end {
            asm!("dc cvau, {0}", in(reg) area::CODE_WRITE_VA + at);
            at += dline;
        }
        asm!("dsb ish");
        at = start & !(iline - 1);
        while at < end {
            asm!("ic ivau, {0}", in(reg) area::CODE_VA + at);
            at += iline;
        }
        asm!("dsb ish", "isb");
    }
}

/// The block whose code holds native address `pc`.
pub fn block_at(pc: u64) -> Option<&'static Block> {
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

/// Marks `gpa`'s page as holding this vCPU's translations. True if this vCPU had shadowed it writable since the
/// last shadow reset (the caller resets its shadows, so the page is read-only everywhere).
pub fn protect(gpa: u64) -> bool {
    let Some(mask) = code_page(gpa) else { return false };
    unsafe { mask.write_volatile(mask.read_volatile() | 1 << VCPU) };
    let page = (gpa >> 12) as usize;
    let bits = lookup_area::<u8>(area::WRITABLE).wrapping_add(page / 8);
    unsafe { bits.read() & 1 << (page % 8) != 0 }
}
/// Some vCPU holds translations from `gpa`'s page: shadows of it stay read-only.
pub fn translated(gpa: u64) -> bool {
    code_page(gpa).is_some_and(|mask| unsafe { mask.read_volatile() } != 0)
}
/// Records that this vCPU shadows `gpa`'s page writable.
pub fn shadowed_writable(gpa: u64) {
    let page = (gpa >> 12) as usize;
    if page < GUEST_PAGES {
        let bits = lookup_area::<u8>(area::WRITABLE).wrapping_add(page / 8);
        unsafe { bits.write(bits.read() | 1 << (page % 8)) };
    }
}
/// This vCPU's shadows were all removed.
pub fn shadows_reset() {
    unsafe { ptr::write_bytes(lookup_area::<u8>(area::WRITABLE), 0, GUEST_PAGES.div_ceil(8)) };
}

/// Takes this vCPU's pending invalidations; true if any (the caller flushes).
// ponytail: every request drops the whole cache; per-page block invalidation arrives with SMC handling (S2e).
pub fn drain() -> bool {
    unsafe {
        let q = queue();
        let pending = (&raw const (*q).flush).read_volatile() != 0 || (&raw const (*q).count).read_volatile() != 0;
        (&raw mut (*q).flush).write_volatile(0);
        (&raw mut (*q).count).write_volatile(0);
        pending
    }
}
