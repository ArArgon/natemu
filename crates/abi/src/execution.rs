//! General block execution (slice 1): the vCPU's x86 architectural state bank,
//! the record the resident and the host exchange per block, the private layout
//! of the execution mode, and the FEX frame offsets admitted code may touch.
//!
//! Flow: the resident asks the host for code at `Cpu::rip` (`hvc #TRANSLATE_HVC`);
//! the host fetches the bytes through the guest walk, compiles and admits one
//! block into the staging area and replies; the resident places it in the code
//! slot, enters it and, at its exit through the dispatcher gate, writes the
//! state back to the bank. Shadow misses are served in the resident.
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
/// Generated code's native stack (spill area below `crate::STACK_TOP`).
pub const BLOCK_STACK_BYTES: u32 = 0x2000;
/// Guest-physical RAM `[0, GUEST_RAM_BYTES)`: backing offset and resident alias.
pub const GUEST_RAM_OFFSET: usize = RAM_SIZE;
pub const GUEST_RAM_BYTES: usize = 0x10_0000;
pub const GUEST_RAM_VA: u64 = PRIVATE + GUEST_RAM_OFFSET as u64;
/// Backing bytes the execution mode needs: window 0 plus guest RAM.
pub const BACKING_BYTES: usize = GUEST_RAM_OFFSET + GUEST_RAM_BYTES;

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
    /// `(offset, bytes)` of every admitted access.
    pub const FIELDS: [(u32, u32); 4] = [(BLOCK_HEADER, 8), (RIP, 8), (PRE_NZCV, 4), (DISPATCHER_LOOP_TOP, 8)];
    pub const BYTES: usize = 4096;
}
/// Native offset of a FEX block's entry: after its 4-byte `JITCodeHeader`.
pub const ENTRY_OFFSET: u32 = 4;

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
    /// A write to a page the current block was translated from.
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
    /// Guest linear and physical pages the admitted block was fetched from
    /// (`u64::MAX` = unused): kept read-only while it runs.
    pub source_va: [u64; 2],
    pub source_gpa: [u64; 2],
    pub stop: u64,
    /// Guest exception due at a stop: vector, CR2 (#PF; else 0), error code, RIP.
    pub fault: [u64; 4],
    /// Native abort at a stop: ESR, FAR, ELR (slot offset).
    pub native: [u64; 3],
    /// The last retry: native PC, FAR and the shadow descriptor then installed at FAR.
    pub last_retry: [u64; 3],
    /// Blocks entered, shadow-miss retries, of which read-only → writable upgrades,
    /// shadow evictions on capacity, page-crossing stores completed by the split service.
    pub blocks: u64,
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
            source_va: [u64::MAX; 2],
            source_gpa: [u64::MAX; 2],
            stop: 0,
            fault: [0; 4],
            native: [0; 3],
            last_retry: [0; 3],
            blocks: 0,
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
