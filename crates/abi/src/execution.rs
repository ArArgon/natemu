//! General block execution (slices 1 and 2): the vCPU's x86 architectural state bank,
//! the record the resident and the host exchange per translation, the private layout
//! of the execution mode (code cache included), and the resident entries generated code makes.
//!
//! Flow: blocks stay in the vCPU's resident code cache ([`cache`]). At a block
//! exit the resident's dispatcher looks up the next RIP (L1, then L2); only a
//! miss asks the host for code (`hvc #TRANSLATE_HVC`): the resident walks the fetch
//! and marks the pages as code, the host copies the bytes from those guest-physical
//! pages, compiles and admits one block into the staging area
//! and replies; the resident places it in the cache and enters it. A literal exit
//! (a [`LinkPoint`]) goes through the resident's linker instead, which finds the
//! target the same way and then patches the exit into a direct branch to it, so a
//! linked edge never leaves generated code. Shadow misses are served in the resident.
use crate::{
    PRIVATE,
    paging::{self, Control},
};

/// `Shared::mode` of the execution mode.
pub const MODE: u64 = 32;
/// Resident → host: translate at `cpu.rip` (state record holds the request).
pub const TRANSLATE_HVC: u64 = 0x4b;
/// The GIC SGI a resident sends a peer to stop it ([`STOP_WORD`]; `ICC_SGI1R_EL1`). The host's doorbell for vCPU v is
/// the SPI `spi_base + v` (`hv_gic_set_spi`), routed to it.
pub const KICK_SGI: u64 = 1;
/// A precise stop by IRQ-triggered unlinking: the resident's IRQ handler (a doorbell: a host SPI or a peer's SGI) stops a
/// vCPU interrupted in the dispatcher, L1 or at a block's first word at once; interrupted inside a block, it patches
/// every exit of that block (each link point's first word and the computed exit's `b`) to this `brk`, so the block
/// stops at its exit with the exit's exact state, and restores the words at the next resident entry.
pub const STOP_IMM: u16 = 6;
pub const STOP_WORD: u32 = 0xd420_0000 | (STOP_IMM as u32) << 5;

/// `svc` immediates of the resident entries made from generated code (fexc `FexcSvc`). Generated code touches no
/// emulator data: these, the check stubs, aborts and interrupts are its only ways into the resident.
pub mod svc {
    /// A literal exit (a [`super::LinkPoint`]), its target RIP in x0: the linker.
    pub const LINK: u16 = 1;
    /// The dispatcher's L1 miss (resident code in the code area, [`super::cache::DISPATCH`]), the RIP in x0.
    pub const MISS: u16 = 2;
    /// 128/64 unsigned / signed division ([`super::divide`]): x0 high, x1 low, x2 divisor in; x0 quotient, x1
    /// remainder, x2 = 1 for #DE (else 0) out. Nothing else changes.
    pub const DIV_U: u16 = 3;
    pub const DIV_S: u16 = 4;
    /// I6b: an x87 transcendental, remainder, scale or extract instruction ([`crate::x87::helper`]), run whole on the x87
    /// state in the guest-state page: w0 = the instruction's second byte (D9 F0..FF: F2XM1 FYL2X FPTAN FPATAN FXTRACT
    /// FPREM1 FPREM FYL2XP1 FSINCOS FSCALE FSIN FCOS). No register changes; only the page's x87 fields.
    pub const X87: u16 = 5;
    /// I6b (I5c's PCMPxSTRx): FEX's VPCMPISTRX/VPCMPESTRX intermediate ([`crate::strings::pcmpstr`]): v0 = xmm1, v1 =
    /// xmm2/m128, w0 = imm8 | REX.W/VEX.W << 8 | explicit << 9, x1 = RAX and x2 = RDX (explicit forms); w0 out = IntRes2
    /// (bits 15:0) | flags (bits 31:28: SF ZF CF OF as NZCV). Nothing else changes.
    pub const STRING: u16 = 6;
}
/// The 128/64 division of [`svc::DIV_U`] / [`svc::DIV_S`] (FEX's LUDIV/LDIV helpers): the dividend `high:low`
/// by `divisor`, as (quotient, remainder), or `None` for #DE (a zero divisor, or a quotient that does not fit 64
/// bits, signed or unsigned). The signed remainder has the dividend's sign.
pub fn divide(signed: bool, high: u64, low: u64, divisor: u64) -> Option<(u64, u64)> {
    let n = (high as u128) << 64 | low as u128;
    if signed {
        let (n, d) = (n as i128, divisor as i64 as i128);
        let q = n.checked_div(d)?;
        i64::try_from(q).ok().map(|q| (q as u64, (n % d) as u64))
    } else {
        let d = divisor as u128;
        (high < divisor).then(|| ((n / d) as u64, (n % d) as u64))
    }
}
/// The register holding an `nzcv=saved` site's incoming NZCV (`mrs x25, nzcv` at its instruction's start; fexc
/// `FexcPrecision::PreNzcvReg`, the object header's `pre_nzcv_register`).
pub const PRE_NZCV_REG: u8 = 25;

pub const STATE_OFFSET: usize = 0x22000;
pub const STATE_VA: u64 = PRIVATE + STATE_OFFSET as u64;
/// FEX `CpuStateFrame` (one page per vCPU, the guest-state page): the resident's EL1-only alias. Admitted code accesses
/// only its guest-state fields ([`frame::GUEST_STATE`]), through x28 = [`FRAME_POINTER`].
pub const FRAME_OFFSET: usize = 0x23000;
pub const FRAME_VA: u64 = PRIVATE + FRAME_OFFSET as u64;
/// The guest-state page's EL0 read/write alias (G1 minimum, I1): x28 points here at every block entry, so generated
/// code reaches the guest-state fields at EL0. It lies in the private window, whose other pages stay EL1-only or EL0
/// execute-only; it is the one window page EL0 may read and write. Documented deviation: a guest access to this page
/// (a non-canonical address, #GP on x86) reaches the vCPU's own guest state instead; the page holds nothing but guest
/// state (the resident rewrites the flag bytes from its bank at every block entry and reads back only DF).
pub const GUEST_STATE_OFFSET: usize = 0x24000;
pub const GUEST_STATE_VA: u64 = PRIVATE + GUEST_STATE_OFFSET as u64;
pub const FRAME_POINTER: u64 = GUEST_STATE_VA;
/// Host-written admitted object (header, code, tables).
pub const STAGING_OFFSET: usize = 0x70000;
pub const STAGING_BYTES: usize = 0x20000;
pub const STAGING_VA: u64 = PRIVATE + STAGING_OFFSET as u64;
/// Native stack mapped below `crate::STACK_TOP` while code runs (SP_EL0; admitted code never uses it).
pub const STACK_BYTES: usize = 0x8000;
/// Guest-physical RAM `[0, R)` (M1): R is the VM's, chosen at run time (a 2 MiB multiple up to [`MAX_GUEST_RAM_BYTES`],
/// the boot map's largest low RAM; [`cache::Control::ram_bytes`]). The resident's EL1-only alias starts at the private
/// window's second GiB and is mapped in 2 MiB blocks through one level-2 table per GiB (window pages from
/// [`RAM_TABLES_OFFSET`], written by the host, never mapped); the backing holds RAM after every vCPU's block (the host's
/// `Backing`).
pub const GUEST_RAM_VA: u64 = PRIVATE + (1 << 30);
pub const MAX_GUEST_RAM_BYTES: usize = 3 << 30;
/// Guest-physical pages the per-page structures cover (only the VM's first R / 4096 are used).
pub const MAX_GUEST_PAGES: usize = MAX_GUEST_RAM_BYTES / 4096;
pub const RAM_TABLES_OFFSET: usize = 0x25000;
/// The PC's legacy hole (VGA memory and option/system ROM on a PC) is not RAM (owner decision, October 29: unassigned
/// ports and addresses behave as on a PC): an access goes the device path (unassigned until a device claims it: a read
/// then shadows the page read-only onto the shared all-ones page, `crate::ALL_ONES_IPA`, never onto its RAM alias), a
/// fetch from it is not RAM. Its alias and per-page slots exist but are never used. Everything else
/// below 16 MiB that the boot map reserves (loader tables, the NVS ranges) is RAM, as on a PC.
pub const LEGACY_HOLE: core::ops::Range<u64> = 0xa_0000..0x10_0000;
/// Guest RAM `[0, bytes)` less [`LEGACY_HOLE`], aliased from `va` (host or resident).
///
/// # Safety
/// As `GuestRam::new`: `[va, va + bytes)` backs `[0, bytes)` while the value is used; `bytes` >= 1 MiB.
pub const unsafe fn guest_ram(bytes: u64, va: u64) -> crate::guest_memory::GuestRam {
    let (start, end) = (LEGACY_HOLE.start, LEGACY_HOLE.end);
    unsafe { crate::guest_memory::GuestRam::new(0, start, va).and(end, bytes - end, va + end) }
}
/// The private window: the first GiB's areas ([`cache`]), then the guest-RAM alias.
pub const PRIVATE_BYTES: u64 = (1 << 30) + MAX_GUEST_RAM_BYTES as u64;
const _: () = assert!(MAX_GUEST_RAM_BYTES % (2 << 20) == 0 && MAX_GUEST_PAGES % 64 == 0);
const _: () = assert!(GUEST_STATE_OFFSET + frame::BYTES <= RAM_TABLES_OFFSET);
const _: () = assert!(RAM_TABLES_OFFSET + (MAX_GUEST_RAM_BYTES >> 30) * 4096 <= (crate::STACK_TOP - PRIVATE) as usize - STACK_BYTES);

/// The code cache (slice 2). Per vCPU: an executable code area with a writable
/// alias, a metadata arena (block records and their access sites) and the lookup
/// structures. Shared between vCPUs ([`cache::GLOBAL_VA`]): the code-page and
/// writable-shadow masks over guest-physical pages, one invalidation record and one
/// shadow-reset counter per vCPU. Areas are mapped in 2 MiB blocks; vCPU v's block of
/// the backing holds its window, then these areas at the offsets below, the code area
/// last because its size is set at `prepare`; guest RAM follows the last block. vCPU v's own areas (window, lookup, metadata, code) sit at the same
/// private addresses as every other vCPU's, backed by its own block of the backing.
pub mod cache {
    use crate::PRIVATE;
    const MIB: usize = 1 << 20;
    /// Code area: executable alias (blocks run here) and writable alias (the resident places them there). It starts
    /// with a head the resident fills ([`SLOT`], [`DISPATCH`], [`L1_CODE`]); blocks follow from [`HEAD_BYTES`].
    pub const CODE_VA: u64 = PRIVATE + 0x1000_0000;
    pub const CODE_WRITE_VA: u64 = PRIVATE + 0x2000_0000;
    /// Default capacity for blocks (after the head).
    pub const CODE_BYTES: usize = 16 * MIB;
    /// The whole area, head included: every branch inside it (links, the dispatcher `b`) is one `b` (±128 MiB).
    pub const CODE_BYTES_MAX: usize = 128 * MIB;
    /// The single-step slot: the uncached one-instruction block ([`STEP`]).
    pub const SLOT: usize = 0;
    pub const SLOT_BYTES: usize = 0x10000;
    pub const SLOT_VA: u64 = CODE_VA + SLOT as u64;
    pub const SLOT_WRITE_VA: u64 = CODE_WRITE_VA + SLOT as u64;
    /// The dispatcher, copied from the resident: computed exits branch here with the next RIP in x0 (each one's `b`
    /// relocated at placement); it branches to the RIP's L1 entry ([`L1_CODE`]). Its `svc #MISS` at [`DISPATCH_MISS`]
    /// enters the resident.
    pub const DISPATCH: usize = SLOT + SLOT_BYTES;
    pub const DISPATCH_BYTES: usize = 0x1000;
    pub const DISPATCH_VA: u64 = CODE_VA + DISPATCH as u64;
    pub const DISPATCH_MISS: usize = DISPATCH + 16;
    /// L1 as code: direct-mapped entries of [`L1_ENTRY_BYTES`] indexed by `rip & L1_MASK` (FEX `L1Pointer/L1Mask`),
    /// so the dispatcher reads no data. A filled entry is `movz/movk x2, #rip` (4 words); `sub x2, x2, x0`; `cbnz x2,
    /// miss`; `b entry`; an empty one starts with `b miss`. Only the resident writes them.
    pub const L1_CODE: usize = DISPATCH + DISPATCH_BYTES;
    pub const L1_ENTRIES: usize = 4096;
    pub const L1_MASK: u64 = L1_ENTRIES as u64 - 1;
    pub const L1_ENTRY_BYTES: usize = 32;
    /// Blocks are placed from here.
    pub const HEAD_BYTES: usize = L1_CODE + L1_ENTRIES * L1_ENTRY_BYTES;
    /// The largest block admitted; the code area holds at least one.
    pub const MAX_BLOCK_BYTES: usize = 32 * 1024;
    /// Pool sizes: the Debian kernel's early boot averaged 261 code and 230 metadata bytes per block (M1), so the arena
    /// holds about [`MAX_BLOCKS`] (K1: 256 Ki, after the reverse map ended invalidation's scan of every block; a boot
    /// into user space overflowed 128 Ki) blocks, which fill about 68 MiB of code.
    pub const META_VA: u64 = PRIVATE + 0x3000_0000;
    pub const META_BYTES: usize = 64 * MIB;
    pub const LOOKUP_VA: u64 = PRIVATE + 0x3800_0000;
    pub const LOOKUP_BYTES: usize = (LINEAR_HEADS + 4 * LINEAR_BUCKETS).next_multiple_of(BLOCK);
    pub const GLOBAL_VA: u64 = PRIVATE + 0x3c00_0000;
    pub const GLOBAL_BYTES: usize = 2 * MIB;
    /// Mapping granule of the areas.
    pub const BLOCK: usize = 2 * MIB;

    /// Backing offsets in a vCPU's block (2 MiB aligned), after its window's 2 MiB.
    pub const LOOKUP_OFFSET: usize = 2 * crate::RAM_SIZE;
    pub const GLOBAL_OFFSET: usize = LOOKUP_OFFSET + LOOKUP_BYTES;
    pub const META_OFFSET: usize = GLOBAL_OFFSET + GLOBAL_BYTES;
    pub const CODE_OFFSET: usize = META_OFFSET + META_BYTES;

    /// Lookup area layout: [`Control`], L1's owners, L2, block index (L1 is code, [`L1_CODE`]).
    pub const CONTROL: usize = 0;
    /// K1: the block each L1 entry enters (u32 block number + 1; 0: empty), so a CR3 load empties only the entries of
    /// blocks it makes stale.
    pub const L1_OWNERS: usize = 0x1_0000;
    /// L2: open-addressed {guest RIP, block number + 1} table keyed by (RIP, context).
    pub const L2: usize = 0x2_0000;
    pub const L2_ENTRIES: usize = 1 << 19;
    /// Block index: metadata offsets of the blocks in placement (= code address) order.
    pub const INDEX: usize = L2 + 16 * L2_ENTRIES;
    /// At most half of L2, so probes stay short.
    pub const MAX_BLOCKS: usize = L2_ENTRIES / 2;
    /// Link records ([`LinkRecord`]), one per patched link point; a full area means no new links.
    pub const LINKS: usize = INDEX + 4 * MAX_BLOCKS;
    pub const MAX_LINKS: usize = 64 * 1024;
    /// The single-step block's record (its code is in the slot, `SLOT_VA`) and its access sites after it.
    pub const STEP_RECORD: usize = LINKS + 16 * MAX_LINKS;
    pub const STEP_RECORD_BYTES: usize = 0x3_c000;
    /// M1: this vCPU's summaries of the code-page and writable mask words it set bits in (bit i % 64 of u64 i / 64: word
    /// i of the mask may hold its bits), so clearing its bits visits only those words, not all of guest RAM's (only the
    /// resident of this vCPU reads or writes them).
    pub const CODE_SUMMARY: usize = STEP_RECORD + STEP_RECORD_BYTES;
    pub const SUMMARY_BYTES: usize = MASK_BYTES / 64;
    pub const WRITABLE_SUMMARY: usize = CODE_SUMMARY + SUMMARY_BYTES;
    /// K1, the reverse map: per guest-physical page (u32 each, [`super::MAX_GUEST_PAGES`]), the last block filed with a
    /// source byte in it (number + 1; 0: none); each block chains to the previous one of each of its source pages
    /// ([`Block::next`]). Nonzero only for pages in this vCPU's code summary.
    pub const PAGE_BLOCKS: usize = (WRITABLE_SUMMARY + SUMMARY_BYTES).next_multiple_of(4096);
    /// K1, the linear map: [`LINEAR_BUCKETS`] chain heads (u32, block number + 1) by a hash of a linear page; each block is
    /// on the chains of the linear pages of its first and last byte ([`Block::lnext`]), so an INVLPG or a #PF looks only at
    /// blocks hashed to its pages. Dead blocks leave a chain when a walk of it meets them.
    pub const LINEAR_HEADS: usize = PAGE_BLOCKS + 4 * super::MAX_GUEST_PAGES;
    pub const LINEAR_BUCKETS: usize = 1 << 16;

