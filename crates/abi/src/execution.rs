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
    PRIVATE, RAM_SIZE,
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
/// FEX `CpuStateFrame`: x28, FEX's guest-state base register, points here at every block entry. Admitted code accesses
/// only its guest-state fields ([`frame::GUEST_STATE`]), none for the slice-1 instruction set.
pub const FRAME_OFFSET: usize = 0x23000;
pub const FRAME_VA: u64 = PRIVATE + FRAME_OFFSET as u64;
pub const FRAME_POINTER: u64 = FRAME_VA;
/// Host-written admitted object (header, code, tables).
pub const STAGING_OFFSET: usize = 0x70000;
pub const STAGING_BYTES: usize = 0x20000;
pub const STAGING_VA: u64 = PRIVATE + STAGING_OFFSET as u64;
/// Native stack mapped below `crate::STACK_TOP` while code runs (SP_EL0; admitted code never uses it).
pub const STACK_BYTES: usize = 0x4000;
/// Guest-physical RAM `[0, GUEST_RAM_BYTES)`: backing offset and resident alias, mapped in 2 MiB blocks.
pub const GUEST_RAM_OFFSET: usize = 2 * RAM_SIZE;
pub const GUEST_RAM_BYTES: usize = 0x40_0000;
pub const GUEST_RAM_VA: u64 = PRIVATE + GUEST_RAM_OFFSET as u64;
/// Guest-physical pages of guest RAM.
pub const GUEST_PAGES: usize = GUEST_RAM_BYTES / 4096;

