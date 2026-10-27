//! Bounded IA-32e transactions and descriptor-checked direct far transfers.
use crate::{
    families::controls::{self as c, ControlOp, Error, Instruction},
    paging,
    recovery::GuestState,
};
pub const OFFSET: usize = 0x25000;
pub const TABLE_OFFSET: usize = 0x7a000;
pub const TABLE_BYTES: usize = 4 * 4096;
pub const ROOT: u64 = 0x6000;
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Visit {
    pub cs_before: [u64; 4],
    pub cs_after: [u64; 4],
    pub mode_before: u64,
    pub mode_after: u64,
    pub pointer_address: u64,
    pub pointer_bytes: [u8; 16],
    pub descriptor_address: u64,
    pub descriptor: u64,
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct State {
    pub gdtr: [u64; 2],
    pub tr: [u64; 4],
    pub pointer: u64,
    pub reads: u64,
    pub pointer_reads: u64,
    pub descriptor_reads: u64,
    pub mutations: u64,
    pub visits: [Visit; c::MAX_VISITS],
}
impl State {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
const _: () = assert!(core::mem::size_of::<State>() <= 4096);

pub fn mode(g: &GuestState) -> Option<u64> {
    let (cr0, cr4, efer) = (g.control[0], g.control[3], g.control[4]);
    if cr0 & !0x80010011 != 0 || cr0 & 0x11 != 0x11 || cr4 & !0x6a0 != 0 || efer & !0xd01 != 0 {
        return None;
    }
    let active = cr0 & (1 << 31) != 0 && efer & 0x100 != 0;
    if (efer & 0x400 != 0) != active {
        return None;
    }
    if active {
        if cr4 & 32 == 0 {
            return None;
        }
        return match g.segments[0][3] & 0x6000 {
            0x2000 => Some(4),
            0x4000 => Some(3),
            _ => None,
        };
    }
    if g.segments[0][3] & 0x6000 != 0x4000 {
        return None;
    }
    Some(if cr0 >> 31 == 0 {
        0
    } else if cr4 & 32 != 0 {
        2
    } else {
        1
    })
}
pub fn prepare(g: &GuestState, tr: &[u64; 4], cpl: u64, op: ControlOp) -> Result<[u64; 8], Error> {
    if cpl != 0 {
        return Err(Error::GeneralProtection);
    }
    let long = mode(g) == Some(4);
    let value = if long {
        g.gpr[op.gpr]
    } else {
        g.gpr[op.gpr] & 0xffff_ffff
    };
    if long && matches!(op.cr, 0 | 4) && value >> 32 != 0 {
        return Err(Error::GeneralProtection);
    }
    let mut next = c::prepare(g, cpl, op, true)?;
    if op.cr == 3 {
        if g.control[4] & 0x400 != 0 {
            if value & !(paging::PA_MASK | 0xfff) != 0 {
                return Err(Error::GeneralProtection);
            }
            if value & 0x18 != 0 {
                return Err(Error::Unsupported);
            }
        }
        next[2] = value;
    }
    if op.cr == 4 && g.control[4] & 0x400 != 0 && next[3] & 32 == 0 {
        return Err(Error::GeneralProtection);
    }
    if op.cr == 0 {
        if long && next[0] & (1 << 31) == 0 {
            return Err(Error::GeneralProtection);
        }
        if next[0] & (1 << 31) != 0 && next[4] & 0x100 != 0 {
            if next[3] & 32 == 0 {
                return Err(Error::GeneralProtection);
            }
            if g.control[0] & (1 << 31) == 0 {
                if g.segments[0][3] & 0x2000 != 0 || matches!(tr[3] & 15, 1 | 3) {
                    return Err(Error::GeneralProtection);
                }
                if !matches!(tr[3] & 15, 9 | 11) {
                    return Err(Error::Unsupported);
                }
            }
            next[4] |= 0x400;
        } else {
            next[4] &= !0x400;
        }
    }
    Ok(next)
}
pub fn write_efer(g: &GuestState, cpl: u64) -> Result<[u64; 8], Error> {
    if cpl != 0 {
        return Err(Error::GeneralProtection);
    }
    if g.gpr[1] as u32 != 0xc0000080 {
        return Err(Error::Unsupported);
    }
    let value = ((g.gpr[2] & 0xffff_ffff) << 32) | (g.gpr[0] & 0xffff_ffff);
    if value & !0xd01 != 0 || (g.control[0] & (1 << 31) != 0 && (value ^ g.control[4]) & 0x100 != 0)
    {
        return Err(Error::GeneralProtection);
    }
    let mut next = g.control;
    next[4] = (value & !0x400) | (next[4] & 0x400);
    Ok(next)
}
#[derive(Debug, PartialEq, Eq)]
pub enum TransferError {
    Fault { vector: u64, error: u64 },
    Unsupported,
}
fn gp(error: u64) -> TransferError {
    TransferError::Fault { vector: 13, error }
}
pub fn descriptor_address(gdtr: &[u64; 2], selector: u64) -> Result<u64, TransferError> {
    if selector & !3 == 0 {
        return Err(gp(0));
    }
    if selector & 4 != 0 {
        return Err(TransferError::Unsupported);
    }
    let offset = selector & !7;
    if offset + 7 > gdtr[1] {
        return Err(gp(selector & !3));
    }
    gdtr[0]
        .checked_add(offset)
        .ok_or(TransferError::Unsupported)
}
pub fn code_segment(
    g: &GuestState,
    cpl: u64,
    selector: u64,
    descriptor: u64,
    target: u64,
) -> Result<[u64; 4], TransferError> {
    let error = selector & !3;
    let access = (descriptor >> 40) & 255;
    if access & 16 == 0 {
        return Err(TransferError::Unsupported);
    }
    if access & 8 == 0 {
        return Err(gp(error));
    }
    let attrs = access | ((descriptor >> 40) & 0xf000);
    if g.control[4] & 0x400 != 0 && attrs & 0x6000 == 0x6000 {
        return Err(gp(error));
    }
    if access & 4 != 0 {
        return Err(TransferError::Unsupported);
    }
    if selector & 3 > cpl || (access >> 5) & 3 != cpl {
        return Err(gp(error));
    }
    if access & 128 == 0 {
        return Err(TransferError::Fault { vector: 11, error });
    }
    let base = ((descriptor >> 16) & 0xffffff) | ((descriptor >> 32) & 0xff000000);
    let raw_limit = (descriptor & 0xffff) | ((descriptor >> 32) & 0xf0000);
    let limit = if attrs & 0x8000 != 0 {
        (raw_limit << 12) | 4095
    } else {
        raw_limit
    };
    let long = g.control[4] & 0x400 != 0 && attrs & 0x2000 != 0;
    if (long && !paging::canonical(target)) || (!long && target > limit) {
        return Err(gp(0));
    }
    // Finite flat, accessed nonconforming descriptors; other legal forms need their own lowering.
    if base != 0 || access & 1 == 0 || (!long && attrs & 0x4000 == 0) {
        return Err(TransferError::Unsupported);
    }
    Ok([(selector & !3) | cpl, base, limit, attrs])
}
const WRMSR: Instruction = Instruction {
    bytes: [0x0f, 0x30, 0, 0, 0, 0, 0, 0],
    len: 2,
};
const LOAD64: Instruction = Instruction {
    bytes: [0x48, 0x8b, 3, 0, 0, 0, 0, 0],
    len: 3,
};
const FAR_MEMORY: Instruction = Instruction {
    bytes: [0x48, 0xff, 0x2e, 0, 0, 0, 0, 0],
    len: 3,
};
const fn far(selector: u16) -> Instruction {
    Instruction {
        bytes: [0xea, 0, 0, 0, 0, selector as u8, (selector >> 8) as u8, 0],
        len: 7,
    }
}
const BASE: [Instruction; 8] = [
    c::mov(0x620),
    c::cr(4),
    c::mov(0x100),
    WRMSR,
    c::mov(0x6000),
    c::cr(3),
    c::mov(0x80010011),
    c::cr(0),
];
struct Program {
    code: [Instruction; c::MAX_VISITS],
    len: usize,
}
const fn join(a: &[Instruction], b: &[Instruction]) -> Program {
    let mut p = Program {
        code: [Instruction {
            bytes: [0; 8],
            len: 0,
        }; c::MAX_VISITS],
        len: a.len() + b.len(),
    };
    assert!(p.len <= c::MAX_VISITS);
    let mut i = 0;
    while i < a.len() {
        p.code[i] = a[i];
        i += 1;
    }
    let mut j = 0;
    while j < b.len() {
        p.code[i + j] = b[j];
        j += 1;
    }
    p
}
pub fn instructions(profile: u64) -> Option<&'static [Instruction]> {
    const PROGRAMS: [Program; 17] = [
        join(
            &BASE,
            &[
                far(0x18),
                LOAD64,
                FAR_MEMORY,
                c::LOAD,
                c::mov(0x10011),
                c::cr(0),
                c::LOAD,
            ],
        ),
        join(
            &[],
            &[
                c::mov(0x620),
                c::cr(4),
                c::mov(0x80010011),
                c::cr(0),
                c::LOAD,
                c::mov(0x10011),
                c::cr(0),
                c::mov(0x100),
                WRMSR,
                c::mov(0x6000),
                c::cr(3),
                c::mov(0x80010011),
                c::cr(0),
                far(0x18),
                LOAD64,
                FAR_MEMORY,
                c::LOAD,
                c::mov(0x10011),
                c::cr(0),
                c::LOAD,
            ],
        ),
        join(
            &[],
            &[
                c::LOAD,
                c::mov(0x80010011),
                c::cr(0),
                far(0x18),
                LOAD64,
                FAR_MEMORY,
                c::LOAD,
                c::mov(0x10011),
                c::cr(0),
                c::JUMP,
            ],
        ),
        join(&BASE, &[c::mov(0), WRMSR]),
        join(&[], &[c::mov(0x100), WRMSR, c::mov(0x80010011), c::cr(0)]),
        join(&BASE, &[c::mov(0x600), c::cr(4)]),
        join(&BASE, &[far(0x18), c::mov(0x10011), c::cr(0)]),
        join(&[], &[c::mov(0xffffffff), WRMSR]),
        join(&BASE, &[far(0x18), far(0x18)]),
        join(&BASE, &[far(0x38)]),
        join(&BASE, &[far(0x18), LOAD64]),
        join(&BASE, &[far(0x18), LOAD64]),
        join(&BASE, &[far(0x18), LOAD64, FAR_MEMORY, c::LOAD]),
        join(&BASE, &[far(0x18), LOAD64]),
        join(&BASE, &[far(0x18), LOAD64]),
        join(&BASE, &[far(0x10)]),
        join(&BASE, &[far(0x18), LOAD64, FAR_MEMORY, LOAD64]),
    ];
    let p = PROGRAMS.get(usize::try_from(profile.checked_sub(22)?).ok()?)?;
    Some(&p.code[..p.len])
}
#[cfg(test)]
mod tests {
    use super::*;
    fn guest() -> GuestState {
        let mut g = GuestState::seed(&crate::NativeFrame::ZERO);
        g.control[0] = 0x10011;
        g.control[2] = ROOT;
        g.control[3] = 0x620;
        g.control[4] = 0;
        g.segments[0] = [8, 0, 0xffffffff, 0xc09b];
        g.gpr[1] = 0xc0000080;
        g.gpr[2] = 0;
        g
    }
    #[test]
    fn lma_is_derived_and_failed_controls_preserve_inputs() {
        let mut g = guest();
        g.gpr[0] = 0x500;
        g.control = write_efer(&g, 0).unwrap();
        assert_eq!(g.control[4], 0x100);
        g.gpr[0] = 0x80010011;
        let op = c::decode(&[0x0f, 0x22, 0xc0]).unwrap();
        assert_eq!(
            prepare(&g, &[0x28, 0, 0x67, 0x83], 0, op),
            Err(Error::GeneralProtection)
        );
        g.control = prepare(&g, &[0x28, 0, 0x67, 0x8b], 0, op).unwrap();
        assert_eq!(mode(&g), Some(3));
        assert_eq!(g.control[4], 0x500);
        g.gpr[0] = 0;
        assert_eq!(write_efer(&g, 0), Err(Error::GeneralProtection));
        g.segments[0][3] = 0xa09b;
        g.gpr[0] = 0x10011;
        assert_eq!(
            prepare(&g, &[0x28, 0, 0x67, 0x8b], 0, op),
            Err(Error::GeneralProtection)
        );
        g.gpr[0] = 1 << 32 | 0x80010011;
        assert_eq!(
            prepare(&g, &[0x28, 0, 0x67, 0x8b], 0, op),
            Err(Error::GeneralProtection)
        );
    }
    #[test]
    fn far_validation_priorities_and_commit_image() {
        let mut g = guest();
        g.control[0] |= 1 << 31;
        g.control[4] = 0x500;
        assert_eq!(descriptor_address(&[0x100200, 0x37], 0), Err(gp(0)));
        assert_eq!(descriptor_address(&[0x100200, 0x37], 0x38), Err(gp(0x38)));
        assert_eq!(
            code_segment(&g, 0, 0x18, 0x00af9b000000ffff, c::RIP).unwrap(),
            [0x18, 0, 0xffffffff, 0xa09b]
        );
        assert_eq!(
            code_segment(&g, 0, 0x18, 0x00ef1b000000ffff, c::RIP),
            Err(gp(0x18))
        );
        assert_eq!(
            code_segment(&g, 0, 0x1b, 0x00af1b000000ffff, c::RIP),
            Err(gp(0x18))
        );
        assert_eq!(
            code_segment(&g, 0, 0x18, 0x00af1b000000ffff, c::RIP),
            Err(TransferError::Fault {
                vector: 11,
                error: 0x18
            })
        );
        assert_eq!(
            code_segment(&g, 0, 0x18, 0x00af9b000000ffff, 0x800000000000),
            Err(gp(0))
        );
        assert_eq!(
            code_segment(&g, 0, 8, 0x00cf9b000000ffff, 0x100000000),
            Err(gp(0))
        );
        assert_eq!(
            code_segment(&g, 0, 0x18, 0x00af9a000000ffff, c::RIP),
            Err(TransferError::Unsupported)
        );
    }
}