    /// Global area layout, shared by all vCPUs and updated only by atomic read-modify-writes (host and resident): one
    /// shadow-reset counter (u64) per vCPU and the per-vCPU [`Events`] in its first 64 KiB; then the per-page structures,
    /// sized for [`super::MAX_GUEST_PAGES`] (the VM uses its first R / 4096 pages; pages never touched are never backed):
    /// the code-page mask (bit v of a page's nibble: vCPU v holds translations from the page) and the writable mask
    /// (bit v: vCPU v may hold a writable shadow of it), 4 bits per page, page p's nibble at bit 4 (p % 16) of u64 word
    /// p / 16, so one atomic word operation reads or changes every vCPU's bit of a page (M1); then one invalidation
    /// [`Record`] per vCPU.
    pub const CODE_PAGES: usize = 0x1_0000;
    pub const MASK_BYTES: usize = super::MAX_GUEST_PAGES / 2;
    pub const WRITABLE_PAGES: usize = CODE_PAGES + MASK_BYTES;
    pub const RECORDS: usize = WRITABLE_PAGES + MASK_BYTES;
    pub const RESETS: usize = 0x3000;
    /// Per-vCPU [`Events`] records (X2).
    pub const EVENTS: usize = 0x3100;
    /// vCPUs of one VM (bits of the mask nibbles).
    pub const MAX_VCPUS: usize = 4;

    /// Per-vCPU cache bookkeeping at [`CONTROL`]. The host sets `code_bytes` (the code area's size, head included) and
    /// `ram_bytes` (the VM's guest RAM, R) at prepare; the resident owns the rest.
    #[repr(C)]
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub struct Control {
        pub code_bytes: u64,
        /// Bump pointers of the code area (from [`HEAD_BYTES`]) and the metadata arena, and blocks placed since the
        /// last flush.
        pub code_used: u64,
        pub meta_used: u64,
        pub blocks: u64,
        /// The context word ([`super::Cpu::code_context`]) L1 was filled under.
        pub l1_context: u64,
        /// Link records in use.
        pub links: u64,
        /// The armed stop ([`super::STOP_WORD`]): `armed` words of the interrupted block, each (code-area offset, its
        /// word before the patch), restored at the next resident entry.
        pub armed: u64,
        pub armed_words: [[u32; 2]; super::MAX_LINK_POINTS + 1],
        /// Doorbells: IRQs acknowledged (spurious ones excluded), of which SGIs, and the CNTVCT ticks spent in their
        /// acknowledge and end (`ICC_IAR1_EL1` read to `ICC_EOIR1_EL1` write); SGIs sent to peers and the ticks of
        /// their `ICC_SGI1R_EL1` writes.
        pub irqs: u64,
        pub irq_sgis: u64,
        pub irq_ticks: u64,
        pub sgis: u64,
        pub sgi_ticks: u64,
        /// Stops armed again in the interrupted block after a resident entry that resumed it mid-block (a division,
        /// a retried access, a split store).
        pub rearms: u64,
        /// The VM's guest RAM, R (M1; set by the host at prepare, last so the resident's fields keep their places).
        pub ram_bytes: u64,
        /// K1: this vCPU's TLB epochs. `epoch` moves at every CR3 load and every global TLB flush (a paging control
        /// change, an MTRR write); `global_epoch` is the `epoch` of the last global flush. A block is current (its source
        /// mapping may be used as a TLB entry) when [`Block::checked`] is `epoch`, or, for a [`GLOBAL`] block, at least
        /// `global_epoch` (MOV to CR3 keeps global entries, SDM Vol.3 4.10.4.1).
        pub epoch: u64,
        pub global_epoch: u64,
        /// K1 counters: CR3 loads, global TLB flushes, INVLPGs; blocks found again by a fetch walk (`revalidated`) or not
        /// (`remapped`: the linear page maps other bytes now); flushes for capacity; blocks killed (a write to a source
        /// page) and made not current by an INVLPG.
        pub cr3_loads: u64,
        pub global_flushes: u64,
        pub invlpgs: u64,
        pub revalidated: u64,
        pub remapped: u64,
        pub capacity_flushes: u64,
        pub killed: u64,
        pub unvalidated: u64,
        /// K1: the linear 2 MiB regions (bit r % 1024) a live block's guest bytes may lie in, so an INVLPG elsewhere looks
        /// at no block.
        pub regions: [u64; 16],
        /// K1: CNTVCT ticks spent in TLB flushes (`cache::tlb`) and INVLPGs (shadows and blocks).
        pub tlb_ticks: u64,
        pub invlpg_ticks: u64,
        /// K1: #PFs delivered (each invalidates its page's blocks) and their ticks; blocks visited on linear chains.
        pub page_faults: u64,
        pub page_fault_ticks: u64,
        pub scanned: u64,
        /// S4 counters: instructions run alone under TF, trap-class #DBs and instruction-breakpoint #DBs delivered,
        /// watchpoint exceptions taken; and the Arm watchpoints this PE implements (ID_AA64DFR0_EL1.WRPs + 1).
        pub debug_steps: u64,
        pub debug_traps: u64,
        pub code_breaks: u64,
        pub watch_hits: u64,
        pub watchpoints: u64,
    }
    /// A block placed in the cache: its record in the metadata arena (at the offset `INDEX` lists), its access sites
    /// ([`crate::code_object::Site`]) right after it.
    #[repr(C)]
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub struct Block {
        pub rip: u64,
        /// [`super::Cpu::code_context`] it was translated under (K1: not CR3; the source pages below say which bytes).
        pub context: u64,
        /// K1: the TLB epoch ([`Control::epoch`]) at which a fetch walk last found its source pages at its linear
        /// address (its placement's, or a revalidation's); 0: never current (an INVLPG of its range).
        pub checked: u64,
        /// Guest-physical source pages (`u64::MAX` = unused).
        pub source_gpa: [u64; 2],
        /// K1: per source page, the block filed from it before this one (number + 1; 0: none), [`PAGE_BLOCKS`]' chains.
        pub next: [u32; 2],
        /// K1: per linear page of its first and last byte, the next block on that page's [`LINEAR_HEADS`] chain.
        pub lnext: [u32; 2],
        /// Offset of the FEX block (its 4-byte `JITCodeHeader` first) in the code area (the step block's: [`SLOT`]), and
        /// its size.
        pub code: u32,
        pub code_size: u32,
        pub sites_count: u32,
        pub guest_bytes: u32,
        /// Its position in `INDEX`.
        pub number: u32,
        /// [`DEAD`], [`STEP`].
        pub flags: u32,
        /// Native offset of its computed exit's `b` to the dispatcher (0: none).
        pub dispatch_exit: u32,
        /// Bit i: link point i is the self-exit of the REP string instruction that ends the block (X2, the host's
        /// [`super::State::rep_exits`]): a stop there is between two iterations, with some elements done.
        pub rep_exits: u32,
        /// Its literal exits as admitted (`offset` 0 = unused).
        pub links: [super::LinkPoint; super::MAX_LINK_POINTS],
    }
    /// `Block::flags`: invalidated (a source page was written): out of L1 and L2, no link into or out of it, never run
    /// again; its space is reclaimed at the next flush.
    pub const DEAD: u32 = 1;
    /// The uncached single-step block, in the slot [`SLOT`] (not in `INDEX`, L1 or L2; never linked; no source page marked).
    pub const STEP: u32 = 2;
    /// K2: a system instruction's decode ([`super::System`], the host's, kept right after the record where a block's sites
    /// are): no code (`code_size` 0, `code` the code area's fill point when placed), never in L1, never entered or linked;
    /// in L2 and invalidated exactly as a block of the same source pages and guest bytes.
    pub const SYSTEM: u32 = 4;
    /// K1: every source page was mapped global (PTE.G with CR4.PGE) when it was last checked: it stays current across
    /// CR3 loads.
    pub const GLOBAL: u32 = 8;
    /// K1: a link may enter it (set when one is made, cleared when the block is cut and the links into it pruned), so
    /// making blocks not current, or dead, prunes the link records only when one of them may be a target.
    pub const INBOUND: u32 = 16;
    impl Block {
        /// Its `JITCodeHeader` in the executable alias.
        pub fn code_va(&self) -> u64 {
            CODE_VA + u64::from(self.code)
        }
        pub fn entry(&self) -> u64 {
            self.code_va() + u64::from(super::ENTRY_OFFSET)
        }
        /// Link point `offset` (a native offset in the block, 0 = none) is the self-exit of the REP string instruction
        /// that ends the block (X2, [`Block::rep_exits`]).
        pub fn rep_exit(&self, offset: u64) -> bool {
            self.links.iter().enumerate().any(|(k, p)| self.rep_exits >> k & 1 != 0 && p.offset != 0 && u64::from(p.offset) == offset)
        }
        /// The block starts with a REP string instruction (X2): one of its self-exits goes to its own RIP.
        pub fn starts_with_rep(&self) -> bool {
            self.links.iter().enumerate().any(|(k, p)| self.rep_exits >> k & 1 != 0 && p.offset != 0 && p.target == self.rip)
        }
    }
    /// A patched link point: the first word of link point `offset` of block `source` (block numbers) was `original`
    /// (the first word of its RIP move) and is now `b` to the entry of block `target`.
    #[repr(C)]
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub struct LinkRecord {
        pub source: u32,
        pub offset: u32,
        pub original: u32,
        pub target: u32,
    }
    /// Invalidation requests to one vCPU, drained at each of its resident entries. Producers only set bits (atomic
    /// OR), the page's `stale` bit, then its word's `summary` bit, before `pending`; the consumer swaps `pending` to 0, then
    /// each `summary` word and the `stale` words it names, so no entry is lost, nothing overflows and a drain visits only
    /// the words posted to (M1).
    #[repr(C)]
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub struct Record {
        /// [`PAGES`], [`FLUSH`], [`SHADOWS`].
        pub pending: u64,
        /// Bit i % 64 of word i / 64: `stale` word i may be nonzero.
        pub summary: [u64; super::MAX_GUEST_PAGES / 64 / 64],
        /// Guest-physical pages whose translations are stale (bit p % 64 of word p / 64).
        pub stale: [u64; super::MAX_GUEST_PAGES / 64],
    }
    /// `Record::pending`: `stale` lists pages; drop the whole cache; reset all shadows (and writable bits).
    pub const PAGES: u64 = 1;
    pub const FLUSH: u64 = 2;
    pub const SHADOWS: u64 = 4;
    /// One vCPU's pending asynchronous x86 events (X2), shared: producers (peers' local APICs, X3; the host's event
    /// sources) set bits with an atomic OR and then ring the vCPU's doorbell; its resident takes them at precise stops
    /// and resident entries (an atomic AND of the bit it accepts). `vectors` is the local APIC's IRR (X3: delivered by
    /// its priority rules, [`crate::lapic::deliverable`]).
    #[repr(C)]
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub struct Events {
        /// The IRR: pending interrupt vectors (bit v % 64 of word v / 64).
        pub vectors: [u64; 4],
        /// 1: an NMI is pending (one pending bit: NMIs posted while one is pending merge, SDM Vol.3 7.7.2 / M64).
        pub nmi: u64,
        /// 1: the host ends the vCPU's run if it is halted with no event it can take ([`crate::execution::stop::HALTED`]).
        pub end: u64,
        /// While the vCPU is halted (HLT, parked in `wfi` in the VM; the host's watchdog treats it as idle): bits 7:0 are 1,
        /// or 2 when its own LAPIC timer can interrupt it (X3: it wakes itself); bits 63:8 hold the `posts` count it read
        /// before it last found no event to take ([`HALTED_SEEN_SHIFT`]). 0 while it runs.
        pub halted: u64,
        /// X3: what senders match an IPI's destination against, written by the owner only ([`crate::lapic::published`]).
        pub apic: u64,
        /// X3: events posted to this vCPU so far (every sender adds 1 after setting its bit, before ringing). A halted vCPU
        /// whose `halted` carries the current count has looked at every event posted to it: with the state 1, nothing
        /// can wake it but a new post (the host's rule for ending halted vCPUs).
        pub posts: u64,
        /// L4a: the TMR (SDM Vol.3 11.8.4): the trigger mode of each vector as last accepted into the IRR, written by the
        /// sender with the IRR bit (set for a level-triggered interrupt, cleared for an edge one, before the IRR bit).
        pub tmr: [u64; 4],
        /// L4a: the level of the LINT0 input (1: the 8259's INT output is asserted), kept current by the host's chipset;
        /// the resident applies its LVT LINT0 entry (ExtINT: the vector from the PIC's INTA cycle, `REQUEST_IRQ`).
        pub lint0: u64,
    }
    /// `Events::halted`: the shift of the `posts` count the vCPU had seen.
    pub const HALTED_SEEN_SHIFT: u32 = 8;

