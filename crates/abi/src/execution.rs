//! General block execution (slices 1 and 2): the vCPU's x86 architectural state bank,
//! the record the resident and the host exchange per translation, the private layout
//! of the execution mode (code cache included), and the FEX frame offsets admitted code may touch.
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
/// Resident → host: kick vCPU `x0` (see [`STOP_OFFSET`]); returns at once.
pub const KICK_HVC: u64 = 0x4c;

pub const STATE_OFFSET: usize = 0x22000;
pub const STATE_VA: u64 = PRIVATE + STATE_OFFSET as u64;
/// FEX `CpuStateFrame` (x28): private plumbing, never guest state.
pub const FRAME_OFFSET: usize = 0x23000;
pub const FRAME_VA: u64 = PRIVATE + FRAME_OFFSET as u64;
/// x28 = the frame page + `FRAME_BIAS`: every admitted field ([`frame::FIELDS`], offsets up to 3040) stays in the frame
/// page, and the block entry's stop store (`str xzr, [x28, #frame::INTERRUPT_FAULT_PAGE]`) lands alone in the next page,
/// the stop page.
pub const FRAME_BIAS: u64 = 0x400;
pub const FRAME_POINTER: u64 = FRAME_VA + FRAME_BIAS;
/// The stop page (precise stop, slice 3): the only page of the vCPU's stop region, `STOP_BYTES` of the window that
/// hold nothing else, so one IPA granule (4 or 16 KiB, as HVF reports it) covers the stop page alone. To stop a running
/// vCPU the host write-protects that granule (a kick): its next block entry takes a stage-2 fault at the stop store
/// with the entry state intact, and the host enters the resident's stop gate. (The regression families map their own
/// pages at these private addresses, e.g. `pressure::SPILLS`; they never run in this mode.)
pub const STOP_OFFSET: usize = FRAME_OFFSET + 0x1000;
pub const STOP_BYTES: usize = 0x4000;
/// Native offset of a block's stop store (its third prologue word, after `adr x0, header; str x0, [x28]`).
pub const STOP_STORE: u32 = ENTRY_OFFSET + 8;
/// Code slot: executable alias at `SLOT_VA`, writable alias at `SLOT_WRITE_VA`.
pub const SLOT_OFFSET: usize = 0x30000;
pub const SLOT_BYTES: usize = 0x10000;
pub const SLOT_VA: u64 = PRIVATE + SLOT_OFFSET as u64;
pub const SLOT_WRITE_VA: u64 = PRIVATE + 0x40000;
/// Host-written admitted object (header, code, tables).
pub const STAGING_OFFSET: usize = 0x70000;
pub const STAGING_BYTES: usize = 0x20000;
pub const STAGING_VA: u64 = PRIVATE + STAGING_OFFSET as u64;
/// Native stack mapped below `crate::STACK_TOP` while code runs.
pub const STACK_BYTES: usize = 0x4000;
/// Generated code's native stack (spill area below `crate::STACK_TOP`).
pub const BLOCK_STACK_BYTES: u32 = 0x2000;
/// Stack below the spill area a 128/64 division uses: FEX's `lr` push and the helper's saved registers.
pub const HELPER_STACK_BYTES: u32 = 64;
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
    /// Code area: executable alias (blocks run here) and writable alias (the resident places them there).
    pub const CODE_VA: u64 = PRIVATE + 0x1000_0000;
    pub const CODE_WRITE_VA: u64 = PRIVATE + 0x2000_0000;
    pub const CODE_BYTES: usize = 16 * MIB;
    /// Every block-to-block branch of a later linking step stays one `b` (±128 MiB).
    pub const CODE_BYTES_MAX: usize = 128 * MIB;
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

    /// Lookup area layout: [`Control`], L1, L2, block index.
    pub const CONTROL: usize = 0;
    /// L1: direct-mapped {guest RIP, host entry} pairs indexed by `rip & L1_MASK` (FEX `L1Pointer/L1Mask`).
    /// An empty entry holds a RIP whose low bits differ from its index, so it never matches.
    pub const L1: usize = 0x1_0000;
    pub const L1_ENTRIES: usize = 4096;
    pub const L1_MASK: u64 = L1_ENTRIES as u64 - 1;
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

    /// Per-vCPU cache bookkeeping at [`CONTROL`]. The host sets `code_bytes` (the code area's capacity) at prepare;
    /// the resident owns the rest.
    #[repr(C)]
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub struct Control {
        pub code_bytes: u64,
        /// Bump pointers of the code area and the metadata arena, and blocks placed since the last flush.
        pub code_used: u64,
        pub meta_used: u64,
        pub blocks: u64,
        /// The lookup context (CR3, context word) L1 was filled under.
        pub l1_context: [u64; 2],
        /// Link records in use.
        pub links: u64,
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
        /// Offset of the FEX block (its 4-byte `JITCodeHeader` first) in the code area, and its size.
        pub code: u32,
        pub code_size: u32,
        pub sites_count: u32,
        pub guest_bytes: u32,
        /// Its position in `INDEX`.
        pub number: u32,
        /// [`DEAD`], [`STEP`].
        pub flags: u32,
        /// Its literal exits as admitted (`offset` 0 = unused).
        pub links: [super::LinkPoint; super::MAX_LINK_POINTS],
    }
    /// `Block::flags`: invalidated (a source page was written): out of L1 and L2, no link into or out of it, never run
    /// again; its space is reclaimed at the next flush.
    pub const DEAD: u32 = 1;
    /// The uncached single-step block, in the slot (not in `INDEX`, L1 or L2; never linked; no source page marked).
    pub const STEP: u32 = 2;
    impl Block {
        /// Its `JITCodeHeader` in the executable alias (what its entry prologue stores in `InlineJITBlockHeader`).
        pub fn code_va(&self) -> u64 {
            if self.flags & STEP != 0 { super::SLOT_VA } else { CODE_VA + u64::from(self.code) }
        }
        pub fn entry(&self) -> u64 {
            self.code_va() + u64::from(super::ENTRY_OFFSET)
        }
    }
    /// A patched link point: the first word of link point `offset` of block `source` (block numbers) was `original`
    /// (`ldr x0, L`) and is now `b` to the entry of block `target`.
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
    const _: () = assert!(CODE_BYTES <= CODE_BYTES_MAX && CODE_BYTES % BLOCK == 0);
    const _: () = assert!(MAX_BLOCK_BYTES <= crate::execution::SLOT_BYTES);
    const _: () = assert!((CODE_VA | CODE_WRITE_VA | META_VA | LOOKUP_VA | GLOBAL_VA) % BLOCK as u64 == 0);
    const _: () = assert!(CODE_OFFSET % BLOCK == 0 && META_BYTES % BLOCK == 0);
    const _: () = assert!(core::mem::size_of::<Control>() <= L1);
    const _: () = assert!(L1 + L1_ENTRIES * 16 <= L2 && L2 + L2_ENTRIES * 16 <= INDEX);
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
    /// `InlineJITBlockHeader`: the entry prologue `adr x0, header; str x0, [x28]` stores the running block's address
    /// (its only admitted access); the linker reads it to find the block whose literal exit it was called from.
    pub const BLOCK_HEADER: u32 = 0;
    /// `State.rip`: every exit stores the next guest RIP here.
    pub const RIP: u32 = 24;
    /// `CPUState.flags[RFLAG_NZCV_LOC]`: an instruction's incoming NZCV (`nzcv=frame` sites).
    pub const PRE_NZCV: u32 = 1032;
    /// `Pointers.DispatcherLoopTop`: the resident exit gate of computed exits (the fast dispatcher).
    pub const DISPATCHER_LOOP_TOP: u32 = 2920;
    /// `Pointers.ExitFunctionLinker`: the resident block linker, the gate of every literal exit (a link point).
    pub const EXIT_FUNCTION_LINKER: u32 = 2952;
    /// `Pointers.LUDIVHandler` / `LDIVHandler`: the resident 128/64 unsigned / signed division helpers a 64-bit
    /// div / idiv calls when its dividend needs 128 bits (FEX's helper ABI, see the runtime's `natemu_ludiv`).
    pub const LUDIV_HANDLER: u32 = 3024;
    pub const LDIV_HANDLER: u32 = 3032;
    /// Native pointers the resident fills; code may only load them.
    pub const POINTERS: [u32; 4] = [DISPATCHER_LOOP_TOP, EXIT_FUNCTION_LINKER, LUDIV_HANDLER, LDIV_HANDLER];
    /// `(offset, bytes)` of every admitted access.
    pub const FIELDS: [(u32, u32); 7] = [
        (BLOCK_HEADER, 8),
        (RIP, 8),
        (PRE_NZCV, 4),
        (DISPATCHER_LOOP_TOP, 8),
        (EXIT_FUNCTION_LINKER, 8),
        (LUDIV_HANDLER, 8),
        (LDIV_HANDLER, 8),
    ];
    /// FEX's `InterruptFaultPage` (`offsetof(InternalThreadState, InterruptFaultPage) - offsetof(.., BaseFrameState)`,
    /// fexc's `state_abi.state_offsets.natemu_interrupt_fault_page`): the block entry's stop store `str xzr, [x28, #this]`.
    /// The runtime stops a vCPU by write-protecting the region holding these 8 bytes. Not a member of [`FIELDS`]:
    /// admission allows that one store and no other access.
    pub const INTERRUPT_FAULT_PAGE: u32 = 3776;
    pub const BYTES: usize = 4096;
}
/// Native offset of a FEX block's entry: after its 4-byte `JITCodeHeader`.
pub const ENTRY_OFFSET: u32 = 4;

