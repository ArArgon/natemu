//! Binary code object written by `tools/fexc --object` (JSON format `natemu-fexc-v2`, object version 3).
//!
//! Little-endian, fixed-width, no pointers. One file: [`Header`], the FEX block's code bytes (relocation
//! literal slots zeroed), then the [`Site`], [`PcEntry`] and [`Reloc`] tables at the offsets the header names.
//! Offsets into the code are from the start of the code bytes, which is the start of FEX's block (its 4-byte
//! header included), the same origin as the JSON. This module only defines the layout; admission (bounds,
//! sorting, version, feature checks) belongs to the consumer.
//!
//! Numeric codes are shared with `tools/fexc/shim/fexc_hook.h`; keep them in sync.

pub const MAGIC: [u8; 8] = *b"NFXOBJ\0\x01";
pub const VERSION: u32 = 3;
/// "No value" for a u32 offset field.
pub const NONE32: u32 = u32::MAX;
pub const NO_REG: u8 = 0xff;

/// [`Site::kind`]. Guard and split sites (profile slice1) are check stubs: `brk #1` / `brk #2` words after the block
/// body, each reached only by its check. `GUARD` is a non-canonical address of the access site whose native offset is
/// in [`Site::guest_offset2`] (#GP(0), or #SS(0) for [`SEG_SS`]), or, with `guest_offset2` = [`NONE32`], a
/// non-canonical indirect branch target in `base_reg` (#GP(0)). `SPLIT` is a store wider than one byte (read-modify-write
/// stores included) crossing a 4 KiB page (access site in `guest_offset2`): the runtime writes the access site's data
/// register(s) to both pages or neither, applies the access site's writeback and resumes after that store.
/// At a stub, `base_reg + imm` is the access's address (no index) and the guest state is that of the access site.
/// `DIVIDE` (`brk #3`) is a div/idiv whose divisor is zero or whose quotient does not fit its destination (#DE): no
/// access (`guest_offset2` = [`NONE32`], no address registers, width 0); the guest state is that before the instruction.
/// Push/pop pairs exist while FEX fuses two guest push/pop into one `stp`/`ldp`; `OTHER` is a guest-memory operation
/// the record cannot describe, which a consumer must refuse. `SWAP` is a memory XCHG (implicitly locked): one `swpal`
/// (`data_reg` = [Rt, Rs]), write intent, nothing written if it aborts; an operand crossing 16 bytes takes an alignment
/// fault there (LSE2), which the runtime does not complete.
pub mod kind {
    pub const LOAD: u8 = 0;
    pub const STORE: u8 = 1;
    pub const LOAD_PAIR: u8 = 2;
    pub const STORE_PAIR: u8 = 3;
    pub const PUSH: u8 = 4;
    pub const POP: u8 = 5;
    pub const GUARD: u8 = 6;
    pub const SPLIT: u8 = 7;
    pub const PUSH_PAIR: u8 = 8;
    pub const POP_PAIR: u8 = 9;
    pub const OTHER: u8 = 10;
    pub const DIVIDE: u8 = 11;
    pub const SWAP: u8 = 12;
}
/// [`Site::intent`].
pub const INTENT_READ: u8 = 0;
pub const INTENT_WRITE: u8 = 1;
/// [`Site::rmw`]: the load or the store half of a read-modify-write instruction.
pub const RMW_NONE: u8 = 0;
pub const RMW_LOAD: u8 = 1;
pub const RMW_STORE: u8 = 2;
/// [`Site::segment`]: decides #GP(0) (DS) versus #SS(0) (SS) for a non-canonical address.
pub const SEG_DS: u8 = 0;
pub const SEG_SS: u8 = 1;
/// [`Site::extend`] of the index register.
pub const EXT_NONE: u8 = 0;
pub const EXT_UXTW: u8 = 1;
pub const EXT_SXTW: u8 = 2;
pub const EXT_SXTX: u8 = 3;
/// [`Site::writeback`]: the access also updates its base register by `writeback_delta`.
pub const WB_NONE: u8 = 0;
pub const WB_PRE: u8 = 1;
pub const WB_POST: u8 = 2;
/// [`Site::data_class`].
pub const CLASS_GPR: u8 = 0;
pub const CLASS_FPR: u8 = 1;
/// [`Site::nzcv`]: where the guest NZCV is at the site.
pub const NZCV_PSTATE: u8 = 0;
pub const NZCV_FRAME: u8 = 1;
/// [`Site::ordering`].
pub const ORD_PLAIN: u8 = 0;
pub const ORD_ACQUIRE: u8 = 1;
pub const ORD_RELEASE: u8 = 2;
/// [`Site::completed_effects`]: `NONE` means guest state at the site is the state before the guest instruction and
/// no effect of it is committed (guaranteed by the precision patches); `UNVERIFIED` is what S1a emits.
pub const EFFECTS_NONE: u8 = 0;
pub const EFFECTS_UNVERIFIED: u8 = 1;