    // The window's 2 MiB (its pages and the shadow pool) precede the areas in a vCPU's block.
    const _: () = assert!(crate::shadow::POOL_OFFSET + crate::shadow::POOL_PAGES * 4096 <= LOOKUP_OFFSET && LOOKUP_OFFSET % BLOCK == 0);
    // Every area lies in PRIVATE..PRIVATE + 1 GiB, 2 MiB aligned, below the guest RAM alias (its own GiBs).
    const _: () = assert!(crate::execution::GUEST_RAM_VA == PRIVATE + (1 << 30));
    const _: () = assert!(CODE_VA + CODE_BYTES_MAX as u64 <= CODE_WRITE_VA);
    const _: () = assert!(CODE_WRITE_VA + CODE_BYTES_MAX as u64 <= META_VA);
    const _: () = assert!(META_VA + META_BYTES as u64 <= LOOKUP_VA);
    const _: () = assert!(LOOKUP_VA + LOOKUP_BYTES as u64 <= GLOBAL_VA);
    const _: () = assert!(GLOBAL_VA + GLOBAL_BYTES as u64 <= PRIVATE + (1 << 30));
    const _: () = assert!(HEAD_BYTES + CODE_BYTES <= CODE_BYTES_MAX && CODE_BYTES % BLOCK == 0);
    const _: () = assert!(MAX_BLOCK_BYTES <= SLOT_BYTES && HEAD_BYTES % 64 == 0);
    const _: () = assert!((CODE_VA | CODE_WRITE_VA | META_VA | LOOKUP_VA | GLOBAL_VA) % BLOCK as u64 == 0);
    const _: () = assert!(CODE_OFFSET % BLOCK == 0 && META_BYTES % BLOCK == 0);
    const _: () = assert!(core::mem::size_of::<Control>() <= L1_OWNERS && L1_OWNERS + 4 * L1_ENTRIES <= L2 && L2 + L2_ENTRIES * 16 <= INDEX);
    const _: () = assert!(WRITABLE_SUMMARY + SUMMARY_BYTES <= PAGE_BLOCKS && PAGE_BLOCKS + 4 * super::MAX_GUEST_PAGES <= LINEAR_HEADS && LINEAR_HEADS + 4 * LINEAR_BUCKETS <= LOOKUP_BYTES);
    const _: () = assert!(L1_MASK == 0xfff && L1_ENTRY_BYTES == 32 && L1_CODE - DISPATCH < 1 << 20); // the dispatcher's form
    const _: () = assert!(INDEX + MAX_BLOCKS * 4 <= LINKS);
    const _: () = assert!(LINKS + MAX_LINKS * core::mem::size_of::<LinkRecord>() <= STEP_RECORD);
    const _: () = assert!(STEP_RECORD + STEP_RECORD_BYTES <= CODE_SUMMARY && STEP_RECORD % core::mem::align_of::<Block>() == 0);
    // The largest step record: its Block and a sites table, which lies inside one staged object (admission).
    const _: () = assert!(core::mem::size_of::<Block>() + crate::execution::STAGING_BYTES <= STEP_RECORD_BYTES);
    const _: () = assert!(WRITABLE_SUMMARY + SUMMARY_BYTES <= LOOKUP_BYTES && SUMMARY_BYTES * 64 == MASK_BYTES);
    const _: () = assert!(MAX_VCPUS <= 4); // a nibble per page
    const _: () = assert!(EVENTS + MAX_VCPUS * core::mem::size_of::<Events>() <= CODE_PAGES && CODE_PAGES % 8 == 0);
    const _: () = assert!(RECORDS + MAX_VCPUS * core::mem::size_of::<Record>() <= GLOBAL_BYTES && MASK_BYTES % 8 == 0);
    const _: () = assert!(RESETS + MAX_VCPUS * 8 <= EVENTS && EVENTS % 64 == 0);
    const _: () = assert!(EVENTS + MAX_VCPUS * core::mem::size_of::<Events>() <= GLOBAL_BYTES);
}

/// Bytes of a vCPU's block of the backing before its code area: its window and the cache's fixed areas.
pub const BACKING_BYTES: usize = cache::CODE_OFFSET;

/// FEX `CpuStateFrame` fields admitted code may access (pinned FEX build).
pub mod frame {
    /// FEX's RFLAGS bytes (`State.flags[]`): the flag of RFLAGS bit `b` is the byte at `FLAGS + b`. FEX keeps the
    /// arithmetic flags in NZCV/x26/x27; the bytes of TF (8), IF (9), DF (10), IOPL (12, the two-bit value), NT (14),
    /// RF (16), VM (17), AC (18), VIF (19), VIP (20) and ID (21) are what `pushf` reads, and DF is what `cld`/`std` write
    /// (FEX: 1 = clear, -1 = set).
    pub const FLAGS: u32 = 0x3f0;
    pub const DF: u32 = FLAGS + 10;
    /// FEX's `gs_cached` and `fs_cached` (`CPUState`, right before the flag bytes): the GS and FS bases FEX adds to every
    /// GS-/FS-prefixed address in 64-bit mode (`OpDispatchBuilder::GetSegment`), 8 bytes each, written by the resident
    /// from the bank at every block entry (I7).
    pub const GS_BASE: u32 = FLAGS - 16;
    pub const FS_BASE: u32 = FLAGS - 8;
    /// x87 (I6, FEX's reduced-precision mode): FEX's flag bytes for FSW.IE (only FEX's own IE: its other exception flags
    /// are in [`X87_EXCEPTIONS`]), C0, C1, C2, C3 (each 0/1) and TOP (0..7); ST(i) as a double in the low 8 bytes of the
    /// 16-byte `mm[(TOP + i) & 7]` (the high 8 are unused); FCW (written from the bank at every block entry, never read back);
    /// the abridged tag word (bit i set: physical register i is not empty); and the x87 exception word, a 4-byte field in
    /// FEX's padding after the tag word, which generated code ORs FPSR into after each x87 floating-point op: bit 0 IE,
    /// 1 ZE, 2 OE, 3 UE, 4 PE, 7 DE (FPSR's IOC DZC OFC UFC IXC IDC), and 6 SF (the resident's stack fault).
    pub const X87_IE: u32 = FLAGS + 32;
    /// FEX's PE flag byte, which FIST/FISTP m16/m32 set (patch 0019: their PE is computed, not FPSR's).
    pub const X87_PE: u32 = FLAGS + 37;
    /// FEX's SF flag byte: generated code's stack faults (patch 0019: IE and SF with the masked response).
    pub const X87_SF: u32 = FLAGS + 38;
    pub const X87_C0: u32 = FLAGS + 40;
    pub const X87_C1: u32 = FLAGS + 41;
    pub const X87_C2: u32 = FLAGS + 42;
    pub const X87_TOP: u32 = FLAGS + 43;
    pub const X87_C3: u32 = FLAGS + 46;
    pub const X87_MM: u32 = FLAGS + 48;
    pub const X87_FCW: u32 = 0x4b0;
    pub const X87_FTW: u32 = 0x4b2;
    pub const X87_EXCEPTIONS: u32 = 0x4b4;
    /// The upper 128 bits of YMM0..YMM15 (I5b; XSAVE state component 2, YMM_Hi128): FEX's `avx_high[16][2]`, where FEX keeps
    /// them without SVE256, two little-endian quadwords per register. Generated code reads and writes them (I8: the VEX forms,
    /// each instruction storing the halves it wrote before the next starts); XSAVE and XRSTOR use the same copy. Zero at
    /// reset with the rest of the page.
    pub const AVX_HIGH: u32 = 0xc0;
    /// Guest-state fields `(offset, bytes)` admitted code may read through x28: the segment bases, the RFLAGS bytes
    /// above, the x87 state (I6) and the YMM upper halves (I8). MXCSR is the bank's. Every other x28 access is refused.
    pub const GUEST_STATE: [(u32, u32); 13] = [
        (AVX_HIGH, 256), (GS_BASE, 8), (FS_BASE, 8), (FLAGS + 8, 3), (FLAGS + 12, 1), (FLAGS + 14, 1), (FLAGS + 16, 6),
        (X87_IE, 1), (X87_PE, 2), (X87_C0, 4), (X87_C3, 1), (X87_MM, 128), (X87_FCW, 8),
    ];
    /// The fields admitted code may also write: DF, the x87 state but FCW, and the YMM upper halves. TF, IF, IOPL, NT, AC, VIF,
    /// VIP and ID are the resident's, and so is FCW (FLDCW, FNINIT, FLDENV, FRSTOR are transactions).
    pub const GUEST_STATE_WRITABLE: [(u32, u32); 8] =
        [(AVX_HIGH, 256), (DF, 1), (X87_IE, 1), (X87_PE, 2), (X87_C0, 4), (X87_C3, 1), (X87_MM, 128), (X87_FTW, 6)];
    pub const BYTES: usize = 4096;
    /// RFLAGS bits held in the flag bytes.
    const BITS: [u32; 10] = [8, 9, 10, 12, 14, 16, 17, 18, 19, 20];
    /// The flag bytes `(offset, value)` for the bank's `rflags`, written by the resident at every block entry: each
    /// flag's bit as 0/1 (IOPL its two-bit value; DF as FEX's 1/-1; RF and VM as 0, which `pushf` clears in its image).
    pub fn flag_bytes(rflags: u64) -> [(u32, u8); 11] {
        let mut out = [(0, 0); 11];
        for (slot, &b) in out.iter_mut().zip(BITS.iter().chain(&[21])) {
            let bit = (rflags >> b & 1) as u8;
            *slot = (FLAGS + b, match b {
                10 => if bit == 1 { 0xff } else { 1 },
                12 => (rflags >> 12 & 3) as u8,
                16 | 17 => 0,
                _ => bit,
            });
        }
        out
    }
    /// RFLAGS.DF from FEX's DF byte.
    pub fn df(byte: u8) -> bool {
        (byte as i8) < 0
    }
}
/// Native offset of a FEX block's entry: after its 4-byte `JITCodeHeader`.
pub const ENTRY_OFFSET: u32 = 4;

/// A literal exit of an admitted block, exactly a move of a proven guest RIP (the block end or its branch's decoded
/// target) into x0 (`movz|movn|orr` then `movk`s) and `svc #LINK`: the native offset of its first word, the only word
/// the resident's linker rewrites (to `b` the entry of the block cached for `target`), that word as admitted, and
/// `target`. Offset 0 (the code header) marks an unused entry.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LinkPoint {
    pub offset: u32,
    pub word: u32,
    pub target: u64,
}
impl LinkPoint {
    pub const NONE: Self = Self { offset: 0, word: 0, target: 0 };
}
/// The value of a logical-immediate word's bitmask (DecodeBitMasks), `None` if reserved.
pub fn bitmask(w: u32) -> Option<u64> {
    let (n, immr, imms) = (w >> 22 & 1, w >> 16 & 63, w >> 10 & 63);
    let len = 31 - (n << 6 | (!imms & 63)).leading_zeros();
    let esize = 1u32.checked_shl(len).filter(|&e| e >= 2 && (w >> 31 == 1 || e <= 32))?;
    let (s, r) = (imms & (esize - 1), immr & (esize - 1));
    if s == esize - 1 {
        return None;
    }
    let ones = (1u64 << (s + 1)) - 1;
    let emask = if esize == 64 { u64::MAX } else { (1 << esize) - 1 };
    let elem = (ones >> r | ones << (esize - r) % esize) & emask;
    Some((0..64 / esize).fold(0, |v, i| v | elem << (i * esize)))
}
/// The constant the move at the start of `words` builds in register `reg` (`movz`, `movn` or `orr #bitmask` from the
/// zero register, then `movk`s of the same width), and its length in words; `None` if the first word is none of those.
pub fn move_value(words: &[u32], reg: u32) -> Option<(usize, u64)> {
    let w = *words.first()?;
    let sf = w >> 31;
    let mask = if sf == 1 { u64::MAX } else { 0xffff_ffff };
    let part = |w: u32| (u64::from(w >> 5 & 0xffff), 16 * (w >> 21 & 3));
    let (imm, sh) = part(w);
    let mut value = match w & 0x7f80_0000 {
        _ if w & 31 != reg => return None,
        0x1280_0000 => !(imm << sh) & mask, // movn
        0x5280_0000 => (imm << sh) & mask,  // movz
        0x3200_0000 if w >> 5 & 31 == 31 => bitmask(w)? & mask, // orr xD, xzr, #bitmask
        _ => return None,
    };
    let n = 1 + words[1..].iter().take_while(|&&k| k & 0xff80_001f == (0x7280_0000 | sf << 31 | reg)).count();
    for &k in &words[1..n] {
        let (imm, sh) = part(k);
        value = (value & !(0xffff << sh) | imm << sh) & mask;
    }
    Some((n, value))
}
/// The placeholder fexc emits for the resident to relocate when it places a block: a computed exit's branch (`b .`,
/// becomes `b` to [`cache::DISPATCH`]).
pub const DISPATCH_PLACEHOLDER: u32 = 0x1400_0000;
/// Link points per block: a slice-1 block ends at one branch (a jcc has two literal exits); admission refuses more.
pub const MAX_LINK_POINTS: usize = 4;
/// The static-register map (FEX `x64::SRA`) admission pins for every block: native registers of RAX..R15, then PF
/// and AF. Guest state is in these registers at every block entry and exit.
pub const SRA: [u8; 18] = [4, 7, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 19, 29, 26, 27];

/// Guest RFLAGS bits slice-1 code computes (CF PF AF ZF SF OF).
pub const ARITHMETIC_FLAGS: u64 = 0x8d5;

/// The vCPU's x86 architectural state; authoritative while no code runs.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Cpu {
    /// RAX RCX RDX RBX RSP RBP RSI RDI R8..R15.
    pub gpr: [u64; 16],
    pub rip: u64,
    pub rflags: u64,
    pub cr0: u64,
    pub cr3: u64,
    pub cr4: u64,
    pub efer: u64,
    /// Current privilege level (CS.RPL in a full segment model).
    pub cpl: u64,
    /// FS.base and GS.base (64-bit mode: the only segment bases that apply, I7). Authoritative here: the resident writes
    /// them into the guest-state page ([`frame::FS_BASE`], [`frame::GS_BASE`]) at every block entry and never reads
    /// them back, so a guest store into the page changes what that vCPU's prefixed accesses use only until the next
    /// `enter`; only the resident changes them (WRFSBASE/WRGSBASE, the MSRs and SWAPGS are its transactions, S3).
    pub fs_base: u64,
    pub gs_base: u64,
    /// CR2 (the page-fault linear address: any 64-bit value; #PF delivery writes it, X1) and, in the field `cr8`, the
    /// local APIC's whole TPR (bits 7:0, X3): CR8 reads and writes its bits 7:4 (SDM Vol.3 11.8.6.1).
    pub cr2: u64,
    pub cr8: u64,
    /// DR0..DR3, DR6, DR7 (MOV DR4/DR5 reach DR6/DR7 while CR4.DE = 0). State only: no breakpoint is armed (S4).
    pub dr: [u64; 6],
    /// ES, CS, SS, DS, FS, GS in the x86 Sreg order ([`SREG_ES`] ..): selector and hidden attributes. 64-bit mode ignores
    /// their bases and limits (flat 64-bit only, plan D1): FS/GS bases are [`Cpu::fs_base`]/[`Cpu::gs_base`], so `base`
    /// stays 0 here; `limit` is recorded. CS.RPL is [`Cpu::cpl`]; CS is a 64-bit code segment ([`Cpu::control`]).
    pub seg: [Segment; 6],
    /// LDTR and TR: selector and the hidden base, limit and attributes LLDT/LTR loaded (from 16-byte descriptors).
    pub ldtr: Segment,
    pub tr: Segment,
    pub gdtr: Table,
    pub idtr: Table,
    /// 1 after a MOV SS (its interrupt and debug shadow, SDM Vol.2 MOV); the next system transaction clears it. X2 owns
    /// its effect and its end at the next instruction boundary inside generated code.
    pub mov_ss: u64,
    /// XMM0..XMM15 (I5a), each as two little-endian quadwords (low, high). In FEX's static vector registers v16..v31 while
    /// generated code runs ([`XMM_VREG`]); banked here at every resident entry, so precise faults hold them too.
    pub xmm: [[u64; 2]; 16],
    /// MXCSR (I5a; SDM Vol.1 10.2.3): authoritative here. Its rounding control, FTZ and DAZ become FPCR at every block entry
    /// ([`fpcr`]); the exception flags FEX's code raises accumulate in FPSR and are or-ed in at the next resident entry
    /// ([`mxcsr_flags`]). LDMXCSR/STMXCSR are the resident's transactions.
    pub mxcsr: u64,
    /// The x87 FPU control word (I6; SDM Vol.1 8.1.5): authoritative here, written into the guest-state page
    /// ([`frame::X87_FCW`]) at every block entry; FLDCW, FNINIT, FLDENV and FRSTOR are the resident's transactions. Every
    /// exception mask stays set (an FCW that clears one is refused). The rest of the x87 state lives in the page.
    pub fcw: u64,
    /// XCR0 (I5b; SDM Vol.1 13.3): written by XSETBV (one of the profile's [`crate::cpu_profile::XCR0_VALID`]), read by XGETBV and
    /// CPUID.0DH.0:EBX, and the requested-feature bitmap's mask for XSAVE/XRSTOR. Reset 1 (x87 only).
    pub xcr0: u64,
}
/// The first native vector register of XMM0 (FEX's x64 `SRAFPR`: XMM0..XMM15 in v16..v31).
pub const XMM_VREG: usize = 16;
/// MXCSR at reset and as Linux sets it (SDM Vol.1 10.2.3.4: all exceptions masked, round to nearest).
pub const MXCSR_RESET: u64 = 0x1f80;
/// MXCSR bits LDMXCSR may set (MXCSR_MASK of a processor with DAZ, SDM Vol.1 11.6.6): a 1 above is #GP(0).
pub const MXCSR_MASK: u64 = 0xffff;
/// MXCSR exception masks (IM DM ZM OM UM PM, bits 12:7).
pub const MXCSR_MASKS: u64 = 0x1f80;
/// FPCR for generated code under `mxcsr` (FEX compiles with FEAT_AFP, DDI0487 FPCR): AH (x86 NaN and min/max rules) and NEP
/// (scalar results keep the vector's upper lanes) always; FIZ = DAZ, FZ = FTZ (with AH, an output flush only), RMode from
/// RC (x86 00 nearest, 01 down, 10 up, 11 toward zero; Arm RN 00, RP 01, RM 10, RZ 11).
pub const fn fpcr(mxcsr: u64) -> u64 {
    let rmode = [0, 2, 1, 3][(mxcsr >> 13 & 3) as usize];
    1 << 1 | 1 << 2 | (mxcsr >> 6 & 1) | (mxcsr >> 15 & 1) << 24 | rmode << 22
}
/// MXCSR exception flags for the FPSR cumulative flags generated code raised under `mxcsr`: IOC -> IE, DZC -> ZE, OFC -> OE,
/// UFC -> UE, IXC -> PE, and IDC -> DE unless DAZ. With FPCR.AH = 1 and FIZ = 0 the M4 sets IDC for a denormal input it
/// does not flush, with x86's precedence (none beside an IE or ZE of the same operation or a NaN operand; not for FP to
/// integer conversions, FRINT* or the reciprocal estimates; the I5a review's measurements, `tools/run_i5a_de.py`). Under DAZ
/// (FIZ = 1) IDC marks a flushed input, where x86 sets no DE.
pub const fn mxcsr_flags(fpsr: u64, mxcsr: u64) -> u64 {
    let de = if mxcsr & MXCSR_DAZ == 0 { fpsr >> 7 & 1 } else { 0 };
    (fpsr & 1) | de << 1 | (fpsr >> 1 & 1) << 2 | (fpsr >> 2 & 1) << 3 | (fpsr >> 3 & 1) << 4 | (fpsr >> 4 & 1) << 5
}
/// MXCSR.DAZ (denormal source operands are zeros: no DE).
pub const MXCSR_DAZ: u64 = 1 << 6;
/// FCW after FNINIT (SDM Vol.1 8.1.5): every exception masked, 64-bit precision, round to nearest. A driver-started vCPU
/// has the FNINIT state (FCW 037FH, FSW 0, every tag empty), as QEMU's and FEX's start; RESET's 0040H/5555H is not built.
pub const FCW_INIT: u64 = 0x37f;
/// FCW's exception masks (IM DM ZM OM UM PM, bits 5:0).
pub const FCW_MASKS: u64 = 0x3f;
/// The FPCR x87 arithmetic runs under for `fcw` (I6): AH and NEP as for SSE, RMode from FCW.RC (bits 11:10, the same
/// encoding as MXCSR.RC), no flush to zero; 0 when it equals MXCSR's ([`fpcr`]), which blocks are entered with.
pub const fn x87_fpcr(fcw: u64, mxcsr: u64) -> u64 {
    let x87 = 1 << 1 | 1 << 2 | [0, 2, 1, 3][(fcw >> 10 & 3) as usize] << 22;
    if x87 == fpcr(mxcsr) { 0 } else { x87 }
}