/// The code cache (slice 2). Per vCPU: an executable code area with a writable
/// alias, a metadata arena (block records and their access sites) and the lookup
/// structures. Shared between vCPUs ([`cache::GLOBAL_VA`]): the code-page and
/// writable-shadow masks over guest-physical pages, one invalidation record and one
/// shadow-reset counter per vCPU. Areas are mapped in 2 MiB blocks; the backing
/// places them after guest RAM, the code area last because its size is set at
/// `prepare`. vCPU v's own areas (window, lookup, metadata, code) sit at the same
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
    pub const META_VA: u64 = PRIVATE + 0x3000_0000;
    pub const META_BYTES: usize = 8 * MIB;
    pub const LOOKUP_VA: u64 = PRIVATE + 0x3800_0000;
    pub const LOOKUP_BYTES: usize = 2 * MIB;
    pub const GLOBAL_VA: u64 = PRIVATE + 0x3c00_0000;
    pub const GLOBAL_BYTES: usize = 2 * MIB;
    /// Mapping granule of the areas.
    pub const BLOCK: usize = 2 * MIB;

    /// Backing offsets (2 MiB aligned).
    pub const LOOKUP_OFFSET: usize = crate::execution::GUEST_RAM_OFFSET + crate::execution::GUEST_RAM_BYTES;
    pub const GLOBAL_OFFSET: usize = LOOKUP_OFFSET + LOOKUP_BYTES;
    pub const META_OFFSET: usize = GLOBAL_OFFSET + GLOBAL_BYTES;
    pub const CODE_OFFSET: usize = META_OFFSET + META_BYTES;

    /// Lookup area layout: [`Control`], L2, block index (L1 is code, [`L1_CODE`]).
    pub const CONTROL: usize = 0;
    /// L2: open-addressed {guest RIP, block number + 1} table keyed by (RIP, context).
    pub const L2: usize = 0x2_0000;
    pub const L2_ENTRIES: usize = 1 << 16;
    /// Block index: metadata offsets of the blocks in placement (= code address) order.
    pub const INDEX: usize = 0x12_0000;
    /// At most half of L2, so probes stay short.
    pub const MAX_BLOCKS: usize = L2_ENTRIES / 2;
    /// Link records ([`LinkRecord`]), one per patched link point; a full area means no new links.
    pub const LINKS: usize = 0x14_0000;
    pub const MAX_LINKS: usize = 32 * 1024;
    /// The single-step block's record (its code is in the slot, `SLOT_VA`) and its access sites after it.
    pub const STEP_RECORD: usize = 0x1c_0000;
    pub const STEP_RECORD_BYTES: usize = 0x4_0000;

    /// Global area layout, shared by all vCPUs and updated only by atomic read-modify-writes (host and resident):
    /// a code-page mask byte per guest-physical page (bit v: vCPU v holds translations from the page), a writable
    /// mask byte per page (bit v: vCPU v may hold a writable shadow of it), one invalidation [`Record`] per vCPU and
    /// one shadow-reset counter (u64) per vCPU.
    pub const CODE_PAGES: usize = 0;
    pub const WRITABLE_PAGES: usize = 0x1000;
    pub const RECORDS: usize = 0x2000;
    pub const RESETS: usize = 0x3000;
    /// vCPUs of one VM (bits of the mask bytes).
    pub const MAX_VCPUS: usize = 4;

    /// Per-vCPU cache bookkeeping at [`CONTROL`]. The host sets `code_bytes` (the code area's size, head included) at
    /// prepare; the resident owns the rest.
    #[repr(C)]
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub struct Control {
        pub code_bytes: u64,
        /// Bump pointers of the code area (from [`HEAD_BYTES`]) and the metadata arena, and blocks placed since the
        /// last flush.
        pub code_used: u64,
        pub meta_used: u64,
        pub blocks: u64,
        /// The lookup context (CR3, context word) L1 was filled under.
        pub l1_context: [u64; 2],
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
    }
    /// A block placed in the cache: its record in the metadata arena (at the offset `INDEX` lists), its access sites
    /// ([`crate::code_object::Site`]) right after it.
    #[repr(C)]
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub struct Block {
        pub rip: u64,
        pub context: [u64; 2],
        /// Guest-physical source pages (`u64::MAX` = unused).
        pub source_gpa: [u64; 2],
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
        /// Its literal exits as admitted (`offset` 0 = unused).
        pub links: [super::LinkPoint; super::MAX_LINK_POINTS],
    }
    /// `Block::flags`: invalidated (a source page was written): out of L1 and L2, no link into or out of it, never run
    /// again; its space is reclaimed at the next flush.
    pub const DEAD: u32 = 1;
    /// The uncached single-step block, in the slot [`SLOT`] (not in `INDEX`, L1 or L2; never linked; no source page marked).
    pub const STEP: u32 = 2;
    impl Block {
        /// Its `JITCodeHeader` in the executable alias.
        pub fn code_va(&self) -> u64 {
            CODE_VA + u64::from(self.code)
        }
        pub fn entry(&self) -> u64 {
            self.code_va() + u64::from(super::ENTRY_OFFSET)
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
    /// OR), the page's `stale` bit before `pending`; the consumer swaps `pending` to 0, then each `stale` word, so no
    /// entry is lost and nothing overflows.
    #[repr(C)]
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub struct Record {
        /// [`PAGES`], [`FLUSH`], [`SHADOWS`].
        pub pending: u64,
        /// Guest-physical pages whose translations are stale (bit p % 64 of word p / 64).
        pub stale: [u64; crate::execution::GUEST_PAGES / 64],
    }
    /// `Record::pending`: `stale` lists pages; drop the whole cache; reset all shadows (and writable bits).
    pub const PAGES: u64 = 1;
    pub const FLUSH: u64 = 2;
    pub const SHADOWS: u64 = 4;

    const _: () = assert!(crate::execution::GUEST_RAM_OFFSET + crate::execution::GUEST_RAM_BYTES <= LOOKUP_OFFSET);
    const _: () = assert!((crate::execution::GUEST_RAM_OFFSET | crate::execution::GUEST_RAM_BYTES | LOOKUP_OFFSET) % BLOCK == 0);
    // The guest RAM alias ends below the code area; every area lies in PRIVATE..PRIVATE + 1 GiB, 2 MiB aligned.
    const _: () = assert!(crate::execution::GUEST_RAM_VA + crate::execution::GUEST_RAM_BYTES as u64 <= CODE_VA);
    const _: () = assert!(CODE_VA + CODE_BYTES_MAX as u64 <= CODE_WRITE_VA);
    const _: () = assert!(CODE_WRITE_VA + CODE_BYTES_MAX as u64 <= META_VA);
    const _: () = assert!(META_VA + META_BYTES as u64 <= LOOKUP_VA);
    const _: () = assert!(LOOKUP_VA + LOOKUP_BYTES as u64 <= GLOBAL_VA);
    const _: () = assert!(GLOBAL_VA + GLOBAL_BYTES as u64 <= PRIVATE + (1 << 30));
    const _: () = assert!(HEAD_BYTES + CODE_BYTES <= CODE_BYTES_MAX && CODE_BYTES % BLOCK == 0);
    const _: () = assert!(MAX_BLOCK_BYTES <= SLOT_BYTES && HEAD_BYTES % 64 == 0);
    const _: () = assert!((CODE_VA | CODE_WRITE_VA | META_VA | LOOKUP_VA | GLOBAL_VA) % BLOCK as u64 == 0);
    const _: () = assert!(CODE_OFFSET % BLOCK == 0 && META_BYTES % BLOCK == 0);
    const _: () = assert!(core::mem::size_of::<Control>() <= L2 && L2 + L2_ENTRIES * 16 <= INDEX);
    const _: () = assert!(L1_MASK == 0xfff && L1_ENTRY_BYTES == 32 && L1_CODE - DISPATCH < 1 << 20); // the dispatcher's form
    const _: () = assert!(INDEX + MAX_BLOCKS * 4 <= LINKS);
    const _: () = assert!(LINKS + MAX_LINKS * core::mem::size_of::<LinkRecord>() <= STEP_RECORD);
    const _: () = assert!(STEP_RECORD + STEP_RECORD_BYTES <= LOOKUP_BYTES && STEP_RECORD % core::mem::align_of::<Block>() == 0);
    const _: () = assert!(CODE_PAGES + crate::execution::GUEST_PAGES <= WRITABLE_PAGES);
    const _: () = assert!(WRITABLE_PAGES + crate::execution::GUEST_PAGES <= RECORDS);
    const _: () = assert!(RECORDS + MAX_VCPUS * core::mem::size_of::<Record>() <= RESETS && RECORDS % 8 == 0);
    const _: () = assert!(RESETS + MAX_VCPUS * 8 <= GLOBAL_BYTES);
    const _: () = assert!(crate::execution::GUEST_PAGES % 64 == 0);
}

/// Backing bytes the execution mode needs before the code area: window 0, guest RAM and the cache's fixed areas.
pub const BACKING_BYTES: usize = cache::CODE_OFFSET;

/// FEX `CpuStateFrame` fields admitted code may access (pinned FEX build).
pub mod frame {
    /// Guest-state fields `(offset, bytes)` FEX keeps in its frame that admitted code may access: none yet (the slice-1
    /// instruction set keeps all guest state in registers; DF, FS/GS bases, MXCSR, x87 and AVX state await their
    /// design). Every other x28 access is refused.
    pub const GUEST_STATE: [(u32, u32); 0] = [];
    pub const BYTES: usize = 4096;
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
}

pub const CR0_PE: u64 = 1;
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
    pub fn control(&self) -> Option<Control> {
        let long = self.cr0 & (CR0_PE | CR0_PG) == CR0_PE | CR0_PG
            && self.cr4 & CR4_PAE != 0
            && self.cr4 & (CR4_LA57 | CR4_SMAP) == 0
            && self.efer & (EFER_LME | EFER_LMA) == EFER_LME | EFER_LMA;
        (long && matches!(self.cpl, 0 | 3) && self.cr3 & !(paging::PA_MASK | CR3_IGNORED) == 0).then_some(Control {
            cr3: self.cr3 & paging::PA_MASK,
            cpl: self.cpl,
            wp: u64::from(self.cr0 & CR0_WP != 0),
            nxe: u64::from(self.efer & EFER_NXE != 0),
            smep: u64::from(self.cr4 & CR4_SMEP != 0),
        })
    }
    /// What code at a RIP is compiled and cached under besides the RIP: CR3, and a word of the CPL, the code mode
    /// (bit 2: 64-bit, the only mode yet) and the paging controls a translation depends on (CR0.PG/WP,
    /// CR4.PGE/PCIDE/SMEP, EFER.NXE).
    pub fn code_context(&self) -> [u64; 2] {
        let bit = |on: bool, n: u32| u64::from(on) << n;
        let word = self.cpl & 3
            | bit(true, 2)
            | bit(self.cr0 & CR0_PG != 0, 3)
            | bit(self.cr0 & CR0_WP != 0, 4)
            | bit(self.cr4 & CR4_PGE != 0, 5)
            | bit(self.cr4 & CR4_PCIDE != 0, 6)
            | bit(self.cr4 & CR4_SMEP != 0, 7)
            | bit(self.efer & EFER_NXE != 0, 8);
        [self.cr3, word]
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
/// (Translate requests only.) The instruction at `cpu.rip` is a system instruction, decoded by the host into
/// `State::system`; nothing is staged. The resident executes it as a transaction ([`system`]).
pub const REPLY_SYSTEM: u64 = 3;
/// The bytes of `cpu.rip`'s page ran out before the block (or the instruction) ended: the resident walks the next page
/// for an instruction fetch (marking it, for a translate request) and asks again with its result in `State::fetch` /
/// `State::fetch_fault`.
pub const REPLY_NEXT_PAGE: u64 = 4;

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
    /// MOV CR: the control register number (0..15, REX.R included) and the general register.
    pub cr: u32,
    pub reg: u32,
    /// INVLPG's memory operand: base register ([`system::RIP`] for RIP-relative, [`system::NO_REG`] for none), index
    /// register ([`system::NO_REG`]: none), scale (a shift, 0..3) and displacement; 64-bit address size.
    pub base: u32,
    pub index: u32,
    pub scale: u32,
    pub disp: i64,
}
pub mod system {
    /// CR `cr` := GPR `reg`; GPR `reg` := CR `cr`.
    pub const MOV_TO_CR: u32 = 1;
    pub const MOV_FROM_CR: u32 = 2;
    pub const INVLPG: u32 = 3;
    /// EDX:EAX := MSR ECX; MSR ECX := EDX:EAX.
    pub const RDMSR: u32 = 4;
    pub const WRMSR: u32 = 5;
    /// `System::base` of a RIP-relative operand (relative to the next instruction).
    pub const RIP: u32 = 16;
    pub const NO_REG: u32 = 0xff;
    /// The one MSR the resident implements; any other RDMSR/WRMSR raises #GP(0).
    pub const MSR_EFER: u32 = 0xc000_0080;
    impl super::System {
        pub const NONE: Self = Self { kind: 0, length: 0, lock: 0, cr: 0, reg: 0, base: NO_REG, index: NO_REG, scale: 0, disp: 0 };
        /// Field ranges the resident relies on (it checks every reply).
        pub fn well_formed(&self) -> bool {
            let reg = |r: u32| r < 16;
            (1..=15).contains(&self.length)
                && self.lock <= 1
                && match self.kind {
                    MOV_TO_CR | MOV_FROM_CR => reg(self.cr) && reg(self.reg),
                    INVLPG => (reg(self.base) || self.base == RIP || self.base == NO_REG)
                        && (reg(self.index) || self.index == NO_REG)
                        && self.scale <= 3
                        && (self.base != RIP || self.index == NO_REG),
                    RDMSR | WRMSR => true,
                    _ => false,
                }
        }
    }
}

/// `State::stop`: why the resident stopped the VM (`hvc #0x49`).
pub mod stop {
    /// The bank does not describe a supported mode.
    pub const UNSUPPORTED_MODE: u64 = 1;
    /// The host reply or staged object is unusable.
    pub const BAD_REPLY: u64 = 2;
    /// A native abort at a PC that is no access site of the current block.
    pub const UNKNOWN_ABORT: u64 = 3;
    /// A site's abort does not match its record (ESR class/direction, FAR).
    pub const SITE_MISMATCH: u64 = 4;
    /// An x86 exception is due at a guest instruction boundary (`State::fault`; the bank holds the
    /// exact state at that instruction's start). Delivery through the IDT is later system work.
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
    /// A system instruction whose form or operand value the resident does not implement (e.g. MOV from CR2, a CR0 or
    /// CR4 feature bit change, CR3 with PWT/PCD); nothing of it was executed.
    pub const UNSUPPORTED_SYSTEM: u64 = 12;
    /// A locked access the resident does not complete: a memory XCHG whose operand crosses a 16-byte boundary (an
    /// alignment fault at its `swpal`, before any effect; contract: split locks wait for the boot phase).
    pub const UNSUPPORTED_ATOMIC: u64 = 13;
    /// Generated code (EL0) touched the resident's private window (`PRIVATE..PRIVATE + 1 GiB`): a data or instruction
    /// abort there (its pages are EL1-only, or EL0 execute-only code). With the address guard on (until E3) only a guard
    /// bypass or a bug reaches it; `State::native` holds the abort.
    pub const PRIVATE_ACCESS: u64 = 14;
    /// Generated code ran an instruction the hardware refuses at EL0 (a system register, cache or TLB maintenance,
    /// `eret`, `hvc`, `wfi`, `udf`, the debug channel, counters and timers: ESR classes 0x00, 0x01, 0x18; also an illegal
    /// execution state, SME, and PC or SP misalignment: 0x0e, 0x1d, 0x22, 0x26); admission never admits one.
    pub const TRAPPED: u64 = 15;

    pub fn name(code: u64) -> &'static str {
        match code {
            UNSUPPORTED_MODE => "unsupported_mode",
            BAD_REPLY => "bad_reply",
            UNKNOWN_ABORT => "unknown_abort",
            SITE_MISMATCH => "site_mismatch",
            GUEST_FAULT => "guest_fault",
            SHADOW => "shadow",
            NO_PROGRESS => "no_progress",
            BAD_LINK => "bad_link",
            PARKED => "parked",
            BAD_STOP => "bad_stop",
            UNSUPPORTED_SYSTEM => "unsupported_system",
            UNSUPPORTED_ATOMIC => "unsupported_atomic",
            PRIVATE_ACCESS => "private_access",
            TRAPPED => "trapped",
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
        }
    }
}
const _: () = assert!(core::mem::size_of::<State>() <= 4096);
const _: () = assert!(STATE_OFFSET + 4096 <= FRAME_OFFSET);
const _: () = assert!(FRAME_OFFSET + frame::BYTES <= STAGING_OFFSET);
const _: () = assert!(STAGING_OFFSET + STAGING_BYTES <= crate::resident::TEXT_OFFSET);

#[cfg(test)]
mod tests {
    use super::*;

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
        for bad in [
            System { kind: 9, ..mov },
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
        };
        let c = cpu.control().unwrap();
        assert_eq!((c.cr3, c.cpl, c.wp, c.nxe, c.smep), (0x1000, 3, 1, 1, 0));
        for bad in [
            Cpu { cr0: CR0_PE, ..cpu },
            Cpu { cr4: 0, ..cpu },
            Cpu { cr4: CR4_PAE | CR4_LA57, ..cpu },
            Cpu { efer: EFER_LME, ..cpu },
            Cpu { cpl: 1, ..cpu },
            Cpu { cr3: 0x1008, ..cpu },
            Cpu { cr3: 0x1000 | 1 << 40, ..cpu },
        ] {
            assert_eq!(bad.control(), None);
        }
        // Ignored CR3 bits stay in CR3 (and in the code context) but not in the walk's root.
        assert_eq!(Cpu { cr3: 0x1fe7, ..cpu }.control().unwrap().cr3, 0x1000);
    }
}