/// A literal exit of an admitted block, exactly `ldr x0, L; str x0, [x28, #RIP]; ldr x1, [x28, #EXIT_FUNCTION_LINKER];
/// br x1` with L holding a proven guest RIP (the block end or its branch's decoded target): the native offset of its
/// first word, the only word the resident's linker rewrites (to `b` the entry of the block cached for `target`), and
/// `target`. Offset 0 (the code header) marks an unused entry.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LinkPoint {
    pub offset: u32,
    pub reserved: u32,
    pub target: u64,
}
impl LinkPoint {
    pub const NONE: Self = Self { offset: 0, reserved: 0, target: 0 };
}
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
    /// The stop gate was entered at a PC that is no live block's stop store (`super::STOP_STORE`), or not from EL1t.
    pub const BAD_STOP: u64 = 11;
    /// A system instruction whose form or operand value the resident does not implement (e.g. MOV from CR2, a CR0 or
    /// CR4 feature bit change, CR3 with PWT/PCD); nothing of it was executed.
    pub const UNSUPPORTED_SYSTEM: u64 = 12;
    /// A locked access the resident does not complete: a memory XCHG whose operand crosses a 16-byte boundary (an
    /// alignment fault at its `swpal`, before any effect; contract: split locks wait for the boot phase).
    pub const UNSUPPORTED_ATOMIC: u64 = 13;

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
    /// Block entries through the resident (slow path) and through the fast dispatcher (L1 hits); slow-path L2 hits;
    /// host translation requests answered.
    pub blocks: u64,
    pub fast: u64,
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
    /// The resident's stop gate (written at start): where the host enters a vCPU stopped at a block entry by a kick.
    pub stop_gate: u64,
    /// Stops taken at the stop gate.
    pub stops: u64,
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
            fetch: [u64::MAX; 2],
            fetch_fault: [0; 3],
            stop_at: u64::MAX,
            stop: 0,
            fault: [0; 4],
            native: [0; 3],
            last_retry: [0; 4],
            shadow_generation: 0,
            blocks: 0,
            fast: 0,
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
            stop_gate: 0,
            stops: 0,
            system: System::NONE,
            systems: 0,
        }
    }
}
const _: () = assert!(core::mem::size_of::<State>() <= 4096);
const _: () = assert!(STATE_OFFSET + 4096 <= FRAME_OFFSET);
// Admitted fields stay in the frame page; the stop store's 8 bytes are in the stop page; the stop region is aligned
// for the largest IPA granule it may be (16 KiB) and holds nothing else.
const _: () = {
    let mut i = 0;
    while i < frame::FIELDS.len() {
        assert!(FRAME_BIAS as usize + (frame::FIELDS[i].0 + frame::FIELDS[i].1) as usize <= frame::BYTES);
        i += 1;
    }
};
const _: () = assert!(FRAME_OFFSET + FRAME_BIAS as usize + frame::INTERRUPT_FAULT_PAGE as usize >= STOP_OFFSET);
const _: () = assert!(FRAME_OFFSET + FRAME_BIAS as usize + frame::INTERRUPT_FAULT_PAGE as usize + 8 <= STOP_OFFSET + 4096);
const _: () = assert!(FRAME_OFFSET + frame::BYTES == STOP_OFFSET && STOP_OFFSET % STOP_BYTES == 0);
const _: () = assert!(STOP_OFFSET + STOP_BYTES <= SLOT_OFFSET);
const _: () = assert!(SLOT_OFFSET + SLOT_BYTES <= 0x40000);
const _: () = assert!(STAGING_OFFSET + STAGING_BYTES <= crate::resident::TEXT_OFFSET);
const _: () = assert!(crate::STACK_TOP - PRIVATE >= 0x50000 + BLOCK_STACK_BYTES as u64);
const _: () = assert!((BLOCK_STACK_BYTES + HELPER_STACK_BYTES) as usize <= STACK_BYTES);

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