/// A segment register's visible selector and hidden part. `attributes` are descriptor bits 47:40 (type, S, DPL, P) in
/// 7:0 and bits 55:52 (AVL, L, D/B, G) in 15:12; a null load leaves 0 (P = 0: unusable). `limit` is byte-granular.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Segment {
    pub selector: u16,
    pub attributes: u16,
    pub limit: u32,
    pub base: u64,
}
/// GDTR or IDTR.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Table {
    pub base: u64,
    pub limit: u64,
}
pub const SREG_ES: usize = 0;
pub const SREG_CS: usize = 1;
pub const SREG_SS: usize = 2;
pub const SREG_DS: usize = 3;
pub const SREG_FS: usize = 4;
pub const SREG_GS: usize = 5;
pub const ATTR_P: u16 = 0x80;
pub const ATTR_L: u16 = 1 << 13;
pub const ATTR_D: u16 = 1 << 14;
impl Segment {
    pub const NULL: Self = Self { selector: 0, attributes: 0, limit: 0, base: 0 };
    /// `selector` loaded from the 8-byte descriptor `raw` (SDM Vol.3 3.4.5): its attributes, its limit scaled by G, and
    /// the 32-bit base (the low half of a 16-byte system descriptor's).
    pub const fn loaded(selector: u16, raw: u64) -> Self {
        let limit = (raw & 0xffff | (raw >> 32) & 0xf_0000) as u32;
        let g = raw & 1 << 55 != 0;
        Self {
            selector,
            attributes: (raw >> 40) as u16 & 0xf0ff,
            limit: if g { limit << 12 | 0xfff } else { limit },
            base: (raw >> 16) & 0xff_ffff | (raw >> 56) << 24,
        }
    }
}
/// The segments of a driver-started vCPU at `cpl`, as the pinned QEMU stub (`tools/bench/smp/stub_smp.asm`) enters its
/// bodies: CPL 0 CS 0x08 (64-bit code), SS = DS = ES = 0x10 (data), FS = GS null; CPL 3 CS 0x23, SS 0x1b and the rest
/// null (its IRETQ nulls DS/ES). Flat descriptors, accessed bit set.
pub const fn flat_segments(cpl: u64) -> [Segment; 6] {
    let (code, data) = if cpl == 3 {
        (Segment::loaded(0x23, 0x00af_fb00_0000_ffff), Segment::loaded(0x1b, 0x00cf_f300_0000_ffff))
    } else {
        (Segment::loaded(0x08, 0x00af_9b00_0000_ffff), Segment::loaded(0x10, 0x00cf_9300_0000_ffff))
    };
    let other = if cpl == 3 { Segment::NULL } else { data };
    [other, code, data, other, Segment::NULL, Segment::NULL]
}
/// GDTR and IDTR at reset (SDM Vol.3 Table 10-1: base 0, limit FFFFH).
pub const TABLE_RESET: Table = Table { base: 0, limit: 0xffff };
/// LDTR and TR at reset (SDM Vol.3 Table 10-1: selector 0, base 0, limit FFFFH, present; an LDT and a busy TSS). The
/// QEMU stub never loads LDTR, so a TI = 1 selector reaches this LDT there too.
pub const LDTR_RESET: Segment = Segment { selector: 0, attributes: 0x82, limit: 0xffff, base: 0 };
pub const TR_RESET: Segment = Segment { selector: 0, attributes: 0x8b, limit: 0xffff, base: 0 };

pub const CR0_PE: u64 = 1;
/// MP, EM, TS (SDM Vol.3 2.5, Table 2-2): recorded; they gate x87/MMX/SSE (#NM, #UD), which no admitted form is yet (I5, I6).
pub const CR0_MP: u64 = 1 << 1;
pub const CR0_EM: u64 = 1 << 2;
pub const CR0_TS: u64 = 1 << 3;
/// Alignment mask: with RFLAGS.AC at CPL 3, alignment checking (#AC), which is not built: [`Cpu::control`] refuses it.
pub const CR0_AM: u64 = 1 << 18;
pub const RFLAGS_AC: u64 = 1 << 18;
/// Hard-wired to 1 (SDM vol. 3 §2.5); writes are ignored.
pub const CR0_ET: u64 = 1 << 4;
pub const CR0_WP: u64 = 1 << 16;
pub const CR0_PG: u64 = 1 << 31;
pub const CR4_PAE: u64 = 1 << 5;
pub const CR4_SMEP: u64 = 1 << 20;
pub const CR4_SMAP: u64 = 1 << 21;
pub const CR4_LA57: u64 = 1 << 12;
pub const CR4_PGE: u64 = 1 << 7;
pub const CR4_PCIDE: u64 = 1 << 17;
/// Debug extensions: MOV DR4/DR5 raise #UD instead of aliasing DR6/DR7 (SDM Vol.3 17.2.2).
pub const CR4_DE: u64 = 1 << 3;
/// RDPMC allowed at CPL > 0.
pub const CR4_PCE: u64 = 1 << 8;
/// SSE enabled (FXSAVE/FXRSTOR save XMM, SSE forms run; else #UD) and unmasked SIMD exceptions as #XM (else #UD).
pub const CR4_OSFXSR: u64 = 1 << 9;
pub const CR4_OSXMMEXCPT: u64 = 1 << 10;
/// CR4.OSXSAVE (I5b): XSAVE/XRSTOR, XGETBV and XSETBV are #UD without it; CPUID.1:ECX.OSXSAVE reads it.
pub const CR4_OSXSAVE: u64 = 1 << 18;
/// DR6 and DR7 in 64-bit mode (SDM Vol.3 17.2.3, 17.2.4; no RTM or bus-lock detection in the profile): bits 63:32
/// reserved (#GP on a write of 1); DR6 writable B0..B3, BD, BS, BT, the rest fixed (1 in 31:16 and 11:4, 0 in 12); DR7
/// writable L0..G3, LE, GE, GD and the R/W, LEN fields, bit 10 fixed 1, bits 11, 12, 14, 15 fixed 0.
pub const DR6_FIXED: u64 = 0xffff_0ff0;
pub const DR6_WRITABLE: u64 = 0xe00f;
pub const DR6_BD: u64 = 1 << 13;
/// DR6.BS (S4): the #DB is a single-step trap (RFLAGS.TF at the start of the instruction that completed).
pub const DR6_BS: u64 = 1 << 14;
pub const DR7_FIXED: u64 = 0x400;
pub const DR7_WRITABLE: u64 = 0xffff_23ff;
pub const DR7_GD: u64 = 1 << 13;
/// DR7 L0..L3, G0..G3: a set bit arms a breakpoint (S4).
pub const DR7_ENABLES: u64 = 0xff;
/// S4: whether DR7 arms breakpoint `n` (L or G: no task switches in IA-32e mode, so both mean the same) and its R/W field
/// (00 execute, 01 write, 10 I/O with CR4.DE, 11 read/write; SDM Vol.3 18.2.4).
pub fn dr7_enabled(dr7: u64, n: usize) -> bool {
    dr7 >> (2 * n) & 3 != 0
}
pub fn dr7_rw(dr7: u64, n: usize) -> u64 {
    dr7 >> (16 + 4 * n) & 3
}
/// S4: the bytes breakpoint `n` covers for a data or I/O breakpoint (SDM Vol.3 18.2.5): LEN 00, 01, 11, 10 = 1, 2, 4, 8
/// bytes, the address's low bits masked by the length (a range is aligned to its length).
pub fn dr_range(dr: &[u64; 6], n: usize) -> (u64, u64) {
    let len = [1, 2, 8, 4][(dr[5] >> (18 + 4 * n) & 3) as usize];
    (dr[n] & !(len - 1), len)
}
/// S4: the DR6 B bits of the armed data breakpoints an access of `width` bytes at linear `va` meets: any byte of the
/// access inside the breakpoint's range, R/W 01 for a write, 11 for a read or a write (SDM Vol.3 18.2.4, 18.2.5). LE and
/// GE are ignored (breakpoints are exact on these processors).
pub fn data_hits(dr: &[u64; 6], va: u64, width: u64, write: bool) -> u64 {
    (0..4).filter(|&n| dr7_enabled(dr[5], n) && matches!((dr7_rw(dr[5], n), write), (1, true) | (3, _))).fold(0, |m, n| {
        let (base, len) = dr_range(dr, n);
        m | u64::from(base.wrapping_sub(va) < width || va.wrapping_sub(base) < len) << n
    })
}
/// [`Cpu::dr`] at reset (SDM Vol.3 Table 10-1: DR0..DR3 0, DR6 FFFF0FF0H, DR7 400H).
pub const DR_RESET: [u64; 6] = [0, 0, 0, 0, DR6_FIXED, DR7_FIXED];
/// CR3 bits a 4-level walk ignores without PCIDE (kept in CR3): 2:0 and 11:5. PWT/PCD (4:3) are not ignored.
pub const CR3_IGNORED: u64 = 0xfe7;
pub const EFER_SCE: u64 = 1;
pub const EFER_LME: u64 = 1 << 8;
pub const EFER_LMA: u64 = 1 << 10;
pub const EFER_NXE: u64 = 1 << 11;

impl Cpu {
    /// Walk controls for 4-level long-mode paging; `None` for any other mode
    /// (paging off, legacy/PAE, LA57, SMAP: not supported by this slice), and
    /// for a CR3 with PWT/PCD or bits above the physical-address width set (memory types of the
    /// root table are not modelled; refused rather than ignored). [`CR3_IGNORED`] bits are ignored.
    /// Alignment checking (CR0.AM, RFLAGS.AC, CPL 3; SDM Vol.3 7.15 #AC) runs since X2: generated code then runs with
    /// SCTLR_EL1.A, and the resident's explicit CPL 3 accesses check it.
    pub fn control(&self) -> Option<Control> {
        let long = self.cr0 & (CR0_PE | CR0_PG) == CR0_PE | CR0_PG
            && self.cr4 & CR4_PAE != 0
            && self.cr4 & (CR4_LA57 | CR4_SMAP) == 0
            && self.efer & (EFER_LME | EFER_LMA) == EFER_LME | EFER_LMA;
        // 64-bit mode: CS.L = 1, CS.D = 0 (compatibility mode is not built, owner decision 3).
        let bits64 = self.seg[SREG_CS].attributes & (ATTR_L | ATTR_D) == ATTR_L;
        (long && bits64 && matches!(self.cpl, 0 | 3) && self.cr3 & !(paging::PA_MASK | CR3_IGNORED) == 0).then_some(Control {
            cr3: self.cr3 & paging::PA_MASK,
            cpl: self.cpl,
            wp: u64::from(self.cr0 & CR0_WP != 0),
            nxe: u64::from(self.efer & EFER_NXE != 0),
            smep: u64::from(self.cr4 & CR4_SMEP != 0),
        })
    }
    /// x86 alignment checking is on (SDM Vol.3 7.15 #AC: CR0.AM, RFLAGS.AC and CPL 3; X2).
    pub fn alignment_check(&self) -> bool {
        self.cr0 & CR0_AM != 0 && self.rflags & RFLAGS_AC != 0 && self.cpl == 3
    }
    /// S4: whether DR7 arms a data breakpoint (R/W 01 or 11).
    pub fn data_breakpoints(&self) -> bool {
        (0..4).any(|n| dr7_enabled(self.dr[5], n) && dr7_rw(self.dr[5], n) & 1 != 0)
    }
    /// Whether BT/BTS/BTR/BTC memory forms are cut as system instructions (`--alignment-gated`): under alignment checking
    /// (X2), and while a data breakpoint is armed (S4: FEX accesses only the byte holding the bit, x86 the operand-sized
    /// unit, which the resident checks).
    pub fn bit_tests_cut(&self) -> bool {
        self.alignment_check() || self.data_breakpoints()
    }
    /// S4: the linear addresses of the armed instruction breakpoints (DR7 Ln/Gn with R/W = 00; DRn, matched against RIP:
    /// CS.base is 0 in 64-bit mode). LEN is ignored: the SDM requires 00 for them and leaves other values undefined
    /// (Vol.3 18.2.5); this matches the address exactly, as pinned QEMU does. The host ends every block before each
    /// one, so each is the first instruction of whatever runs it, and the resident checks it there.
    pub fn code_breakpoints(&self) -> impl Iterator<Item = u64> + '_ {
        (0..4).filter(|&n| dr7_enabled(self.dr[5], n) && dr7_rw(self.dr[5], n) == 0).map(|n| self.dr[n])
    }
    /// What code at a RIP is compiled and cached under besides the RIP and its source bytes (K1: not CR3; a block's
    /// guest-physical source pages are checked against the current tables instead): a word of the CPL, the code mode
    /// (bit 2: 64-bit, the only mode yet), and the SSE gating bits (I5a: CR0.EM, CR0.TS, CR4.OSFXSR; a translation cuts every
    /// SSE form when they gate it, so a block is never entered under other gating than it was made for, even should a
    /// control change ever keep the code), CR0.MP (I6: WAIT's gating with TS), the x87 rounding when it differs from MXCSR's
    /// (bit 14 and FCW.RC in 16:15: a translation runs x87 arithmetic under that FPCR), and alignment checking (X2: a
    /// translation under it cuts every BT* memory form,
    /// and RFLAGS.AC changes without dropping code).
    /// I8: the VEX AVX forms run (SDM Vol.1 13.2, Vol.2 2.8: no #UD) only with CR4.OSXSAVE and XCR0[2:1] = 11b; CR0.TS still
    /// makes them #NM. Part of [`Self::code_context`] (XSETBV changes XCR0 without a flush).
    pub fn avx_enabled(&self) -> bool {
        self.cr4 & CR4_OSXSAVE != 0 && self.xcr0 & 6 == 6
    }
    pub fn code_context(&self) -> u64 {
        let bit = |on: bool, n: u32| u64::from(on) << n;
        let word = self.cpl & 3
            | bit(true, 2)
            // (Bits 3-8 held CR0.PG/WP, CR4.PGE/PCIDE/SMEP and EFER.NXE before K1: a change of one is a global TLB flush,
            // after which a block's fetch is walked again under the new controls before it runs.)
            | bit(self.cr0 & CR0_EM != 0, 9)
            | bit(self.cr0 & CR0_TS != 0, 10)
            | bit(self.cr4 & CR4_OSFXSR != 0, 11)
            | bit(self.bit_tests_cut(), 12)
            | bit(self.cr0 & CR0_MP != 0, 13)
            | (x87_fpcr(self.fcw, self.mxcsr) != 0) as u64 * (1 | (self.fcw >> 10 & 3) << 1) << 14
            | bit(self.avx_enabled(), 17);
        word
    }
}

