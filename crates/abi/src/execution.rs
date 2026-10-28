//! General block execution (slices 1 and 2): the vCPU's x86 architectural state bank,
//! the record the resident and the host exchange per translation, the private layout
//! of the execution mode (code cache included), and the FEX frame offsets admitted code may touch.
//!
//! Flow: blocks stay in the vCPU's resident code cache ([`cache`]). At a block
//! exit the resident's dispatcher looks up the next RIP (L1, then L2); only a
//! miss asks the host for code (`hvc #TRANSLATE_HVC`): the host fetches the bytes
//! through the guest walk, compiles and admits one block into the staging area
//! and replies; the resident places it in the cache and enters it. Shadow misses
//! are served in the resident.
use crate::{
    PRIVATE, RAM_SIZE,
    paging::{self, Control},
};

/// `Shared::mode` of the execution mode.
pub const MODE: u64 = 32;
/// Resident → host: translate at `cpu.rip` (state record holds the request).
pub const TRANSLATE_HVC: u64 = 0x4b;

pub const STATE_OFFSET: usize = 0x22000;
pub const STATE_VA: u64 = PRIVATE + STATE_OFFSET as u64;
/// FEX `CpuStateFrame` (x28): private plumbing, never guest state.
pub const FRAME_OFFSET: usize = 0x23000;
pub const FRAME_VA: u64 = PRIVATE + FRAME_OFFSET as u64;
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
/// Guest-physical RAM `[0, GUEST_RAM_BYTES)`: backing offset and resident alias.
pub const GUEST_RAM_OFFSET: usize = RAM_SIZE;
pub const GUEST_RAM_BYTES: usize = 0x10_0000;
pub const GUEST_RAM_VA: u64 = PRIVATE + GUEST_RAM_OFFSET as u64;
/// Guest-physical pages of guest RAM.
pub const GUEST_PAGES: usize = GUEST_RAM_BYTES / 4096;

/// The code cache (slice 2). Per vCPU: an executable code area with a writable
/// alias, a metadata arena (block records and their access sites) and the lookup
/// structures. Shared between owners ([`cache::GLOBAL_VA`]): the code-page masks
/// over guest-physical pages and one invalidation queue per vCPU. Areas are
/// mapped in 2 MiB blocks; the backing places them after guest RAM, the code
/// area last because its size is set at `prepare`.
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
    pub const LOOKUP_OFFSET: usize = 2 * MIB;
    pub const GLOBAL_OFFSET: usize = LOOKUP_OFFSET + LOOKUP_BYTES;
    pub const META_OFFSET: usize = GLOBAL_OFFSET + GLOBAL_BYTES;
    pub const CODE_OFFSET: usize = META_OFFSET + META_BYTES;

    /// Lookup area layout: [`Control`], the vCPU's writable-shadow bits over guest-physical pages, L1, L2, block index.
    pub const CONTROL: usize = 0;
    pub const WRITABLE: usize = 0x1000;
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

    /// Global area layout: one code-page mask byte per guest-physical page (bit v: vCPU v holds translations from
    /// the page), then one [`Queue`] per vCPU.
    pub const CODE_PAGES: usize = 0;
    pub const QUEUES: usize = 0x4000;
    pub const MAX_VCPUS: usize = 8;

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
    }
    /// Invalidation requests to one vCPU, drained at each resident entry. `pages` lists guest-physical pages whose
    /// translations are stale; `flush` (or a full list) drops the whole cache.
    #[repr(C)]
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub struct Queue {
        pub flush: u64,
        pub count: u64,
        pub pages: [u64; 30],
    }
    pub const FLUSH: u64 = 1;

    const _: () = assert!(crate::execution::GUEST_RAM_OFFSET + crate::execution::GUEST_RAM_BYTES <= LOOKUP_OFFSET);
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
    const _: () = assert!(core::mem::size_of::<Control>() <= WRITABLE);
    const _: () = assert!(WRITABLE + crate::execution::GUEST_PAGES / 8 <= L1);
    const _: () = assert!(L1 + L1_ENTRIES * 16 <= L2 && L2 + L2_ENTRIES * 16 <= INDEX);
    const _: () = assert!(INDEX + MAX_BLOCKS * 4 <= LOOKUP_BYTES);
    const _: () = assert!(CODE_PAGES + crate::execution::GUEST_PAGES <= QUEUES);
    const _: () = assert!(QUEUES + MAX_VCPUS * core::mem::size_of::<Queue>() <= GLOBAL_BYTES);
}

/// Backing bytes the execution mode needs before the code area: window 0, guest RAM and the cache's fixed areas.
pub const BACKING_BYTES: usize = cache::CODE_OFFSET;

