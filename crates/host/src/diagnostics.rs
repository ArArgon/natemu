//! JSON renderings of architectural state records shared by every report.
use super::*;
use natemu_abi::recovery::GuestState;

pub(crate) fn frame_json(f: &NativeFrame) -> String {
    format!(
        "{{\"x\":{:?},\"q\":{:?},\"sp\":{},\"nzcv\":{},\"fpcr\":{},\"fpsr\":{}}}",
        f.x, f.q, f.sp, f.nzcv, f.fpcr, f.fpsr
    )
}

pub(crate) fn guest_json(g: &GuestState) -> String {
    format!(
        "{{\"gpr\":{:?},\"rip\":{},\"rflags\":{},\"ymm\":{:?},\"mxcsr\":{},\"x87\":[{},{},{},{},{},{}],\"x87_regs\":{:?},\"control\":{:?},\"segments\":{:?},\"reserved\":{}}}",
        g.gpr,
        g.rip,
        g.rflags,
        g.ymm,
        g.mxcsr,
        g.x87_control,
        g.x87_status,
        g.x87_tag,
        g.x87_opcode,
        g.x87_ip,
        g.x87_dp,
        g.x87_regs,
        g.control,
        g.segments,
        g.reserved
    )
}

/// The 512 little-endian words of the 4 KiB table page at `offset`.
pub(crate) fn table_words(memory: &Memory, offset: usize) -> [u64; 512] {
    let bytes = memory.read::<4096>(offset);
    core::array::from_fn(|i| u64::from_le_bytes(bytes[i * 8..i * 8 + 8].try_into().unwrap()))
}