/// FEX's flag registers for guest `rflags`: (NZCV with C = !CF, PF register, AF register).
/// FEX keeps PF as a byte whose parity is the flag (PF = parity((pf & 0xff) ^ 1)) and
/// AF as bit 4 of `af ^ pf`.
pub fn native_flags(rflags: u64) -> (u64, u64, u64) {
    let bit = |n: u32| (rflags >> n) & 1;
    let nzcv = bit(7) << 31 | bit(6) << 30 | (bit(0) ^ 1) << 29 | bit(11) << 28;
    let pf = bit(2) ^ 1;
    (nzcv, pf, (bit(4) << 4) ^ pf)
}
/// Guest RFLAGS after code ran: `rflags` with its arithmetic flags replaced from
/// FEX's registers; `cf_inverted` as recorded for the location (block exits: true).
pub fn guest_flags(rflags: u64, nzcv: u64, pf: u64, af: u64, cf_inverted: bool) -> u64 {
    let flags = (((nzcv >> 29) & 1) ^ u64::from(cf_inverted))
        | u64::from((pf as u8 ^ 1).count_ones() & 1) << 2
        | ((af ^ pf) & 16)
        | ((nzcv >> 30) & 1) << 6
        | ((nzcv >> 31) & 1) << 7
        | ((nzcv >> 28) & 1) << 11;
    (rflags & !ARITHMETIC_FLAGS) | flags
}

/// `State::request` / `State::reply`.
pub const NONE: u64 = 0;
pub const REQUEST_TRANSLATE: u64 = 1;
/// Translate only the instruction at `cpu.rip` (`max_instructions = 1`) for the single-step slot.
pub const REQUEST_STEP: u64 = 2;
pub const REPLY_ADMITTED: u64 = 1;
/// The host ends the vCPU's run at this request (its stop is the host's: `stop_at` reached, a fetch fault, no code):
/// the resident halts with [`stop::PARKED`].
pub const REPLY_PARK: u64 = 2;
/// The instruction at `cpu.rip` is a system instruction, decoded by the host into `State::system` from the bytes it
/// was asked to translate; nothing is staged. The resident executes it as a transaction ([`system`]); for a translate
/// request it first keeps the decode in its code cache as a block without code ([`cache::SYSTEM`], K2), so the
/// instruction is decoded once per translation, like a block, and later runs need no host exit.
pub const REPLY_SYSTEM: u64 = 3;
/// The bytes of `cpu.rip`'s page ran out before the block (or the instruction) ended: the resident walks the next page
/// for an instruction fetch (marking it, for a translate request) and asks again with its result in `State::fetch` /
/// `State::fetch_fault`.
pub const REPLY_NEXT_PAGE: u64 = 4;
/// The instruction fetch at `cpu.rip` faults (`State::fetch_fault`: RIP's page, or the next one an instruction crossing
/// into it needs): nothing is staged; the resident delivers the exception (X1a).
pub const REPLY_FETCH_FAULT: u64 = 5;
/// M3 (numbered apart from the other chunks' kinds): complete the device access described by the [`Mmio`] record; the
/// host answers [`REPLY_DEVICE`] with the record's reply half.
pub const REQUEST_DEVICE: u64 = 0x30;
pub const REPLY_DEVICE: u64 = 0x30;
/// K2: the vCPU's entropy pool ([`Entropy`]) is empty at an RDRAND: the host tops it up (as at every exit) and answers
/// [`REPLY_ENTROPY`].
pub const REQUEST_ENTROPY: u64 = 0x31;
pub const REPLY_ENTROPY: u64 = 0x31;
/// L3: one I/O port access described by the [`Mmio`] record (`gpa` holds the port, 0..=0xffff); the host performs it on
/// its port bus (devices, or the unassigned-port rule) and answers [`REPLY_PORT`] with the record's reply half.
pub const REQUEST_PORT: u64 = 0x32;
pub const REPLY_PORT: u64 = 0x32;
/// L3 (amendment): a read touched the guest-physical page `Mmio::gpa` that is not RAM and that no resident device claims:
/// the host answers [`REPLY_PAGE`] with `Mmio::status` [`mmio::DONE`] (unassigned: the resident shadows it read-only onto
/// the shared all-ones page, `crate::ALL_ONES_IPA`; the host counts the fill), [`mmio::CLAIMED`] (a device's: the device
/// path) or [`mmio::UNASSIGNED`] (unassigned under the strict switch: refused).
pub const REQUEST_PAGE: u64 = 0x33;
pub const REPLY_PAGE: u64 = 0x33;
/// L4a: the interrupt controllers, through the [`Mmio`] record: `write` 1, the EOI broadcast of the level-triggered
/// vector in `value` (a LAPIC's EOI of a vector whose TMR bit is set); `write` 0, the INTA cycle of an ExtINT taken from
/// LINT0, the 8259's vector returned in `value`. The host answers [`REPLY_IRQ`] with `status` [`mmio::DONE`].
pub const REQUEST_IRQ: u64 = 0x34;
pub const REPLY_IRQ: u64 = 0x34;

/// A request's instruction fetch, walked by the resident (`State::fetch_fault`): why the walk of the last page it walked
/// failed — kind, then the #PF address and error code for [`fetch::PAGE`].
pub mod fetch {
    use crate::paging::Fault;
    pub const NONE: u64 = 0;
    pub const PAGE: u64 = 1;
    pub const NONCANONICAL: u64 = 2;
    pub const UNSUPPORTED: u64 = 3;
    /// The page is not write-back memory or lies outside guest RAM (code is fetched only from guest RAM).
    pub const NOT_RAM: u64 = 4;
    pub fn encode(f: Fault) -> [u64; 3] {
        match f {
            Fault::Page { address, error } => [PAGE, address, error],
            Fault::Noncanonical => [NONCANONICAL, 0, 0],
            Fault::Unsupported | Fault::Contended => [UNSUPPORTED, 0, 0],
        }
    }
    /// The walk's fault ([`NOT_RAM`] and [`NONE`] have none).
    pub fn decode([kind, address, error]: [u64; 3]) -> Option<Fault> {
        match kind {
            PAGE => Some(Fault::Page { address, error }),
            NONCANONICAL => Some(Fault::Noncanonical),
            UNSUPPORTED => Some(Fault::Unsupported),
            _ => None,
        }
    }
}