/// [`Header::profile`]: the fexc compile profile.
pub const PROFILE_DEFAULT: u8 = 0;
pub const PROFILE_SLICE1: u8 = 1;

/// [`Header::settings`] bits.
pub const SETTING_TSO: u8 = 1;
pub const SETTING_MULTIBLOCK: u8 = 2;
pub const SETTING_DENSE_PC: u8 = 4;
pub const SETTING_END_MARKER: u8 = 8;

/// [`Header::end_reason`]: why translation of the block ended. Names are the JSON `guest.end.reason` strings.
pub mod end_reason {
    pub const END_OF_INPUT: u8 = 1;
    pub const BLOCK_ENDING_INSTRUCTION: u8 = 2;
    pub const MAX_INSTRUCTIONS: u8 = 3;
    pub const FEX_INSTRUCTION_LIMIT: u8 = 4;
    pub const SYSTEM_INSTRUCTION: u8 = 5;
    pub const UNSUPPORTED_FORM: u8 = 6;
    pub const DISPATCH_FAILURE: u8 = 7;
    pub const INVALID_INSTRUCTION: u8 = 8;
    pub const PARTIAL_DECODE: u8 = 9;
    pub const NOT_EXECUTABLE: u8 = 10;
    pub const BAD_RELOCATION: u8 = 11;
    pub const UNIMPLEMENTED_INSTRUCTION: u8 = 12;
    /// Any other reason string.
    pub const OTHER: u8 = 255;
}

/// [`PcEntry::cf_inverted`].
pub const CF_NORMAL: u8 = 0;
pub const CF_INVERTED: u8 = 1;
pub const CF_UNKNOWN: u8 = 2;

/// [`Reloc::kind`].
pub const RELOC_GUEST_RIP_LITERAL: u8 = 1;
pub const RELOC_NAMED_SYMBOL_LITERAL: u8 = 2;
pub const RELOC_GUEST_RIP_MOVE: u8 = 3;
pub const RELOC_OTHER: u8 = 255;

#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Header {
    pub magic: [u8; 8],
    pub version: u32,
    /// `size_of::<Header>()` of the writer.
    pub header_size: u32,
    /// Whole file.
    pub total_size: u32,
    pub guest_bitness: u32,
    /// Guest RIP of the block entry.
    pub guest_rip: u64,
    /// Guest bytes and instructions translated.
    pub guest_bytes: u32,
    pub guest_insns: u32,
    pub code_offset: u32,
    pub code_size: u32,
    /// First native instruction after the entry prologue.
    pub body_offset: u32,
    /// `udf #0x420f` marker offset, or [`NONE32`].
    pub end_marker_offset: u32,
    /// End of the instruction region; FEX's tail follows.
    pub code_region_end: u32,
    /// Bytes the prologue subtracts from sp.
    pub spill_bytes: u32,
    pub sites_offset: u32,
    pub sites_count: u32,
    pub pcs_offset: u32,
    pub pcs_count: u32,
    pub relocs_offset: u32,
    pub relocs_count: u32,
    /// Native register numbers of RAX..R15, PF, AF in x86 order (first `sra_count` entries).
    pub sra_gpr: [u8; 20],
    pub sra_count: u8,
    /// [`PROFILE_DEFAULT`] or [`PROFILE_SLICE1`].
    pub profile: u8,
    /// One of [`end_reason`]; the end RIP is `guest_rip + guest_bytes`.
    pub end_reason: u8,
    /// `SETTING_*` bits of the compile.
    pub settings: u8,
    /// Feature profile: bit i is the i-th entry of fexc's feature table (JSON `features.host_features`, in order).
    pub feature_bits: u64,
    /// Entries in that table.
    pub feature_count: u8,
    pub dcache_line_log2: u8,
    pub reserved: [u8; 2],
    /// Offset in the guest-state frame (x28) of the 32-bit slot where a [`NZCV_FRAME`] site finds the instruction's
    /// incoming NZCV (PSTATE layout); [`NONE32`] if the compile has no such sites (default profile).
    pub pre_nzcv_offset: u32,
}