/// FEX `CpuStateFrame` fields admitted code may access (pinned FEX build).
pub mod frame {
    /// `InlineJITBlockHeader`: the entry prologue stores the block's address.
    pub const BLOCK_HEADER: u32 = 0;
    /// `State.rip`: every exit stores the next guest RIP here.
    pub const RIP: u32 = 24;
    /// `CPUState.flags[RFLAG_NZCV_LOC]`: an instruction's incoming NZCV (`nzcv=frame` sites).
    pub const PRE_NZCV: u32 = 1032;
    /// `Pointers.DispatcherLoopTop`: the resident exit gate.
    pub const DISPATCHER_LOOP_TOP: u32 = 2920;
    /// `Pointers.LUDIVHandler` / `LDIVHandler`: the resident 128/64 unsigned / signed division helpers a 64-bit
    /// div / idiv calls when its dividend needs 128 bits (FEX's helper ABI, see the runtime's `natemu_ludiv`).
    pub const LUDIV_HANDLER: u32 = 3024;
    pub const LDIV_HANDLER: u32 = 3032;
    /// Native pointers the resident fills; code may only load them.
    pub const POINTERS: [u32; 3] = [DISPATCHER_LOOP_TOP, LUDIV_HANDLER, LDIV_HANDLER];
    /// `(offset, bytes)` of every admitted access.
    pub const FIELDS: [(u32, u32); 6] =
        [(BLOCK_HEADER, 8), (RIP, 8), (PRE_NZCV, 4), (DISPATCHER_LOOP_TOP, 8), (LUDIV_HANDLER, 8), (LDIV_HANDLER, 8)];
    pub const BYTES: usize = 4096;
}
/// Native offset of a FEX block's entry: after its 4-byte `JITCodeHeader`.
pub const ENTRY_OFFSET: u32 = 4;
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
pub const CR0_WP: u64 = 1 << 16;
pub const CR0_PG: u64 = 1 << 31;
pub const CR4_PAE: u64 = 1 << 5;
pub const CR4_SMEP: u64 = 1 << 20;
pub const CR4_SMAP: u64 = 1 << 21;
pub const CR4_LA57: u64 = 1 << 12;
pub const CR4_PGE: u64 = 1 << 7;
pub const CR4_PCIDE: u64 = 1 << 17;
pub const EFER_LME: u64 = 1 << 8;
pub const EFER_LMA: u64 = 1 << 10;
pub const EFER_NXE: u64 = 1 << 11;

impl Cpu {
    /// Walk controls for 4-level long-mode paging; `None` for any other mode
    /// (paging off, legacy/PAE, LA57, SMAP: not supported by this slice), and
    /// for a CR3 with PWT/PCD or other non-address bits set (memory types of the
    /// root table are not modelled; refused rather than ignored).
    pub fn control(&self) -> Option<Control> {
        let long = self.cr0 & (CR0_PE | CR0_PG) == CR0_PE | CR0_PG
            && self.cr4 & CR4_PAE != 0
            && self.cr4 & (CR4_LA57 | CR4_SMAP) == 0
            && self.efer & (EFER_LME | EFER_LMA) == EFER_LME | EFER_LMA;
        (long && matches!(self.cpl, 0 | 3) && self.cr3 & !paging::PA_MASK == 0).then_some(Control {
            cr3: self.cr3,
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
pub const REPLY_ADMITTED: u64 = 1;

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
    /// A write to a guest-physical page code was translated from (until per-page invalidation exists).
    pub const UNSUPPORTED_SMC: u64 = 6;
    /// Shadow installation failed (capacity after eviction, memory outside guest RAM).
    pub const SHADOW: u64 = 7;
    /// The same abort recurred after its shadow was installed and left unchanged.
    pub const NO_PROGRESS: u64 = 8;

    pub fn name(code: u64) -> &'static str {
        match code {
            UNSUPPORTED_MODE => "unsupported_mode",
            BAD_REPLY => "bad_reply",
            UNKNOWN_ABORT => "unknown_abort",
            SITE_MISMATCH => "site_mismatch",
            GUEST_FAULT => "guest_fault",
            UNSUPPORTED_SMC => "unsupported_smc",
            SHADOW => "shadow",
            NO_PROGRESS => "no_progress",
            _ => "none",
        }
    }
}

/// Exchange and diagnostics record of the execution mode.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct State {
    pub cpu: Cpu,
    pub request: u64,
    pub reply: u64,
    /// The cache's flush generation (flushes so far), sent with each request: the host never needs to resend an
    /// object the resident holds.
    pub generation: u64,
    /// With a reply: guest-physical pages the staged block was fetched from (`u64::MAX` = unused).
    pub source_gpa: [u64; 2],
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
}
impl State {
    pub const fn new(cpu: Cpu) -> Self {
        Self {
            cpu,
            request: NONE,
            reply: NONE,
            generation: 0,
            source_gpa: [u64::MAX; 2],
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
        }
    }
}
const _: () = assert!(core::mem::size_of::<State>() <= 4096);
const _: () = assert!(STATE_OFFSET + 4096 <= FRAME_OFFSET);
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
            Cpu { cr3: 0x1001, ..cpu },
        ] {
            assert_eq!(bad.control(), None);
        }
    }
}