/// A system instruction (FEX cuts every block before one) as the host decoded it, for the resident to execute with
/// x86 semantics: privilege, prefix and operand checks, the architectural effect, RIP past the instruction (`length`
/// bytes) and the invalidation its semantics require. RFLAGS are unchanged. Register numbers are x86 (RAX = 0 .. R15).
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct System {
    /// [`system::MOV_TO_CR`] ..
    pub kind: u32,
    pub length: u32,
    /// The instruction has a LOCK prefix (#UD).
    pub lock: u32,
    /// MOV CR/DR: the control or debug register number (0..15, REX.R included) and the general register. MOV to/from
    /// Sreg: the segment register (ModRM.reg, 0..7; 6, 7 and MOV to CS are #UD). An r/m operand that is a register
    /// (`memory` = 0): `reg`.
    pub cr: u32,
    pub reg: u32,
    /// A memory operand (INVLPG, descriptor-table and segment forms, far JMP/CALL): base register ([`system::RIP`] for
    /// RIP-relative, [`system::NO_REG`] for none), index register ([`system::NO_REG`]: none), scale (a shift, 0..3) and
    /// displacement; 64-bit address size. RET far: `disp` is the imm16 released.
    pub base: u32,
    pub index: u32,
    pub scale: u32,
    pub disp: i64,
    /// 1: the r/m operand is the memory operand above.
    pub memory: u32,
    /// Operand size in bytes where the form has one: a register written by MOV r, Sreg / SLDT / STR (2: its low 16 bits;
    /// 4, 8: the selector zero-extended), the offset of a far pointer and the width of RET far / IRET pops (4 or 8).
    pub size: u32,
    /// A segment override on the memory operand: [`SREG_FS`], [`SREG_GS`] (its base is added); [`SREG_SS`] (an SS prefix)
    /// or [`SREG_DS`] (CS, DS or ES), which add nothing in 64-bit mode but make the reference SS- or DS-class for a
    /// non-canonical address (#SS(0) or #GP(0), Intel SDM Vol. 3A 4.5.1); else 0 (the operand's default: SS for an RSP or
    /// RBP base).
    pub segment: u32,
    /// L3, INS/OUTS: [`system::REP`] (an F3 prefix) and [`system::ADDRESS32`] (a 0x67 prefix: ESI/EDI and ECX); else 0.
    pub prefixes: u32,
}
pub mod system {
    /// CR `cr` := GPR `reg`; GPR `reg` := CR `cr`.
    pub const MOV_TO_CR: u32 = 1;
    pub const MOV_FROM_CR: u32 = 2;
    pub const INVLPG: u32 = 3;
    /// EDX:EAX := MSR ECX; MSR ECX := EDX:EAX (the profile's MSRs, [`crate::cpu`]).
    pub const RDMSR: u32 = 4;
    pub const WRMSR: u32 = 5;
    /// Test drivers only (no x86 instruction; the host's decoder never yields it): the `length`-byte instruction at RIP
    /// is replaced by a change of the CPL to `cr` (0 or 3) at any CPL, through the resident's general CPL change.
    pub const SET_CPL: u32 = 6;
    /// CPUID (any CPL, serialising), RDTSC and RDTSCP (#GP at CPL > 0 with CR4.TSD).
    pub const CPUID: u32 = 7;
    pub const RDTSC: u32 = 8;
    pub const RDTSCP: u32 = 9;
    /// DR `cr` := GPR `reg`; GPR `reg` := DR `cr`.
    pub const MOV_TO_DR: u32 = 10;
    pub const MOV_FROM_DR: u32 = 11;
    /// RDPMC (the profile has no performance counters: always #GP(0) or #UD).
    pub const RDPMC: u32 = 12;
    /// LGDT/LIDT m16&64, SGDT/SIDT m (memory operand). The resident dispatches these by ranges of kinds (the runtime
    /// may not use jump tables, `tools/check_resident_stack.py`): keep each group contiguous.
    pub const LGDT: u32 = 13;
    pub const LIDT: u32 = 14;
    pub const SGDT: u32 = 15;
    pub const SIDT: u32 = 16;
    /// SLDT/STR r/m and MOV r/m := Sreg `cr` (`size` for a register destination).
    pub const SLDT: u32 = 17;
    pub const STR: u32 = 18;
    pub const MOV_FROM_SREG: u32 = 19;
    /// LLDT/LTR r/m16 and MOV Sreg `cr` := r/m16.
    pub const LLDT: u32 = 20;
    pub const LTR: u32 = 21;
    pub const MOV_TO_SREG: u32 = 22;
    /// JMP/CALL far through a memory m16:`size` pointer.
    pub const JMP_FAR: u32 = 23;
    pub const CALL_FAR: u32 = 24;
    /// RET far (`size`-byte pops, `disp` released) and IRET (`size`-byte pops).
    pub const RET_FAR: u32 = 25;
    pub const IRET: u32 = 26;
    /// A LOCK prefix on an instruction that is not lockable (I3; SDM Vol.2 "LOCK"): #UD, whatever the instruction (fexc
    /// cuts it as a system instruction; `lock` is 1, `length` is not used: the fault leaves RIP at the instruction).
    pub const LOCK_UD: u32 = 0xf0;
    /// Software interrupts and #UD instructions (X1a), delivered through the IDT by the resident: INT n (`cr` = n,
    /// `length` past the immediate), INT3 (`cc`), INTO (`ce`: #UD in 64-bit mode) and UD0/UD1/UD2 (`0f ff`, `0f b9`,
    /// `0f 0b`: #UD; their ModRM is not decoded, `length` is not used: the fault leaves RIP at the instruction).
    pub const INT: u32 = 27;
    pub const INT3: u32 = 28;
    pub const INTO: u32 = 29;
    pub const UD: u32 = 30;
    /// LDMXCSR/STMXCSR m32 (I5a; `0f ae /2`, `/3`): MXCSR from or to the memory operand, gated like any SSE form.
    pub const LDMXCSR: u32 = 31;
    pub const STMXCSR: u32 = 32;
    /// An SSE instruction under CR0.EM, CR0.TS or without CR4.OSFXSR (I5a; fexc cut it, `--sse-gated`): #UD or #NM from the
    /// bank's controls (`length` is not used: the fault leaves RIP at the instruction).
    pub const SSE_GATED: u32 = 33;
    /// A VEX-encoded AVX/AVX2/FMA/F16C instruction without CR4.OSXSAVE, with XCR0[2:1] != 11b, or under CR0.TS (I8; fexc cut
    /// it, `--avx-gated`): #UD, else #NM, from the bank's controls (SDM Vol.2 2.8; `length` is not used).
    pub const AVX_GATED: u32 = 49;
    /// VLDMXCSR/VSTMXCSR m32 (I8; `VEX.L0.0F.WIG ae /2`, `/3`): LDMXCSR/STMXCSR's transaction without their CR0.EM/CR4.OSFXSR
    /// gating (fexc cut them under the AVX gating, `--avx-gated`, which the resident raises as [`AVX_GATED`]).
    pub const VLDMXCSR: u32 = 50;
    pub const VSTMXCSR: u32 = 51;
    /// An MMX instruction (EMMS included; I9: fexc cut it, `--x87-gated`) under CR0.EM (#UD) or CR0.TS (#NM), from the bank's
    /// controls (SDM Vol.3 Table 12-1; CR4.OSFXSR does not gate MMX forms; `length` is not used).
    pub const MMX_GATED: u32 = 52;
    /// S3: SWAPGS (`0f 01 f8`), SYSCALL (`0f 05`), SYSRET (`0f 07`, `size` 8 with REX.W, else 4: compatibility mode).
    pub const SWAPGS: u32 = 34;
    pub const SYSCALL: u32 = 35;
    pub const SYSRET: u32 = 36;
    /// RDFSBASE/RDGSBASE/WRFSBASE/WRGSBASE `reg` (`f3 0f ae /0../3`, register; `size` 4 or 8).
    pub const RDFSBASE: u32 = 37;
    pub const RDGSBASE: u32 = 38;
    pub const WRFSBASE: u32 = 39;
    pub const WRGSBASE: u32 = 40;
    /// LAR/LSL GPR `cr` := r/m16 (`0f 02`, `0f 03`; `size` of the destination); VERR/VERW r/m16 (`0f 00 /4`, `/5`).
    pub const LAR: u32 = 41;
    pub const LSL: u32 = 42;
    pub const VERR: u32 = 43;
    pub const VERW: u32 = 44;
    /// RDRAND `reg` (`0f c7 /6`, register; `size` 2, 4 or 8): a value of the entropy service ([`super::Entropy`]).
    pub const RDRAND: u32 = 45;
    /// WBINVD, INVD and WBNOINVD (`0f 09`, `0f 08`, `f3 0f 09`): cache maintenance without an architectural memory effect.
    pub const WBINVD: u32 = 46;
    /// CLFLUSH m8 (`0f ae /7`, memory).
    pub const CLFLUSH: u32 = 47;
    /// An instruction that needs a 16th byte (fexc `instruction_too_long`): #GP(0) at it (SDM Vol.3 Interrupt 13,
    /// "Exceeding the instruction length limit of 15 bytes"; `length` is not used). A fetch fault within its first 15 bytes
    /// comes first (fexc reports `partial_decode` there); the 16th byte is never fetched (natemu policy).
    pub const TOO_LONG: u32 = 48;
    /// `System::base` of a RIP-relative operand (relative to the next instruction).
    pub const RIP: u32 = 16;
    pub const NO_REG: u32 = 0xff;
    /// X2 (numbered apart from the other chunks' kinds): CLI (`fa`), STI (`fb`), POPF (`9d`; `size` 8, or 2 with a 66
    /// prefix: 64-bit mode has no 32-bit form) and HLT (`f4`).
    pub const CLI: u32 = 0x40;
    pub const STI: u32 = 0x41;
    pub const POPF: u32 = 0x42;
    pub const HLT: u32 = 0x43;
    /// A test driver's event source (never decoded from x86): the one-byte instruction at RIP, a `nop`, where the host
    /// posted an event into the vCPU's [`super::cache::Events`] and rang its doorbell before answering; the transaction
    /// only moves RIP past it.
    pub const EVENT_HOOK: u32 = 0x44;
    /// BT/BTS/BTR/BTC with a memory operand (`0f a3/ab/b3/bb /r`, `0f ba /4../7 ib`) under alignment checking (X2: fexc cut
    /// it, `--alignment-gated`): `cr` 0..3 = BT, BTS, BTR, BTC; `reg` the bit-offset register, or [`NO_REG`] for the imm8
    /// form; `size` the operand size (2, 4, 8). The resident checks the operand's alignment (#AC(0)), then steps it.
    pub const BT_ALIGNED: u32 = 0x45;
    /// S4: INT1 (`f1`, ICEBP): #DB delivered as a trap (RIP past it) with EXT = 1 and no gate DPL check (a privileged
    /// software exception, SDM Vol.2 INT n/INTO/INT3/INT1); DR6 unchanged.
    pub const INT1: u32 = 0x46;
    /// x87 (I6, numbered apart): an x87 instruction under CR0.EM or CR0.TS, or WAIT under CR0.TS and CR0.MP (fexc cut it,
    /// `--x87-gated`/`--wait-gated`): #NM (`length` is not used). FNINIT (`db e3`), FNCLEX (`db e2`), FLDCW m16 (`d9 /5`),
    /// FLDENV/FNSTENV m (`d9 /4`, `/6`; `size` 2 with a 66 prefix: the 14-byte image, else 4: 28 bytes), FRSTOR/FNSAVE m
    /// (`dd /4`, `/6`; 94 or 108 bytes), FLD/FSTP m80 (`db /5`, `/7`).
    pub const X87_GATED: u32 = 0x48;
    pub const FNINIT: u32 = 0x49;
    pub const FNCLEX: u32 = 0x4a;
    pub const FLDCW: u32 = 0x4b;
    pub const FLDENV: u32 = 0x4c;
    pub const FNSTENV: u32 = 0x4d;
    pub const FRSTOR: u32 = 0x4e;
    pub const FNSAVE: u32 = 0x4f;
    pub const FLD80: u32 = 0x50;
    pub const FSTP80: u32 = 0x51;
    /// I5b (numbered apart: 0x68..=0x6f): FXSAVE/FXRSTOR m512 (`0f ae /0`, `/1`; REX.W's FXSAVE64 image is the same here:
    /// the x87 pointers are 0), XSAVE/XRSTOR m (`0f ae /4`, `/5`, standard format; EDX:EAX the instruction mask), XGETBV and
    /// XSETBV (`0f 01 d0`, `d1`; ECX the XCR). The memory forms have a 64-bit address and no 66/F2/F3.
    pub const FXSAVE: u32 = 0x68;
    pub const FXRSTOR: u32 = 0x69;
    pub const XSAVE: u32 = 0x6a;
    pub const XRSTOR: u32 = 0x6b;
    pub const XGETBV: u32 = 0x6c;
    pub const XSETBV: u32 = 0x6d;
    /// Port I/O (L3, numbered apart; SDM Vol.2 IN, OUT, INS, OUTS; Vol.1 19): `size` 1, 2 or 4 (66: 2; REX.W does not
    /// promote); IN/OUT name their port by DX (`reg` 2) or an imm8 (`reg` [`NO_REG`], the port in `cr`); INS/OUTS move one
    /// element per transaction between DX's port and ES:RDI (INS) or DS:RSI (OUTS; `segment` an FS/GS override), with
    /// [`super::System::prefixes`].
    pub const IN: u32 = 0x60;
    pub const OUT: u32 = 0x61;
    pub const INS: u32 = 0x62;
    pub const OUTS: u32 = 0x63;
    /// [`super::System::prefixes`] bits.
    pub const REP: u32 = 1;
    pub const ADDRESS32: u32 = 2;
    impl super::System {
        pub const NONE: Self = Self {
            kind: 0, length: 0, lock: 0, cr: 0, reg: 0, base: NO_REG, index: NO_REG, scale: 0, disp: 0, memory: 0, size: 0, segment: 0, prefixes: 0,
        };
        /// Field ranges the resident relies on (it checks every reply).
        pub fn well_formed(&self) -> bool {
            let reg = |r: u32| r < 16;
            let address = (reg(self.base) || self.base == RIP || self.base == NO_REG)
                && (reg(self.index) || self.index == NO_REG)
                && self.scale <= 3
                && (self.base != RIP || self.index == NO_REG)
                && matches!(self.segment as usize, 0 | super::SREG_SS | super::SREG_DS | super::SREG_FS | super::SREG_GS);
            let rm = if self.memory == 1 { address } else { self.memory == 0 && reg(self.reg) && self.segment == 0 };
            (1..=15).contains(&self.length)
                && self.lock <= 1
                && (self.prefixes == 0 || matches!(self.kind, INS | OUTS))
                && match self.kind {
                    MOV_TO_CR | MOV_FROM_CR | MOV_TO_DR | MOV_FROM_DR => reg(self.cr) && reg(self.reg),
                    INVLPG => address && self.segment == 0,
                    RDMSR | WRMSR | CPUID | RDTSC | RDTSCP | RDPMC => true,
                    SET_CPL => matches!(self.cr, 0 | 3),
                    LGDT..=SIDT => self.memory == 1 && address,
                    SLDT..=MOV_FROM_SREG => rm && self.cr < 8 && matches!(self.size, 2 | 4 | 8),
                    LLDT..=MOV_TO_SREG => rm && self.cr < 8,
                    JMP_FAR..=CALL_FAR => self.memory == 1 && address && matches!(self.size, 4 | 8),
                    RET_FAR..=IRET => matches!(self.size, 4 | 8) && (self.kind == IRET || (0..=0xffff).contains(&self.disp)),
                    LOCK_UD => self.lock == 1,
                    LDMXCSR | STMXCSR | VLDMXCSR | VSTMXCSR => self.memory == 1 && address,
                    SSE_GATED | AVX_GATED | MMX_GATED => true,
                    INT => self.cr < 256,
                    INT3 | INT1 | INTO | UD | TOO_LONG => true,
                    SWAPGS | SYSCALL | WBINVD => true,
                    SYSRET => matches!(self.size, 4 | 8),
                    RDFSBASE..=WRGSBASE => self.memory == 0 && reg(self.reg) && self.segment == 0 && matches!(self.size, 4 | 8),
                    LAR | LSL => rm && reg(self.cr) && matches!(self.size, 2 | 4 | 8),
                    VERR | VERW => rm,
                    RDRAND => self.memory == 0 && reg(self.reg) && self.segment == 0 && matches!(self.size, 2 | 4 | 8),
                    CLFLUSH => self.memory == 1 && address,
                    CLI | STI | HLT => true,
                    POPF => matches!(self.size, 2 | 8),
                    EVENT_HOOK => self.length == 1,
                    BT_ALIGNED => self.memory == 1 && address && self.cr < 4 && (reg(self.reg) || self.reg == NO_REG) && matches!(self.size, 2 | 4 | 8),
                    X87_GATED | FNINIT | FNCLEX => true,
                    FLDCW | FLD80 | FSTP80 => self.memory == 1 && address,
                    FLDENV..=FNSAVE => self.memory == 1 && address && matches!(self.size, 2 | 4),
                    FXSAVE..=XRSTOR => self.memory == 1 && address,
                    XGETBV | XSETBV => self.memory == 0,
                    IN | OUT => self.memory == 0 && matches!(self.size, 1 | 2 | 4) && (self.reg == 2 || (self.reg == NO_REG && self.cr < 256)),
                    INS => self.memory == 0 && matches!(self.size, 1 | 2 | 4) && self.prefixes < 4 && self.segment == 0,
                    OUTS => self.memory == 0 && matches!(self.size, 1 | 2 | 4) && self.prefixes < 4 && matches!(self.segment as usize, 0 | super::SREG_FS | super::SREG_GS),
                    _ => false,
                }
        }
    }
}

/// `State::stop`: why the resident stopped the VM (`hvc #0x49`).
pub mod stop {
    /// The bank does not describe a supported mode ([`super::Cpu::control`] is `None`). When a transaction's change
    /// caused it (e.g. alignment checking: CR0.AM with RFLAGS.AC at CPL 3), the record holds the new CR0/CPL and the old
    /// RIP (the instruction that made the change).
    pub const UNSUPPORTED_MODE: u64 = 1;
    /// The host reply or staged object is unusable.
    pub const BAD_REPLY: u64 = 2;
    /// A native abort at a PC that is no access site of the current block.
    pub const UNKNOWN_ABORT: u64 = 3;
    /// A site's abort does not match its record (ESR class/direction, FAR).
    pub const SITE_MISMATCH: u64 = 4;
    /// (Retired by X1a: an x86 exception is delivered through the IDT, never a stop; a shutdown is [`SHUTDOWN`].)
    /// The number stays reserved.
    pub const GUEST_FAULT: u64 = 5;
    /// Shadow installation failed (capacity after eviction, memory outside guest RAM).
    pub const SHADOW: u64 = 7;
    /// The same abort recurred after its shadow was installed and left unchanged.
    pub const NO_PROGRESS: u64 = 8;
    /// A linker exit whose `InlineJITBlockHeader` names no live cache block (or a block without a link point to the
    /// exit's RIP), or a link point holding neither its original word nor a branch to a live block it may link to.
    pub const BAD_LINK: u64 = 9;
    /// The host parked the vCPU ([`super::REPLY_PARK`]).
    pub const PARKED: u64 = 10;
    /// A stop that is not one: an IRQ outside generated code or in a dead block, a [`super::STOP_WORD`] trap at a word
    /// the resident did not patch, or an armed patch record whose words do not hold the stop word.
    pub const BAD_STOP: u64 = 11;
    /// A system instruction whose form or operand value the resident does not implement (e.g. CR3 with PWT/PCD, a DR7
    /// enabling a breakpoint); nothing of it was executed.
    pub const UNSUPPORTED_SYSTEM: u64 = 12;
    /// A locked access the resident does not complete: a memory XCHG or LOCK read-modify-write (or an unlocked CMPXCHG8B,
    /// which is lowered to the same `caspal`) whose operand crosses a 16-byte boundary: an alignment fault at its atomic
    /// word, before any effect (owner decision: LOCKed accesses crossing 16 bytes stop; slice-3 contract §2).
    pub const UNSUPPORTED_ATOMIC: u64 = 13;
    /// Generated code (EL0) touched the resident's private window (`PRIVATE..PRIVATE + 1 GiB`): a data or instruction
    /// abort there (its pages are EL1-only, or EL0 execute-only code) at no access site of the block (a guest access to the
    /// window is #GP/#SS at its site): only code the code-shape check refuses, or forged code, reaches it.
    /// `State::native` holds the abort (ESR, FAR, ELR as an offset in its block when ELR is in one, else absolute).
    pub const PRIVATE_ACCESS: u64 = 14;
    /// Generated code ran an instruction the hardware refuses at EL0 (a system register, cache or TLB maintenance,
    /// `eret`, `hvc`, `wfi`, `udf`, the debug channel, counters and timers: ESR classes 0x00, 0x01, 0x18; also an illegal
    /// execution state, SME, and PC or SP misalignment: 0x0e, 0x1d, 0x22, 0x26); admission never admits one.
    pub const TRAPPED: u64 = 15;
    /// Shutdown (X1a; SDM Vol.3 7.15 interrupt 8): a contributory or page fault while delivering #DF, i.e. a triple fault.
    /// `State::fault` holds the last exception detected (vector, CR2, error code, RIP of the original event); the bank
    /// holds the state at the original event (CR2 as written by any #PF detected on the way); nothing was delivered.
    pub const SHUTDOWN: u64 = 16;
    /// (X2, numbered apart from the other chunks' reasons.) The vCPU was halted (HLT) with no event it could take, and the
    /// host ended its run ([`super::cache::Events::end`]: no source could wake it). The bank holds the state after the
    /// HLT.
    pub const HALTED: u64 = 32;
    /// (M3, numbered apart.) A device access the resident does not complete ([`super::Mmio`]: its `status` says why):
    /// a precise stop with the state at the instruction, nothing of it done (the device untouched).
    pub const UNSUPPORTED_MMIO: u64 = 48;
    /// (L3.) A port access the host refused ([`super::Mmio`] holds the port in `gpa`, `status` why: `unassigned` under the
    /// strict switch, or `device`): a precise stop with the state at the instruction (for a REP INS/OUTS, after the elements
    /// it completed), the access not made.
    pub const UNSUPPORTED_PORT: u64 = 49;

    pub fn name(code: u64) -> &'static str {
        match code {
            UNSUPPORTED_MODE => "unsupported_mode",
            BAD_REPLY => "bad_reply",
            UNKNOWN_ABORT => "unknown_abort",
            SITE_MISMATCH => "site_mismatch",
            SHADOW => "shadow",
            NO_PROGRESS => "no_progress",
            BAD_LINK => "bad_link",
            PARKED => "parked",
            BAD_STOP => "bad_stop",
            UNSUPPORTED_SYSTEM => "unsupported_system",
            UNSUPPORTED_ATOMIC => "unsupported_atomic",
            PRIVATE_ACCESS => "private_access",
            TRAPPED => "trapped",
            SHUTDOWN => "shutdown",
            HALTED => "halted",
            UNSUPPORTED_MMIO => "unsupported_mmio",
            UNSUPPORTED_PORT => "unsupported_port",
            _ => "none",
        }
    }
}