/// One native instruction that performs a guest memory access, sorted by `native_offset` in the table.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Site {
    /// The faulting native instruction.
    pub native_offset: u32,
    /// Guest instruction, as an offset from the block RIP. For push/pop pairs, the first of two.
    pub guest_offset: u32,
    /// Push/pop pairs: the second guest instruction. Guard/split stubs: the native offset of the access site they
    /// cover ([`NONE32`] for a branch-target guard). Else [`NONE32`].
    pub guest_offset2: u32,
    /// Where that guest instruction's translation starts (its PC-table entry).
    pub insn_native_offset: u32,
    /// Constant address offset from the base register as it is before the access, read from the emitted instruction
    /// (pre-index writeback: equals `writeback_delta`; post-index: 0; 0 when an index register is used).
    pub imm: i32,
    pub kind: u8,
    /// Bytes the native instruction accesses (pairs: both registers).
    pub width: u8,
    pub intent: u8,
    pub rmw: u8,
    pub segment: u8,
    /// Native register number; 31 is sp.
    pub base_reg: u8,
    /// [`NO_REG`] if none.
    pub index_reg: u8,
    pub extend: u8,
    pub shift: u8,
    pub writeback: u8,
    pub writeback_delta: i8,
    /// Transfer registers, 31 = xzr; the second is used only by pair kinds.
    pub data_reg: [u8; 2],
    pub data_class: u8,
    pub nzcv: u8,
    /// Guest CF is inverted in PSTATE at the guest instruction's start.
    pub cf_inverted: u8,
    /// An NZCV-writing IR op of this guest instruction precedes the site.
    pub flags_written_before: u8,
    pub ordering: u8,
    pub completed_effects: u8,
    /// 1 if fexc found the access word in the op's emitted range (0 would be a recorder bug; consumers refuse it).
    pub located: u8,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PcEntry {
    pub native_offset: u32,
    pub guest_offset: u32,
    pub cf_inverted: u8,
    pub reserved: [u8; 3],
}

#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Reloc {
    /// Offset of the literal slot (zeroed in the code) or of the first instruction of a move sequence.
    pub offset: u32,
    pub kind: u8,
    /// Destination register of a move sequence.
    pub register: u8,
    /// Literal size in bytes, 0 for a move sequence.
    pub size: u8,
    pub reserved: u8,
    /// Guest RIP for the guest-RIP kinds, else 0.
    pub value: u64,
}

const _: () = assert!(core::mem::size_of::<Header>() == 128);
const _: () = assert!(core::mem::size_of::<Site>() == 40);
const _: () = assert!(core::mem::size_of::<PcEntry>() == 12);
const _: () = assert!(core::mem::size_of::<Reloc>() == 16);
const _: () = assert!(core::mem::align_of::<Header>() == 8);
const _: () = assert!(core::mem::align_of::<Site>() == 4);
const _: () = assert!(core::mem::align_of::<Reloc>() == 8);

#[cfg(test)]
mod tests {
    use super::*;
    use core::mem::offset_of;