/// Exchange and diagnostics record of the execution mode.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct State {
    pub cpu: Cpu,
    /// This vCPU's number (0..`cache::MAX_VCPUS`): its bit in the global masks, its record and counter.
    pub vcpu: u64,
    pub request: u64,
    pub reply: u64,
    /// The cache's flush generation (flushes so far), sent with each request: the host never needs to resend an
    /// object the resident holds.
    pub generation: u64,
    /// With a reply: the guest-physical pages the staged block's bytes came from (`u64::MAX` = unused; the resident
    /// derives them from `fetch` and the block's length), and its link points (the host's).
    pub source_gpa: [u64; 2],
    pub link_points: [LinkPoint; MAX_LINK_POINTS],
    /// With a reply: the native offset of the staged block's computed exit `b` ([`DISPATCH_PLACEHOLDER`]), 0 for none.
    pub dispatch_exit: u64,
    /// With a request: the guest-physical pages of the instruction fetch at `cpu.rip`, walked by the resident (its page,
    /// then the next one if the host asked, [`REPLY_NEXT_PAGE`]; `u64::MAX` = not walked or the walk failed), and the
    /// failure of the last walk ([`fetch`]). The host copies the bytes from these pages only.
    pub fetch: [u64; 2],
    pub fetch_fault: [u64; 3],
    /// The host's stop RIP (`u64::MAX`: none): a miss there is not fetched (the host ends the run at it).
    pub stop_at: u64,
    pub stop: u64,
    /// Guest exception due at a stop: vector, CR2 (#PF; else 0), error code, RIP.
    pub fault: [u64; 4],
    /// Native abort at a stop: ESR, FAR, ELR (offset in its block; the address itself when no block holds it).
    pub native: [u64; 3],
    /// The last retry: native PC, FAR, the shadow descriptor then installed at FAR and the shadow generation.
    pub last_retry: [u64; 4],
    /// Bumped by every shadow reset (eviction, protection reset) and cache flush: an abort that repeats the last
    /// retry across one is progress, not a loop.
    pub shadow_generation: u64,
    /// Block entries through the resident (slow path); slow-path L2 hits; host translation requests answered.
    /// (Dispatcher L1 hits are not counted: generated code writes no emulator data.)
    pub blocks: u64,
    pub l2_hits: u64,
    pub requests: u64,
    /// Shadow-miss retries, of which read-only → writable upgrades,
    /// shadow evictions on capacity, page-crossing stores completed by the split service.
    pub retries: u64,
    pub upgrades: u64,
    pub evictions: u64,
    pub splits: u64,
    /// Linker entries (literal exits not yet linked); link points patched and restored; linker entries that did not
    /// link because the cache was flushed while the target was found or requested, or because the link records were
    /// full.
    pub linker: u64,
    pub links: u64,
    pub unlinks: u64,
    pub stale_links: u64,
    pub full_links: u64,
    /// Writes to pages holding translations that invalidated the page's blocks (stores, split stores, walker A/D
    /// updates), plus one per drain of this vCPU's record that carried `PAGES` (however many pages it listed);
    /// instructions single-stepped because they wrote a page their own block came from.
    pub invalidations: u64,
    pub steps: u64,
    /// Peers asked to drop their writable shadows of a page this vCPU marked for a block (and waited for).
    pub shootdowns: u64,
    /// A doorbell asked for a stop that is not yet taken (its block's exits are patched; [`super::STOP_WORD`]).
    pub stop_wanted: u64,
    /// Stops taken, and where: at once (interrupted at a block's first word; in the dispatcher; in L1), at a patched
    /// exit, or at a resident entry made at a block boundary while armed (linker, dispatcher miss, step, halt).
    pub stops: u64,
    pub stops_entry: u64,
    pub stops_dispatch: u64,
    pub stops_l1: u64,
    pub stops_exit: u64,
    pub stops_resident: u64,
    /// Blocks armed by a doorbell; doorbells that found the stop already armed (merged into it); doorbells that
    /// interrupted the step block.
    pub arms: u64,
    pub merged: u64,
    pub irqs_step: u64,
    /// CNTVCT when the last stop was taken.
    pub stop_ticks: u64,
    /// With a [`REPLY_SYSTEM`] reply: the decoded instruction.
    pub system: System,
    /// System instructions the resident began executing as transactions: counted on entry, so those refused
    /// (`unsupported_system`) are included along with the committed and faulted.
    pub systems: u64,
    /// The VM's vCPU count (CPUID's topology fields; set by the host, 1 by default).
    pub vcpus: u64,
    /// NMI blocking (SDM Vol.3 7.7.2): set by NMI delivery (X2), released by every IRET even when it faults (X1a).
    pub nmi_blocked: u64,
    /// Delivery attempts through the IDT (X1a): one per exception or software interrupt, plus one per fault detected while
    /// delivering (the nested fault or #DF that follows); attempts that fault are counted too.
    pub deliveries: u64,
    /// The last event delivered (X1a): its vector and the error code its frame holds, `u64::MAX` when it holds none (INT n
    /// and INT3 to any vector, and exceptions without one). The test driver's logging IDT reads its frame by it.
    pub delivered: [u64; 2],
    /// With a reply (X2): bit i set when link point i is the self-exit of a REP string instruction that ends the block
    /// (admission proves it from the guest bytes); the resident keeps it in the block ([`cache::Block::rep_exits`]).
    pub rep_exits: u64,
    /// 1 after an STI that set IF (SDM Vol.3 7.8.1, Vol.2 STI): maskable interrupts and NMIs are blocked until the next
    /// instruction completes (`Cpu::mov_ss` is the MOV SS shadow). Both end where the resident sees an instruction complete.
    pub sti_shadow: u64,
    /// 1 at a resident entry made at a REP string's self-exit (between iterations, elements done): an interrupt or NMI
    /// delivered there pushes RF = 1 (SDM Vol.3 "RF").
    pub mid_string: u64,
    /// The entry of the block entered right after a shadow-setting instruction with no event pending (X2), else 0: the
    /// shadow is cleared at that entry (the next resident entry follows a completed instruction), and a doorbell at that
    /// block's first word arms a stop instead of stopping there, so no boundary later than the shadow's sees it.
    pub shadow_entry: u64,
    /// With a step request (X2): 1 = compile the instruction without `--alignment-gated` (a BT* form the resident has just
    /// found aligned under alignment checking, [`system::BT_ALIGNED`]).
    pub plain_step: u64,
    /// X2 counters: external interrupts and NMIs delivered, HLTs that parked the vCPU, single steps taken to end a shadow
    /// with an event pending.
    pub interrupts: u64,
    pub nmis: u64,
    pub halts: u64,
    pub shadow_steps: u64,
    /// X4: the guest-physical address of the ACPI multiprocessor wakeup mailbox (ACPI 6.5 5.2.12.19, [`mailbox`]) this
    /// vCPU waits on before it runs (an AP in the firmware's waiting state, its bank the firmware's wake state), else 0.
    /// The resident clears it when the mailbox wakes the vCPU.
    pub mailbox: u64,
    /// X4: the times the waiting vCPU slept (`wfi`) before its wakeup command (stores to the mailbox page that land before
    /// it marks the page again coalesce into one doorbell), the CNTVCT of its acknowledgement, and the CNTVCT ticks it
    /// spent marking the page (waiting for writers to drop their writable shadows of it).
    pub mailbox_wakes: u64,
    pub mailbox_acked: u64,
    pub mailbox_mark_ticks: u64,
    /// S4, one word (the state record is copied at every resident entry, so it stays small; [`debug`]): the DR6 bits of
    /// a trap-class #DB pending at the next boundary (BS, B0..B3; taken first there, SDM Vol.3 Table 7-2 class 4, but a MOV
    /// SS shadow discards a single-step trap and holds a data breakpoint's, delivery_model A9); the B bits the instruction
    /// in progress met with a data or I/O access (added to the trap when it completes, dropped by a delivery); and the
    /// step mark, set while the instruction at RIP runs alone because RFLAGS.TF was 1 at its start (SDM Vol.3 18.3.1.4;
    /// SYSCALL and SYSRET set it from the TF they leave).
    pub debug: u64,
}
/// S4: [`State::debug`]'s fields.
pub mod debug {
    /// The pending trap's DR6 bits.
    pub const TRAP: u64 = 0xffff;
    /// B0..B3 the instruction in progress met, at this shift.
    pub const HITS: u32 = 16;
    pub const STEP: u64 = 1 << 20;
}
/// X4: the ACPI multiprocessor wakeup mailbox, version 0 (ACPI 6.5 Table 5.44; the 6.6 snapshot's Table 5.46, whose
/// Test command and reset vector belong to version 1): little endian, `command` (u16) at 0, reserved (u16) at 2,
/// `apic_id` (u32) at 4, `wakeup_vector` (u64) at 8, then 2032 bytes for the OS and 2048 for the firmware.
pub mod mailbox {
    /// Command 1, Wakeup: the AP whose APIC ID matches jumps to the wakeup vector. 0 is Noop (and the acknowledgement);
    /// every other value is reserved in version 0 (ignored, not acknowledged).
    pub const WAKEUP: u64 = 1;
    pub const VECTOR: u64 = 8;
}
impl State {
    pub const fn new(vcpu: u64, cpu: Cpu) -> Self {
        Self {
            cpu,
            vcpu,
            request: NONE,
            reply: NONE,
            generation: 0,
            source_gpa: [u64::MAX; 2],
            link_points: [LinkPoint::NONE; MAX_LINK_POINTS],
            dispatch_exit: 0,
            fetch: [u64::MAX; 2],
            fetch_fault: [0; 3],
            stop_at: u64::MAX,
            stop: 0,
            fault: [0; 4],
            native: [0; 3],
            last_retry: [0; 4],
            shadow_generation: 0,
            blocks: 0,
            l2_hits: 0,
            requests: 0,
            retries: 0,
            upgrades: 0,
            evictions: 0,
            splits: 0,
            linker: 0,
            links: 0,
            unlinks: 0,
            stale_links: 0,
            full_links: 0,
            invalidations: 0,
            steps: 0,
            shootdowns: 0,
            stop_wanted: 0,
            stops: 0,
            stops_entry: 0,
            stops_dispatch: 0,
            stops_l1: 0,
            stops_exit: 0,
            stops_resident: 0,
            arms: 0,
            merged: 0,
            irqs_step: 0,
            stop_ticks: 0,
            system: System::NONE,
            systems: 0,
            vcpus: 1,
            nmi_blocked: 0,
            deliveries: 0,
            delivered: [0, u64::MAX],
            rep_exits: 0,
            sti_shadow: 0,
            mid_string: 0,
            shadow_entry: 0,
            plain_step: 0,
            interrupts: 0,
            nmis: 0,
            halts: 0,
            shadow_steps: 0,
            mailbox: 0,
            mailbox_wakes: 0,
            mailbox_acked: 0,
            mailbox_mark_ticks: 0,
            debug: 0,
        }
    }
}
/// The vCPU's MSRs ([`crate::cpu::Msrs`]): a record of their own after [`State`] in its page, so the resident never copies
/// them with the state. The host writes [`crate::cpu::reset_msrs`] before the vCPU first runs; the resident owns them.
pub const MSRS_OFFSET: usize = STATE_OFFSET + 0x800;
pub const MSRS_VA: u64 = PRIVATE + MSRS_OFFSET as u64;
const _: () = assert!(core::mem::size_of::<State>() <= MSRS_OFFSET - STATE_OFFSET);
const _: () = assert!(MSRS_OFFSET + core::mem::size_of::<crate::cpu::Msrs>() <= LAPIC_OFFSET);
/// X3: the vCPU's local APIC ([`crate::lapic::Lapic`]), a record of its own in the state page; the host writes
/// [`crate::lapic::RESET`] before the vCPU first runs, the resident owns it.
pub const LAPIC_OFFSET: usize = STATE_OFFSET + 0xa00;
pub const LAPIC_VA: u64 = PRIVATE + LAPIC_OFFSET as u64;
const _: () = assert!(LAPIC_OFFSET + core::mem::size_of::<crate::lapic::Lapic>() <= ENTROPY_OFFSET);
/// The vCPU's entropy service (S3, RDRAND): a pool of random words the host fills and the resident consumes, a record of
/// its own in the state page. The host writes the record only while the vCPU is in the host (at reset and at each of its
/// exits it tops the pool up: [`Entropy::taken`] words are replaced, `taken` returns to 0); the resident only while the
/// vCPU runs (each RDRAND takes `words[taken]` and counts it). An RDRAND that finds the pool empty (`taken` = [`ENTROPY_WORDS`])
/// asks the host for a refill once ([`REQUEST_ENTROPY`], K2: RDRAND itself makes no host exit) and takes the next word;
/// a pool still empty after it (the host's entropy source failed) is RDRAND's architectural failure, CF = 0 (SDM Vol.2
/// RDRAND: "a value of zero ... and CF = 0 indicates the random number was not available"), which software retries.
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Entropy {
    pub taken: u64,
    pub words: [u64; ENTROPY_WORDS],
}
pub const ENTROPY_WORDS: usize = 32;
pub const ENTROPY_OFFSET: usize = STATE_OFFSET + 0xc00;
pub const ENTROPY_VA: u64 = PRIVATE + ENTROPY_OFFSET as u64;
const _: () = assert!(ENTROPY_OFFSET + core::mem::size_of::<Entropy>() <= STATE_OFFSET + 4096);
/// M3: a guest access by translated code to a guest-physical page that is not RAM (a device page: shadows never map
/// one, so the access aborts at its site). The resident walks it first (every #GP/#SS/#AC/#PF the access owes comes
/// before; the walk's A/D updates stay, as on x86, also when the access is then refused), and completes the instruction
/// from its site's completion record (`code_object::completion`: fexc's decode of the instruction it translated): one
/// device access through the host ([`REQUEST_DEVICE`]: the host performs exactly that read or write and replies
/// [`REPLY_DEVICE`]), then the load's register and RIP committed; the instruction never runs again. A record of its own
/// in the state page, written by the resident before the request and by the host before the reply.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Mmio {
    /// Request: the device byte (guest-physical), the width (1, 2, 4, 8), the direction (1: a store) and a store's data.
    pub gpa: u64,
    pub width: u64,
    pub write: u64,
    /// A store's data (request); a load's value, in its low `width` bytes (reply).
    pub value: u64,
    /// Reply: [`mmio::DONE`], or why the access is refused ([`mmio`]; also the resident's own refusals).
    pub status: u64,
    /// Device accesses this vCPU completed (resident).
    pub completed: u64,
}
pub mod mmio {
    /// [`super::Mmio::status`]: completed.
    pub const DONE: u64 = 1;
    /// Refusals (the vCPU stops [`super::stop::UNSUPPORTED_MMIO`] at the instruction, nothing of it done, the device untouched):
    /// a LOCK read-modify-write, memory XCHG or other atomic site; a pair, push or pop site; an access crossing a page with a
    /// device page on either side; a site without a completion record (every x86 form but MOV loads and stores of 1, 2, 4,
    /// 8 bytes, MOV of an immediate, moffs, MOVZX, MOVSX, MOVSXD); a page no device claims, only under the host's strict
    /// switch (L3, owner decision of October 29 and its amendment: otherwise a read shadows the page onto the all-ones page
    /// and runs natively, and a MOV store is completed as ignored, counted per access, status [`DONE`]); a device refusing the access (e.g. a width it does
    /// not implement); a system instruction's or a delivery's own access to a device page. Port accesses (L3,
    /// [`super::REQUEST_PORT`]) use [`DONE`], [`UNASSIGNED`] and [`DEVICE`] alike.
    pub const ATOMIC: u64 = 2;
    pub const SITE: u64 = 3;
    pub const CROSSING: u64 = 4;
    pub const FORM: u64 = 5;
    pub const UNASSIGNED: u64 = 6;
    pub const DEVICE: u64 = 7;
    pub const SYSTEM: u64 = 8;
    /// [`super::REQUEST_PAGE`]'s reply for a page a device claims.
    pub const CLAIMED: u64 = 9;
    pub fn name(status: u64) -> &'static str {
        match status {
            DONE => "done",
            ATOMIC => "atomic",
            SITE => "site",
            CROSSING => "crossing",
            FORM => "form",
            UNASSIGNED => "unassigned",
            DEVICE => "device",
            SYSTEM => "system",
            CLAIMED => "claimed",
            _ => "none",
        }
    }
}
pub const MMIO_OFFSET: usize = STATE_OFFSET + 0xe00;
pub const MMIO_VA: u64 = PRIVATE + MMIO_OFFSET as u64;
const _: () = assert!(ENTROPY_OFFSET + core::mem::size_of::<Entropy>() <= MMIO_OFFSET);
const _: () = assert!(MMIO_OFFSET + core::mem::size_of::<Mmio>() <= STATE_OFFSET + 4096);
const _: () = assert!(STATE_OFFSET + 4096 <= FRAME_OFFSET);
const _: () = assert!(FRAME_OFFSET + frame::BYTES <= GUEST_STATE_OFFSET);
const _: () = assert!(GUEST_STATE_OFFSET + frame::BYTES <= (crate::STACK_TOP - PRIVATE) as usize - STACK_BYTES);
const _: () = assert!(STAGING_OFFSET + STAGING_BYTES <= crate::resident::TEXT_OFFSET);