    #[test]
    fn header_layout() {
        assert_eq!(offset_of!(Header, magic), 0);
        assert_eq!(offset_of!(Header, version), 8);
        assert_eq!(offset_of!(Header, header_size), 12);
        assert_eq!(offset_of!(Header, total_size), 16);
        assert_eq!(offset_of!(Header, guest_bitness), 20);
        assert_eq!(offset_of!(Header, guest_rip), 24);
        assert_eq!(offset_of!(Header, guest_bytes), 32);
        assert_eq!(offset_of!(Header, guest_insns), 36);
        assert_eq!(offset_of!(Header, code_offset), 40);
        assert_eq!(offset_of!(Header, code_size), 44);
        assert_eq!(offset_of!(Header, body_offset), 48);
        assert_eq!(offset_of!(Header, end_marker_offset), 52);
        assert_eq!(offset_of!(Header, code_region_end), 56);
        assert_eq!(offset_of!(Header, spill_bytes), 60);
        assert_eq!(offset_of!(Header, sites_offset), 64);
        assert_eq!(offset_of!(Header, sites_count), 68);
        assert_eq!(offset_of!(Header, pcs_offset), 72);
        assert_eq!(offset_of!(Header, pcs_count), 76);
        assert_eq!(offset_of!(Header, relocs_offset), 80);
        assert_eq!(offset_of!(Header, relocs_count), 84);
        assert_eq!(offset_of!(Header, sra_gpr), 88);
        assert_eq!(offset_of!(Header, sra_count), 108);
        assert_eq!(offset_of!(Header, profile), 109);
        assert_eq!(offset_of!(Header, end_reason), 110);
        assert_eq!(offset_of!(Header, settings), 111);
        assert_eq!(offset_of!(Header, feature_bits), 112);
        assert_eq!(offset_of!(Header, feature_count), 120);
        assert_eq!(offset_of!(Header, dcache_line_log2), 121);
        assert_eq!(offset_of!(Header, reserved), 122);
        assert_eq!(offset_of!(Header, pre_nzcv_offset), 124);
    }

    #[test]
    fn site_layout() {
        assert_eq!(offset_of!(Site, native_offset), 0);
        assert_eq!(offset_of!(Site, guest_offset), 4);
        assert_eq!(offset_of!(Site, guest_offset2), 8);
        assert_eq!(offset_of!(Site, insn_native_offset), 12);
        assert_eq!(offset_of!(Site, imm), 16);
        assert_eq!(offset_of!(Site, kind), 20);
        assert_eq!(offset_of!(Site, width), 21);
        assert_eq!(offset_of!(Site, intent), 22);
        assert_eq!(offset_of!(Site, rmw), 23);
        assert_eq!(offset_of!(Site, segment), 24);
        assert_eq!(offset_of!(Site, base_reg), 25);
        assert_eq!(offset_of!(Site, index_reg), 26);
        assert_eq!(offset_of!(Site, extend), 27);
        assert_eq!(offset_of!(Site, shift), 28);
        assert_eq!(offset_of!(Site, writeback), 29);
        assert_eq!(offset_of!(Site, writeback_delta), 30);
        assert_eq!(offset_of!(Site, data_reg), 31);
        assert_eq!(offset_of!(Site, data_class), 33);
        assert_eq!(offset_of!(Site, nzcv), 34);
        assert_eq!(offset_of!(Site, cf_inverted), 35);
        assert_eq!(offset_of!(Site, flags_written_before), 36);
        assert_eq!(offset_of!(Site, ordering), 37);
        assert_eq!(offset_of!(Site, completed_effects), 38);
        assert_eq!(offset_of!(Site, located), 39);
    }

    #[test]
    fn table_entry_layout() {
        assert_eq!(offset_of!(PcEntry, native_offset), 0);
        assert_eq!(offset_of!(PcEntry, guest_offset), 4);
        assert_eq!(offset_of!(PcEntry, cf_inverted), 8);
        assert_eq!(offset_of!(Reloc, offset), 0);
        assert_eq!(offset_of!(Reloc, kind), 4);
        assert_eq!(offset_of!(Reloc, register), 5);
        assert_eq!(offset_of!(Reloc, size), 6);
        assert_eq!(offset_of!(Reloc, value), 8);
    }

    #[test]
    fn magic_and_version() {
        assert_eq!(&MAGIC[..6], b"NFXOBJ");
        assert_eq!(VERSION, 3);
    }
}