#[cfg(test)]
mod tests {
    use super::*;

    /// S4: data breakpoint ranges (LEN masks the address), R/W 01 writes only, 11 either, straddles, several bits.
    #[test]
    fn data_breakpoint_hits() {
        // DR0: 4 bytes at 0x1003 (masked to 0x1000), write only; DR1: 8 bytes at 0x1008, read or write; DR2 disabled.
        let dr = [0x1003, 0x1008, 0x1000, 0, DR6_FIXED, 0x400 | 1 | 4 | (1 | 3 << 2) << 16 | (3 | 2 << 2) << 20];
        assert_eq!(dr_range(&dr, 0), (0x1000, 4));
        assert_eq!(dr_range(&dr, 1), (0x1008, 8));
        assert_eq!(data_hits(&dr, 0x1000, 1, false), 0);
        assert_eq!(data_hits(&dr, 0x1000, 1, true), 1);
        assert_eq!(data_hits(&dr, 0xffe, 4, true), 1); // straddles into DR0's range
        assert_eq!(data_hits(&dr, 0xffc, 4, true), 0);
        assert_eq!(data_hits(&dr, 0x1002, 8, true), 3); // both ranges
        assert_eq!(data_hits(&dr, 0x1002, 8, false), 2);
        assert_eq!(data_hits(&dr, 0x1010, 8, false), 0);
    }

    /// G1 invariants: the guest-state fields lie in the page, the writable ones among them; `pushf` sees the bank's
    /// flags exactly (FEX ORs each byte shifted by its bit; DF is the byte's sign); DF round-trips.
    #[test]
    fn guest_state_page_layout() {
        let inside = |f: &[(u32, u32)], at: u32| f.iter().any(|&(o, n)| (o..o + n).contains(&at));
        for &(o, n) in frame::GUEST_STATE.iter().chain(&frame::GUEST_STATE_WRITABLE) {
            assert!(o as usize + n as usize <= frame::BYTES);
        }
        assert!(frame::GUEST_STATE_WRITABLE.iter().all(|&(o, n)| (o..o + n).all(|b| inside(&frame::GUEST_STATE, b))));
        assert_eq!(GUEST_STATE_VA % 4096, 0);
        assert_eq!(FRAME_POINTER, GUEST_STATE_VA);
        for rflags in [0x2u64, 0x202, 0x3202, 0x7_7702, 0x24_7f02, 0x40d02, 0x30_0302 | 1 << 14] {
            let mut image = 0u64;
            for (at, v) in frame::flag_bytes(rflags) {
                assert!(inside(&frame::GUEST_STATE, at), "{at:#x}");
                let b = at - frame::FLAGS;
                image |= if b == 10 { u64::from(frame::df(v)) } else { u64::from(v) } << b;
            }
            assert_eq!(image, rflags & 0x3f_7700 & !(3 << 16), "{rflags:#x}");
            assert_eq!(frame::df(frame::flag_bytes(rflags)[2].1), rflags & 0x400 != 0);
        }
        assert!(!frame::df(0) && !frame::df(1) && frame::df(0xff));
        // I7: FEX's gs_cached/fs_cached, 8 bytes each right below the flag bytes, readable, never writable.
        assert_eq!((frame::GS_BASE, frame::FS_BASE), (992, 1000));
        for at in [frame::GS_BASE, frame::FS_BASE] {
            assert!((at..at + 8).all(|b| inside(&frame::GUEST_STATE, b)) && !inside(&frame::GUEST_STATE_WRITABLE, at));
        }
        // I6: FEX's x87 fields in the pinned CPUState (TOP and C0..C3 are flag bytes 43, 40..42, 46; mm[8][2] after the 48
        // flag bytes; FCW and AbridgedFTW after the segment arrays), the x87 exception word in the padding after the tag word.
        assert_eq!(frame::X87_SF, frame::X87_PE + 1);
        let x87 = [frame::X87_IE, frame::X87_PE, frame::X87_C0, frame::X87_C1, frame::X87_C2, frame::X87_TOP, frame::X87_C3, frame::X87_MM];
        assert_eq!(x87, [0x410, 0x415, 0x418, 0x419, 0x41a, 0x41b, 0x41e, 0x420]);
        assert_eq!((frame::X87_FCW, frame::X87_FTW, frame::X87_EXCEPTIONS), (0x4b0, 0x4b2, 0x4b4));
        for at in x87.into_iter().chain([frame::X87_MM + 127, frame::X87_FTW, frame::X87_EXCEPTIONS, frame::X87_EXCEPTIONS + 3]) {
            assert!(inside(&frame::GUEST_STATE_WRITABLE, at), "{at:#x}");
        }
        assert!(inside(&frame::GUEST_STATE, frame::X87_FCW + 1) && !inside(&frame::GUEST_STATE_WRITABLE, frame::X87_FCW));
        assert!(!inside(&frame::GUEST_STATE, frame::X87_MM + 128) && !inside(&frame::GUEST_STATE, frame::X87_IE + 1));
        // I8: FEX's avx_high[16][2] (the pinned CPUState), readable and writable, nothing around it.
        assert!((frame::AVX_HIGH..frame::AVX_HIGH + 256).all(|b| inside(&frame::GUEST_STATE_WRITABLE, b)));
        assert!(!inside(&frame::GUEST_STATE, frame::AVX_HIGH - 1) && !inside(&frame::GUEST_STATE, frame::AVX_HIGH + 256));
    }

    /// I6: x87 arithmetic runs under MXCSR's FPCR when FCW.RC equals MXCSR.RC and neither FTZ nor DAZ is set, else under its own.
    #[test]
    fn x87_fpcr_differs_only_when_it_must() {
        assert_eq!(x87_fpcr(FCW_INIT, MXCSR_RESET), 0);
        for rc in 0..4u64 {
            assert_eq!(x87_fpcr(FCW_INIT | rc << 10, MXCSR_RESET | rc << 13), 0);
            assert_eq!(x87_fpcr(FCW_INIT | rc << 10, MXCSR_RESET | (rc ^ 1) << 13), fpcr(MXCSR_RESET | rc << 13));
        }
        assert_eq!(x87_fpcr(FCW_INIT, MXCSR_RESET | 1 << 15), fpcr(MXCSR_RESET)); // FTZ
        assert_eq!(x87_fpcr(FCW_INIT, MXCSR_RESET | MXCSR_DAZ), fpcr(MXCSR_RESET));
        assert_eq!(x87_fpcr(FCW_INIT & !0x300, MXCSR_RESET), 0); // precision control has no FPCR effect (doubles throughout)
    }

    #[test]
    fn flags_round_trip_through_fex_registers() {
        for rflags in 0..=ARITHMETIC_FLAGS {
            if rflags & !ARITHMETIC_FLAGS != 0 {
                continue;
            }
            let (nzcv, pf, af) = native_flags(rflags | 0x202);
            assert_eq!(guest_flags(0x202, nzcv, pf, af, true), rflags | 0x202);
        }
        // C is the inverse of CF; PF from the parity of a raw result byte.
        assert_eq!(native_flags(0).0, 1 << 29);
        assert_eq!(guest_flags(0, 0, 0x03, 0x03, false) & 4, 4);
        assert_eq!(guest_flags(0, 0, 0x01, 0x01, false) & 4, 0);
        assert_eq!(guest_flags(0, 0, 0x00, 0x10, false), 0x10 | 1 << 2);
    }

    #[test]
    fn division_of_128_by_64_bits() {
        let neg = |v: i64| v as u64;
        assert_eq!(divide(false, 1, 0, 2), Some((1 << 63, 0)));
        assert_eq!(divide(false, 0, 7, 2), Some((3, 1)));
        assert_eq!(divide(false, 2, 0, 2), None); // quotient 2^64
        assert_eq!(divide(false, 0, 5, 0), None);
        assert_eq!(divide(true, neg(-1), neg(-7), 2), Some((neg(-3), neg(-1)))); // remainder takes the dividend's sign
        assert_eq!(divide(true, 0, 7, neg(-2)), Some((neg(-3), 1)));
        assert_eq!(divide(true, neg(-1), 1 << 63, neg(-1)), None); // -2^63 / -1 = 2^63
        assert_eq!(divide(true, neg(-1), 1 << 63, 1), Some((1 << 63, 0))); // -2^63 fits
        assert_eq!(divide(true, 0, 1 << 63, 1), None); // 2^63 does not
        assert_eq!(divide(true, i64::MIN as u64, 0, neg(-1)), None); // -2^127 / -1 overflows i128 too
        assert_eq!(divide(true, 0, 5, 0), None);
    }

    #[test]
    fn system_replies_are_range_checked() {
        let mov = System { kind: system::MOV_TO_CR, length: 3, cr: 3, ..System::NONE };
        let invlpg = System { kind: system::INVLPG, length: 7, base: system::RIP, ..System::NONE };
        assert!(mov.well_formed() && invlpg.well_formed());
        let ltr = System { kind: system::LTR, length: 3, reg: 0, ..System::NONE };
        let far = System { kind: system::JMP_FAR, length: 3, memory: 1, size: 8, base: 0, segment: SREG_GS as u32, ..System::NONE };
        let ret = System { kind: system::RET_FAR, length: 3, size: 4, disp: 0xffff, ..System::NONE };
        assert!(ltr.well_formed() && far.well_formed() && ret.well_formed());
        assert!(!System { disp: 0x1_0000, ..ret }.well_formed() && !System { memory: 1, base: 17, ..ltr }.well_formed());
        for bad in [
            System { kind: 31, ..mov },
            System { kind: system::INT, cr: 256, ..mov }, // no such vector
            System { kind: system::LGDT, ..mov }, // no memory operand
            System { kind: system::IRET, size: 2, ..mov },
            System { kind: system::MOV_FROM_SREG, size: 8, memory: 0, reg: 16, ..mov },
            System { kind: system::JMP_FAR, size: 8, memory: 1, segment: 1, ..mov }, // CS: decoded as SREG_DS
            System { kind: system::MOV_FROM_DR, cr: 16, ..mov },
            System { length: 0, ..mov },
            System { length: 16, ..mov },
            System { reg: 16, ..mov },
            System { cr: 16, ..mov },
            System { lock: 2, ..mov },
            System { index: 3, ..invlpg }, // RIP-relative has no index
            System { base: 17, ..invlpg },
            System { scale: 4, base: 0, ..invlpg },
        ] {
            assert!(!bad.well_formed(), "{bad:?}");
        }
    }

    #[test]
    fn only_four_level_long_mode_has_walk_controls() {
        let cpu = Cpu {
            gpr: [0; 16],
            rip: 0,
            rflags: 0x202,
            cr0: CR0_PE | CR0_PG | CR0_WP,
            cr3: 0x1000,
            cr4: CR4_PAE,
            efer: EFER_LME | EFER_LMA | EFER_NXE,
            cpl: 3,
            fs_base: 0,
            gs_base: 0,
            cr2: 0,
            cr8: 0,
            dr: DR_RESET,
            seg: flat_segments(3),
            ldtr: LDTR_RESET,
            tr: TR_RESET,
            gdtr: TABLE_RESET,
            idtr: TABLE_RESET,
            mov_ss: 0,
            xmm: [[0; 2]; 16],
            mxcsr: MXCSR_RESET,
            fcw: FCW_INIT,
            xcr0: crate::cpu_profile::XCR0_RESET,
        };
        let c = cpu.control().unwrap();
        assert_eq!((c.cr3, c.cpl, c.wp, c.nxe, c.smep), (0x1000, 3, 1, 1, 0));
        // The driver's flat segments (QEMU stub's selectors) and a descriptor's hidden part.
        assert_eq!(flat_segments(0)[SREG_CS], Segment { selector: 0x08, attributes: 0xa09b, limit: 0xffff_ffff, base: 0 });
        assert_eq!(flat_segments(3)[SREG_SS], Segment { selector: 0x1b, attributes: 0xc0f3, limit: 0xffff_ffff, base: 0 });
        assert_eq!(flat_segments(3)[SREG_DS], Segment::NULL);
        assert_eq!(Segment::loaded(0x50, 0x12cf_9334_5678_0067), Segment { selector: 0x50, attributes: 0xc093, limit: 0xf006_7fff, base: 0x1234_5678 });
        assert_eq!(Segment::loaded(0x70, 0x0000_8900_0000_0067).limit, 0x67); // byte-granular
        for bad in [
            Cpu { cr0: CR0_PE, ..cpu },
            Cpu { cr4: 0, ..cpu },
            Cpu { cr4: CR4_PAE | CR4_LA57, ..cpu },
            Cpu { efer: EFER_LME, ..cpu },
            Cpu { cpl: 1, ..cpu },
            Cpu { cr3: 0x1008, ..cpu },
            Cpu { cr3: 0x1000 | 1 << 40, ..cpu },
            Cpu { seg: [Segment { attributes: 0xc0fb, ..cpu.seg[1] }; 6], ..cpu }, // compatibility mode (CS.L = 0)
            Cpu { seg: [Segment { attributes: 0xe0fb, ..cpu.seg[1] }; 6], ..cpu }, // CS.L = CS.D = 1
        ] {
            assert_eq!(bad.control(), None);
        }
        // Ignored CR3 bits stay in CR3 (and in the code context) but not in the walk's root.
        assert_eq!(Cpu { cr3: 0x1fe7, ..cpu }.control().unwrap().cr3, 0x1000);
        // AM or AC alone, or both at CPL 0, check nothing; both at CPL 3 run with alignment checking (X2).
        for fine in [
            Cpu { cr0: cpu.cr0 | CR0_AM, rflags: cpu.rflags | RFLAGS_AC, ..cpu },
            Cpu { cr0: cpu.cr0 | CR0_AM, ..cpu },
            Cpu { rflags: cpu.rflags | RFLAGS_AC, ..cpu },
            Cpu { cr0: cpu.cr0 | CR0_AM, rflags: cpu.rflags | RFLAGS_AC, cpl: 0, ..cpu },
        ] {
            assert!(fine.control().is_some());
        }
    }
}
