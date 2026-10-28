//! Admission of fexc code objects (`natemu_abi::code_object`) for the execution
//! mode, before anything reaches the VM: header, profile, sizes and offsets,
//! feature profile against the vCPU's ID registers, sorted in-body access sites
//! of supported kinds and guest-RIP relocations of the requested RIP; then the
//! code itself ([`structure`]): every instruction decoded, frame accesses, address
//! guards, split and #DE checks, exits and indirect branches verified on the words.
//! Literal slots are resolved here, and the literal exits returned as link points;
//! the resident fills the frame's native pointers itself.
use natemu_abi::code_object::{self as object, Header, PcEntry, Reloc, Site};
use natemu_abi::execution::{ENTRY_OFFSET, LinkPoint, MAX_LINK_POINTS, SRA, STAGING_BYTES, cache::MAX_BLOCK_BYTES, frame};

mod structure;

/// The vCPU ID registers features are checked against, in [`Field::register`] order.
pub(crate) const ID_REGISTERS: [(&str, u16); 5] = [
    ("ID_AA64ISAR0_EL1", 0xc030),
    ("ID_AA64ISAR1_EL1", 0xc031),
    ("ID_AA64PFR0_EL1", 0xc020),
    ("ID_AA64MMFR0_EL1", 0xc038),
    ("ID_AA64MMFR1_EL1", 0xc039),
];
const ISAR0: usize = 0;
const ISAR1: usize = 1;
const PFR0: usize = 2;
const MMFR0: usize = 3;
const MMFR1: usize = 4;

/// What a fexc feature bit needs from the vCPU.
#[derive(Clone, Copy)]
enum Need {
    /// A code-generation setting or guest-visible x86 feature: nothing.
    Nothing,
    /// ID register fields (register, shift, minimum value), all required.
    Fields(&'static [(usize, u32, u64)]),
    /// Needs a register this host cannot read (ID_AA64ISAR2, DCZID, SVE length): refused.
    Unverifiable,
}
/// fexc's feature table (`fexc.cpp FeatureTable`), in object bit order.
const FEATURES: [(&str, Need); 32] = {
    use Need::*;
    [
        ("atomics_lse", Fields(&[(ISAR0, 20, 2)])),
        ("lrcpc", Fields(&[(ISAR1, 20, 1)])),
        ("lrcpc2", Fields(&[(ISAR1, 20, 2)])),
        ("sve128", Fields(&[(PFR0, 32, 1)])),
        ("sve256", Unverifiable),
        ("preserve_all_abi", Nothing),
        ("rpres", Unverifiable),
        ("wfxt", Unverifiable),
        ("flagm", Fields(&[(ISAR0, 52, 1)])),
        ("flagm2", Fields(&[(ISAR0, 52, 2)])),
        ("fcma", Fields(&[(ISAR1, 16, 1)])),
        ("crc32", Fields(&[(ISAR0, 16, 1)])),
        ("aes", Fields(&[(ISAR0, 4, 1)])),
        ("sha", Fields(&[(ISAR0, 8, 1), (ISAR0, 12, 1)])),
        ("pmull128", Fields(&[(ISAR0, 4, 2)])),
        ("rng", Fields(&[(ISAR0, 60, 1)])),
        ("cssc", Unverifiable),
        ("afp", Fields(&[(MMFR1, 44, 1)])),
        ("frintts", Fields(&[(ISAR1, 32, 1)])),
        ("mops", Unverifiable),
        ("i8mm", Fields(&[(ISAR1, 52, 1)])),
        ("dotprod", Fields(&[(ISAR0, 44, 1)])),
        ("ecv", Fields(&[(MMFR0, 60, 1)])),
        ("clzero", Unverifiable),
        ("svebitperm", Unverifiable),
        ("float_exceptions", Unverifiable),
        ("cache_maintenance_ops", Nothing),
        ("prefer_zva_for_vzero", Unverifiable),
        ("x86_avx", Nothing),
        ("x86_aes256", Nothing),
        ("x86_3dnow", Nothing),
        ("x86_sse4a", Nothing),
    ]
};

/// Feature bits the vCPU with these ID register values can execute.
pub(crate) fn supported_features(id: &[u64; ID_REGISTERS.len()]) -> u64 {
    let field = |&(r, shift, min): &(usize, u32, u64)| (id[r] >> shift) & 15 >= min;
    (FEATURES.iter().enumerate())
        .filter(|(_, (_, need))| match need {
            Need::Nothing => true,
            Need::Fields(fields) => fields.iter().all(field),
            Need::Unverifiable => false,
        })
        .fold(0, |bits, (i, _)| bits | 1 << i)
}
/// fexc `--feature` overrides that turn off every feature the vCPU lacks.
pub(crate) fn feature_overrides(supported: u64) -> Vec<String> {
    (FEATURES.iter().enumerate())
        .filter(|&(i, _)| supported & 1 << i == 0)
        .map(|(_, (name, _))| format!("{name}=0"))
        .collect()
}

fn record<T: Copy>(bytes: &[u8], offset: usize) -> T {
    assert!(offset + size_of::<T>() <= bytes.len());
    unsafe { std::ptr::read_unaligned(bytes.as_ptr().add(offset).cast()) }
}
/// `count` records of `T` at `offset`, aligned and inside `[from, to)`.
fn table<T: Copy>(bytes: &[u8], offset: u32, count: u32, from: usize, to: usize) -> Result<Vec<T>, String> {
    let (offset, count) = (offset as usize, count as usize);
    let end = count.checked_mul(size_of::<T>()).and_then(|n| n.checked_add(offset));
    if offset < from || end.is_none_or(|end| end > to) || offset % align_of::<T>() != 0 {
        return Err(format!("table at {offset:#x} x{count} outside the object"));
    }
    Ok((0..count).map(|i| record(bytes, offset + i * size_of::<T>())).collect())
}

/// Feature bit of FlagM (the contract requires it: FEX emits CFINV/RMIF).
const FLAGM: u64 = 1 << 8;

pub(crate) struct Admitted {
    /// The object with its guest-RIP literals resolved.
    pub object: Vec<u8>,
    /// Guest bytes the block translated from `rip`.
    pub guest_bytes: u32,
    /// Guest instructions in it.
    pub guest_insns: u32,
    /// Why translation ended ([`object::end_reason`]).
    pub end_reason: u8,
    /// Its literal exits, the only words the resident's linker may rewrite.
    pub links: Vec<LinkPoint>,
}

/// Direct branch target of the x86 instruction `insn` ending at `end`
/// (jmp/jcc rel8/rel32, loopne/loope/loop/jrcxz rel8, call rel32, REX prefixes allowed), if it is one.
fn branch_target(insn: &[u8], end: u64) -> Option<u64> {
    let start = insn.iter().position(|b| b & 0xf0 != 0x40)?;
    let rel = match &insn[start..] {
        [0xeb | 0x70..=0x7f | 0xe0..=0xe3, r] => *r as i8 as i64,
        [0xe9 | 0xe8, r @ ..] | [0x0f, 0x80..=0x8f, r @ ..] if r.len() == 4 => i32::from_le_bytes(r.try_into().unwrap()) as i64,
        _ => return None,
    };
    Some(end.wrapping_add(rel as u64))
}

/// Admits `bytes`, an fexc `--object` compiled at `rip` from `source` (the guest
/// bytes given to fexc), for a vCPU executing `supported` feature bits.
pub(crate) fn admit(bytes: &[u8], rip: u64, source: &[u8], supported: u64) -> Result<Admitted, String> {
    let (mut admitted, parts) = admit_records(bytes, rip, source, supported)?;
    let h = &parts.header;
    let code = &admitted.object[h.code_offset as usize..][..h.code_region_end as usize];
    let split = parts.sites.iter().position(is_stub).unwrap_or(parts.sites.len());
    let (accesses, stubs) = parts.sites.split_at(split);
    let c = structure::Code {
        code,
        literals: &parts.literals,
        accesses,
        stubs,
        pcs: &parts.pcs,
        relocs: &parts.relocs,
        source,
        rip,
        guest_bytes: h.guest_bytes,
        targets: &parts.targets,
    };
    let links = structure::check(&c).map_err(|e| format!("object refused: {e}"))?;
    if links.len() > MAX_LINK_POINTS {
        return Err(format!("object refused: {} literal exits, at most {MAX_LINK_POINTS}", links.len()));
    }
    admitted.links = links;
    Ok(admitted)
}

/// The records of an object [`admit_records`] accepted.
struct Parts {
    header: Header,
    sites: Vec<Site>,
    pcs: Vec<PcEntry>,
    relocs: Vec<Reloc>,
    /// Literal slots in the code (offsets from the code start).
    literals: Vec<std::ops::Range<usize>>,
    /// The block end and the direct target of the branch ending it (when it is one).
    targets: Vec<u64>,
}

/// Everything but the code: header, tables, site records, relocations.
fn admit_records(bytes: &[u8], rip: u64, source: &[u8], supported: u64) -> Result<(Admitted, Parts), String> {
    let fail = |what: &str| Err(format!("object refused: {what}"));
    if bytes.len() < size_of::<Header>() || bytes.len() > STAGING_BYTES {
        return fail("size");
    }
    let h: Header = record(bytes, 0);
    if h.magic != object::MAGIC
        || h.version != object::VERSION
        || h.header_size as usize != size_of::<Header>()
        || h.total_size as usize != bytes.len()
    {
        return fail("magic, version or size");
    }
    if h.guest_bitness != 64 || h.profile != object::PROFILE_SLICE1 || h.settings != object::SETTING_DENSE_PC {
        return fail("profile or settings");
    }
    if h.guest_rip != rip {
        return fail("compiled for another RIP");
    }
    if h.feature_count as usize != FEATURES.len() || h.feature_bits & !supported != 0 || h.feature_bits & FLAGM == 0 {
        return fail("feature profile not supported by the vCPU");
    }
    let code = h.code_offset as usize;
    let code_end = code.checked_add(h.code_size as usize).filter(|&end| end <= bytes.len());
    let region = h.code_region_end;
    if code < size_of::<Header>()
        || code % 16 != 0
        || code_end.is_none()
        || h.code_size as usize > MAX_BLOCK_BYTES
        || h.code_size % 4 != 0
        || region > h.code_size
        || h.body_offset < ENTRY_OFFSET
        || h.body_offset > region
        || h.end_marker_offset != object::NONE32
        // ponytail: no spill area (fexc never spilled on the slice-1 corpora); admitting one needs the sp
        // prologue/epilogue and spill-slot accesses verified on every path.
        || h.spill_bytes != 0
        || h.guest_bytes == 0
        || h.guest_bytes as usize > source.len()
        || h.pre_nzcv_offset != frame::PRE_NZCV
    {
        return fail("code layout");
    }
    let code_end = code_end.unwrap();
    if h.sra_gpr[..h.sra_count as usize] != SRA {
        return fail("static register map");
    }
    let sites: Vec<Site> = table(bytes, h.sites_offset, h.sites_count, code_end, bytes.len())?;
    let pcs: Vec<PcEntry> = table(bytes, h.pcs_offset, h.pcs_count, code_end, bytes.len())?;
    let relocs: Vec<Reloc> = table(bytes, h.relocs_offset, h.relocs_count, code_end, bytes.len())?;
    let sorted = |a: &[u32]| a.windows(2).all(|w| w[0] < w[1]);
    // Dense, in emission order: the block entry, every guest instruction, and the
    // block end when code follows the last instruction (guest offset = guest bytes).
    if pcs.len() <= h.guest_insns as usize
        || pcs.windows(2).any(|w| w[0].native_offset > w[1].native_offset || w[0].guest_offset > w[1].guest_offset)
        || pcs.iter().any(|p| p.native_offset < ENTRY_OFFSET || p.native_offset >= region || p.guest_offset > h.guest_bytes)
    {
        return fail("PC table");
    }
    if !sorted(&sites.iter().map(|s| s.native_offset).collect::<Vec<_>>()) {
        return fail("access sites not sorted");
    }
    // Access sites, then the check stubs placed after the body.
    let (accesses, stubs) = sites.split_at(sites.iter().position(is_stub).unwrap_or(sites.len()));
    for s in accesses {
        if !site_fields_valid(s, &h, region)
            || s.guest_offset2 != object::NONE32
            || !matches!(s.kind, object::kind::LOAD | object::kind::STORE | object::kind::LOAD_PAIR | object::kind::STORE_PAIR | object::kind::PUSH | object::kind::POP)
            || !matches!(s.width, 1 | 2 | 4 | 8 | 16)
            || s.intent != intent(s)
        {
            return fail(&format!("access site at native offset {:#x}", s.native_offset));
        }
    }
    let word = |at: u32| u32::from_le_bytes(bytes[code + at as usize..code + at as usize + 4].try_into().unwrap());
    for s in stubs {
        let access = accesses.binary_search_by_key(&s.guest_offset2, |a| a.native_offset).ok().map(|i| &accesses[i]);
        if !is_stub(s) || !site_fields_valid(s, &h, region) || word(s.native_offset) != stub_word(s.kind) || !stub_matches(s, access) {
            return fail(&format!("check stub at native offset {:#x}", s.native_offset));
        }
    }
    // Literal values: the block's own RIP (FEX's tail), its end, or the direct target
    // of a branch that ends it.
    let end = rip.wrapping_add(h.guest_bytes as u64);
    let last = pcs.iter().map(|p| p.guest_offset).filter(|&g| g < h.guest_bytes).max().unwrap_or(0) as usize;
    let target = (h.end_reason == object::end_reason::BLOCK_ENDING_INSTRUCTION)
        .then(|| branch_target(&source[last..h.guest_bytes as usize], end))
        .flatten();
    let plausible = |v: u64| (v == rip || v == end || Some(v) == target) && (structure::canonical(v) || v == end);
    let mut out = bytes.to_vec();
    let mut literals = Vec::new();
    for r in &relocs {
        let at = r.offset as usize;
        match r.kind {
            object::RELOC_GUEST_RIP_LITERAL
                if r.size == 8
                    && at % 4 == 0
                    && at + 8 <= h.code_size as usize
                    && out[code + at..code + at + 8] == [0; 8]
                    && plausible(r.value) =>
            {
                out[code + at..code + at + 8].copy_from_slice(&r.value.to_le_bytes());
                literals.push(at..at + 8);
            }
            // Materialised in the code by fexc for the requested RIP.
            object::RELOC_GUEST_RIP_MOVE if r.size == 0 && at % 4 == 0 && at < region as usize && r.register < 31 => {}
            _ => return fail(&format!("relocation at {at:#x}")),
        }
    }
    let admitted = Admitted { object: out, guest_bytes: h.guest_bytes, guest_insns: h.guest_insns, end_reason: h.end_reason, links: Vec::new() };
    let targets = [Some(end), target].into_iter().flatten().collect();
    Ok((admitted, Parts { header: h, sites, pcs, relocs, literals, targets }))
}

/// The intent a site's kind implies: stores write; a load writes only as the
/// first half of a read-modify-write (its page must be writable for the store).
fn intent(s: &Site) -> u8 {
    use object::kind::*;
    match (s.kind, s.rmw) {
        (STORE | STORE_PAIR | PUSH, object::RMW_NONE | object::RMW_STORE) => object::INTENT_WRITE,
        (LOAD | LOAD_PAIR, object::RMW_LOAD) => object::INTENT_WRITE,
        (LOAD | LOAD_PAIR | POP, object::RMW_NONE) => object::INTENT_READ,
        _ => u8::MAX,
    }
}

/// Fields every site (access or stub) must satisfy.
fn site_fields_valid(s: &Site, h: &Header, region: u32) -> bool {
    let register = |r: u8| r < 31 && r != 28;
    s.native_offset % 4 == 0
        && s.native_offset >= h.body_offset
        && s.native_offset < region
        && s.insn_native_offset <= s.native_offset
        && s.guest_offset < h.guest_bytes
        && s.segment <= object::SEG_SS
        && (register(s.base_reg) || s.base_reg == 31 || (s.kind == object::kind::DIVIDE && s.base_reg == object::NO_REG))
        && (register(s.index_reg) || s.index_reg == object::NO_REG)
        && s.extend <= object::EXT_SXTX
        && s.shift <= 4
        && s.writeback <= object::WB_POST
        && s.data_class == object::CLASS_GPR
        && s.nzcv <= object::NZCV_FRAME
        && s.completed_effects == object::EFFECTS_NONE
        && s.located == 1
}

fn is_stub(s: &Site) -> bool {
    matches!(s.kind, object::kind::GUARD | object::kind::SPLIT | object::kind::DIVIDE)
}
/// `brk #1` (guard), `brk #2` (split) or `brk #3` (divide).
pub(crate) fn stub_word(kind: u8) -> u32 {
    let imm = match kind {
        object::kind::GUARD => 1,
        object::kind::SPLIT => 2,
        _ => 3,
    };
    0xd420_0000 | imm << 5
}
/// A stub's record against the access site it covers (`None`: no site at `guest_offset2`). At a stub the guest
/// state is that of its access, so instruction, flags location, width, intent and data registers are the access's;
/// its address is `base_reg + imm` without index or writeback. A split covers a store wider than one byte whose data
/// registers survive the check (x0/x1 are the check's scratch). A guard without an access is a branch-target guard:
/// the target is in `base_reg`. The address register at a stub is neither x0 (the check's scratch, overwritten by it)
/// nor sp (the checks cannot name it). A divide stub (#DE) has no access and no address.
fn stub_matches(s: &Site, access: Option<&Site>) -> bool {
    use object::kind::*;
    if s.kind == DIVIDE {
        return access.is_none()
            && s.guest_offset2 == object::NONE32
            && (s.base_reg, s.index_reg, s.imm, s.writeback) == (object::NO_REG, object::NO_REG, 0, object::WB_NONE)
            && (s.width, s.intent, s.rmw, s.segment) == (0, object::INTENT_READ, object::RMW_NONE, object::SEG_DS);
    }
    let address_only = s.index_reg == object::NO_REG && s.writeback == object::WB_NONE && !matches!(s.base_reg, 0 | 31);
    match access {
        None => {
            s.kind == GUARD
                && s.guest_offset2 == object::NONE32
                && address_only
                && s.imm == 0
                && (s.width, s.intent, s.rmw, s.segment) == (0, object::INTENT_READ, object::RMW_NONE, object::SEG_DS)
        }
        Some(a) => {
            let same = (s.guest_offset, s.insn_native_offset, s.width, s.intent, s.rmw, s.segment)
                == (a.guest_offset, a.insn_native_offset, a.width, a.intent, a.rmw, a.segment)
                && (s.data_reg, s.data_class, s.nzcv, s.cf_inverted) == (a.data_reg, a.data_class, a.nzcv, a.cf_inverted);
            let store = matches!(a.kind, STORE | STORE_PAIR | PUSH);
            let pair = a.kind == STORE_PAIR;
            let survives = |r: u8| r > 1;
            let split_ok = s.kind != SPLIT
                || (store && a.width > 1 && survives(a.data_reg[0]) && (!pair || survives(a.data_reg[1])));
            address_only && same && split_ok
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The record checks only: the synthetic object's code is no real block (`structure` tests use fexc output).
    fn admit(bytes: &[u8], rip: u64, source: &[u8], supported: u64) -> Result<Admitted, String> {
        admit_records(bytes, rip, source, supported).map(|(a, _)| a)
    }

    const RIP: u64 = 0x10_0000;
    /// `str x0,[x28]`, `ldr x0,[x20]`, `str x0,[x28,#24]`, `ldr x1,[x28,#2920]`, `br x1`, literal.
    fn object() -> Vec<u8> {
        object_with(&[load_site(8)])
    }
    fn load_site(native_offset: u32) -> Site {
        Site {
            native_offset,
            guest_offset: 0,
            guest_offset2: object::NONE32,
            insn_native_offset: 8,
            imm: 0,
            kind: object::kind::LOAD,
            width: 8,
            intent: object::INTENT_READ,
            rmw: object::RMW_NONE,
            segment: object::SEG_DS,
            base_reg: 20,
            index_reg: object::NO_REG,
            extend: object::EXT_NONE,
            shift: 0,
            writeback: object::WB_NONE,
            writeback_delta: 0,
            data_reg: [0, 31],
            data_class: object::CLASS_GPR,
            nzcv: object::NZCV_PSTATE,
            cf_inverted: 1,
            flags_written_before: 0,
            ordering: object::ORD_PLAIN,
            completed_effects: object::EFFECTS_NONE,
            located: 1,
        }
    }
    fn object_with(sites: &[Site]) -> Vec<u8> {
        let words: [u32; 8] = [0, 0xf9000380, 0xf9400280, 0xf9000f80, 0xf945b781, 0xd61f0020, 0, 0];
        let code: Vec<u8> = words.iter().flat_map(|w| w.to_le_bytes()).collect();
        let pc = |native_offset| PcEntry { native_offset, guest_offset: 0, cf_inverted: 1, reserved: [0; 3] };
        let reloc = Reloc { offset: 24, kind: object::RELOC_GUEST_RIP_LITERAL, register: 0, size: 8, reserved: 0, value: RIP + 3 };
        let sites_offset = 128 + code.len();
        let tables = sites_offset + 40 * sites.len();
        let h = Header {
            magic: object::MAGIC,
            version: object::VERSION,
            header_size: 128,
            total_size: (tables + 24 + 16) as u32,
            guest_bitness: 64,
            guest_rip: RIP,
            guest_bytes: 3,
            guest_insns: 1,
            code_offset: 128,
            code_size: code.len() as u32,
            body_offset: 8,
            end_marker_offset: object::NONE32,
            code_region_end: 24,
            spill_bytes: 0,
            sites_offset: sites_offset as u32,
            sites_count: sites.len() as u32,
            pcs_offset: tables as u32,
            pcs_count: 2,
            relocs_offset: tables as u32 + 24,
            relocs_count: 1,
            sra_gpr: [4, 7, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 19, 29, 26, 27, 0, 0],
            sra_count: 18,
            profile: object::PROFILE_SLICE1,
            end_reason: object::end_reason::END_OF_INPUT,
            settings: object::SETTING_DENSE_PC,
            feature_bits: 1 << 8,
            feature_count: 32,
            dcache_line_log2: 4,
            reserved: [0; 2],
            pre_nzcv_offset: frame::PRE_NZCV,
        };
        let mut out = vec![0; h.total_size as usize];
        let put = |out: &mut Vec<u8>, at: usize, bytes: &[u8]| out[at..at + bytes.len()].copy_from_slice(bytes);
        fn raw<T>(v: &T) -> &[u8] {
            unsafe { std::slice::from_raw_parts((v as *const T).cast(), size_of::<T>()) }
        }
        put(&mut out, 0, raw(&h));
        put(&mut out, 128, &code);
        for (i, site) in sites.iter().enumerate() {
            put(&mut out, sites_offset + 40 * i, raw(site));
        }
        put(&mut out, tables, raw(&pc(4)));
        put(&mut out, tables + 12, raw(&pc(8)));
        put(&mut out, tables + 24, raw(&reloc));
        out
    }
    fn with<T: Copy>(mut bytes: Vec<u8>, at: usize, f: impl FnOnce(&mut T)) -> Vec<u8> {
        let mut value: T = record(&bytes, at);
        f(&mut value);
        let raw = unsafe { std::slice::from_raw_parts((&raw const value).cast::<u8>(), size_of::<T>()) };
        bytes[at..at + size_of::<T>()].copy_from_slice(raw);
        bytes
    }
    fn header(f: impl FnOnce(&mut Header)) -> Vec<u8> {
        with(object(), 0, f)
    }
    const ALL: u64 = u64::MAX;
    /// Guest bytes of the test block: `rex jmp $+1` (target RIP + 4).
    const SOURCE: [u8; 3] = [0x48, 0xeb, 0x01];

    #[test]
    fn valid_object_is_admitted_with_resolved_literal() {
        let out = admit(&object(), RIP, &SOURCE, ALL).unwrap().object;
        assert_eq!(out[128 + 24..128 + 32], (RIP + 3).to_le_bytes());
        assert!(admit(&object(), RIP + 1, &SOURCE, ALL).is_err());
    }

    #[test]
    fn truncation_bounds_and_overflow_are_refused() {
        let o = object();
        for len in 0..o.len() {
            assert!(admit(&o[..len], RIP, &SOURCE, ALL).is_err(), "admitted a {len}-byte prefix");
        }
        for bad in [
            header(|h| h.code_size = u32::MAX - 3),
            header(|h| h.code_offset = u32::MAX - 15),
            header(|h| h.sites_count = u32::MAX),
            header(|h| h.sites_offset = u32::MAX - 3),
            header(|h| h.relocs_offset = 128), // inside the code
            header(|h| h.code_region_end = 64),
            header(|h| h.body_offset = 0),
            header(|h| h.spill_bytes = 16),
            header(|h| h.sra_gpr[3] = 4),
            header(|h| h.sra_gpr[0] = 28),
            header(|h| h.version += 1),
            header(|h| h.profile = object::PROFILE_DEFAULT),
            header(|h| h.settings |= object::SETTING_TSO),
        ] {
            assert!(admit(&bad, RIP, &SOURCE, ALL).is_err());
        }
    }

    #[test]
    fn unsorted_or_unsupported_sites_are_refused() {
        assert!(admit(&object_with(&[load_site(8), load_site(12)]), RIP, &SOURCE, ALL).is_ok());
        assert!(admit(&object_with(&[load_site(12), load_site(8)]), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&object_with(&[load_site(8), load_site(8)]), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&header(|h| h.sites_count = 2), RIP, &SOURCE, ALL).is_err());
        let at = record::<Header>(&object(), 0).sites_offset as usize;
        for bad in [
            with(object(), at, |s: &mut Site| s.kind = object::kind::OTHER),
            with(object(), at, |s: &mut Site| s.located = 0),
            with(object(), at, |s: &mut Site| s.completed_effects = object::EFFECTS_UNVERIFIED),
            with(object(), at, |s: &mut Site| s.native_offset = 4),
            with(object(), at, |s: &mut Site| s.base_reg = 28),
            with(object(), at, |s: &mut Site| s.guest_offset = 3),
        ] {
            assert!(admit(&bad, RIP, &SOURCE, ALL).is_err());
        }
    }

    #[test]
    fn feature_profile_must_be_supported_by_the_vcpu() {
        assert!(admit(&object(), RIP, &SOURCE, 1 << 8).is_ok());
        assert!(admit(&object(), RIP, &SOURCE, !(1 << 8)).is_err());
        assert!(admit(&header(|h| h.feature_count = 31), RIP, &SOURCE, ALL).is_err());
        // FlagM present, LSE absent: only Fields/Nothing bits whose fields hold.
        let supported = supported_features(&[1 << 52, 0, 0, 0, 0]);
        assert!(supported & 1 << 8 != 0 && supported & 1 == 0 && supported & 1 << 9 == 0);
        assert!(supported & 1 << 5 != 0 && supported & 1 << 6 == 0);
        assert!(feature_overrides(supported).contains(&"atomics_lse=0".to_string()));
    }

    #[test]
    fn intent_follows_the_site_kind() {
        let at = record::<Header>(&object(), 0).sites_offset as usize;
        let site = |f: fn(&mut Site)| with(object(), at, f);
        assert!(admit(&site(|s| s.intent = object::INTENT_WRITE), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&site(|s| (s.kind, s.intent) = (object::kind::STORE, object::INTENT_READ)), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&site(|s| (s.kind, s.intent) = (object::kind::PUSH, object::INTENT_READ)), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&site(|s| (s.kind, s.intent) = (object::kind::STORE, object::INTENT_WRITE)), RIP, &SOURCE, ALL).is_ok());
        assert!(admit(&site(|s| (s.rmw, s.intent) = (object::RMW_LOAD, object::INTENT_WRITE)), RIP, &SOURCE, ALL).is_ok());
        assert!(admit(&site(|s| (s.rmw, s.intent) = (object::RMW_LOAD, object::INTENT_READ)), RIP, &SOURCE, ALL).is_err());
    }

    #[test]
    fn register_map_flagm_and_literal_values_are_pinned() {
        // RAX and RCX swapped: would silently exchange guest registers.
        assert!(admit(&header(|h| h.sra_gpr.swap(0, 1)), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&header(|h| h.sra_count = 16), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&header(|h| h.feature_bits = 0), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&header(|h| h.guest_bytes = 4), RIP, &SOURCE, ALL).is_err());
        let at = record::<Header>(&object(), 0).relocs_offset as usize;
        let literal = |value: u64, reason: u8| {
            let o = with(object(), at, |r: &mut Reloc| r.value = value);
            with(o, 0, |h: &mut Header| h.end_reason = reason)
        };
        let ending = object::end_reason::BLOCK_ENDING_INSTRUCTION;
        assert!(admit(&literal(RIP, ending), RIP, &SOURCE, ALL).is_ok());
        assert!(admit(&literal(RIP + 4, ending), RIP, &SOURCE, ALL).is_ok()); // jmp target
        assert!(admit(&literal(RIP + 4, object::end_reason::END_OF_INPUT), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&literal(RIP + 5, ending), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&literal(0xdead_0000, ending), RIP, &SOURCE, ALL).is_err());
        // A non-canonical branch target is refused; the fall-through RIP may leave canonical space.
        let top = 0x7fff_ffff_fffd;
        let at_top = |value: u64| with(literal(value, ending), 0, |h: &mut Header| h.guest_rip = top);
        assert!(admit(&at_top(top + 3), top, &SOURCE, ALL).is_ok());
        assert!(admit(&at_top(top + 4), top, &SOURCE, ALL).is_err());
        assert_eq!(branch_target(&[0x48, 0xe9, 0xfb, 0xff, 0xff, 0xff], 0x10), Some(0x0b));
        assert_eq!(branch_target(&[0x0f, 0x84, 1, 0, 0, 0], 0x10), Some(0x11));
        for op in 0xe0..=0xe3 {
            assert_eq!(branch_target(&[op, 0xfe], 0x10), Some(0x0e)); // loopne/loope/loop/jrcxz rel8
        }
        assert_eq!(branch_target(&[0xe4, 0xfe], 0x10), None); // in al, imm8
        assert_eq!(branch_target(&[0x90], 0x10), None);
    }

    /// The load at 8 and a stub at 20 (the `br`'s word replaced by `brk`).
    fn with_stub(stub: Site, word: u32) -> Vec<u8> {
        with(object_with(&[load_site(8), stub]), 128 + 20, |w: &mut u32| *w = word)
    }
    fn stub(kind: u8, access: u32) -> Site {
        Site { native_offset: 20, kind, guest_offset2: access, ..load_site(8) }
    }
    const BRK1: u32 = 0xd420_0020;
    const BRK2: u32 = 0xd420_0040;

    #[test]
    fn check_stubs_must_be_brk_words_covering_a_matching_access() {
        use object::kind::*;
        assert!(admit(&with_stub(stub(GUARD, 8), BRK1), RIP, &SOURCE, ALL).is_ok());
        assert!(admit(&with_stub(stub(GUARD, 8), BRK2), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&with_stub(stub(GUARD, 8), 0xd61f_0020), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&with_stub(stub(GUARD, 12), BRK1), RIP, &SOURCE, ALL).is_err()); // no access there
        assert!(admit(&with_stub(Site { width: 4, ..stub(GUARD, 8) }, BRK1), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&with_stub(Site { segment: object::SEG_SS, ..stub(GUARD, 8) }, BRK1), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&with_stub(Site { nzcv: object::NZCV_FRAME, ..stub(GUARD, 8) }, BRK1), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&with_stub(Site { index_reg: 3, ..stub(GUARD, 8) }, BRK1), RIP, &SOURCE, ALL).is_err());
        // The address register at the stub may differ from the access's (x1 after an add).
        assert!(admit(&with_stub(Site { base_reg: 1, imm: 0, ..stub(GUARD, 8) }, BRK1), RIP, &SOURCE, ALL).is_ok());
        // A split covers only a store wider than a byte whose data register survives the check.
        assert!(admit(&with_stub(stub(SPLIT, 8), BRK2), RIP, &SOURCE, ALL).is_err());
        let store = Site { kind: STORE, intent: object::INTENT_WRITE, data_reg: [4, 0], ..load_site(8) };
        let split = Site { native_offset: 20, kind: SPLIT, guest_offset2: 8, ..store };
        let both = |a: Site, s: Site| with(object_with(&[a, s]), 128 + 20, |w: &mut u32| *w = BRK2);
        assert!(admit(&both(store, split), RIP, &SOURCE, ALL).is_ok());
        assert!(admit(&both(store, Site { kind: GUARD, ..split }), RIP, &SOURCE, ALL).is_err()); // brk #2 for a guard
        assert!(admit(&both(Site { data_reg: [0, 0], ..store }, Site { data_reg: [0, 0], ..split }), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&both(Site { width: 1, ..store }, Site { width: 1, ..split }), RIP, &SOURCE, ALL).is_err());
        // The stub's address register survives the check: not x0 (its scratch), not sp.
        assert!(admit(&both(store, Site { base_reg: 1, ..split }), RIP, &SOURCE, ALL).is_ok());
        assert!(admit(&both(store, Site { base_reg: 0, ..split }), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&both(store, Site { base_reg: 31, ..split }), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&with_stub(Site { base_reg: 0, ..stub(GUARD, 8) }, BRK1), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&with_stub(Site { base_reg: 31, ..stub(GUARD, 8) }, BRK1), RIP, &SOURCE, ALL).is_err());
        // Branch-target guard: no access, width 0, target register.
        let target = Site { width: 0, base_reg: 4, ..stub(GUARD, object::NONE32) };
        assert!(admit(&with_stub(target, BRK1), RIP, &SOURCE, ALL).is_ok());
        assert!(admit(&with_stub(Site { width: 8, ..target }, BRK1), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&with_stub(Site { base_reg: 31, ..target }, BRK1), RIP, &SOURCE, ALL).is_err());
        // Stubs follow every access site.
        let early = with(object_with(&[Site { native_offset: 8, kind: GUARD, guest_offset2: 12, ..load_site(8) }, load_site(12)]), 128 + 8, |w: &mut u32| *w = BRK1);
        assert!(admit(&early, RIP, &SOURCE, ALL).is_err());
    }

    #[test]
    fn divide_stubs_have_no_access_or_address() {
        use object::kind::*;
        let divide = Site { kind: DIVIDE, guest_offset2: object::NONE32, base_reg: object::NO_REG, width: 0, ..stub(DIVIDE, 0) };
        let brk3 = stub_word(DIVIDE);
        assert_eq!(brk3, 0xd420_0060);
        assert!(admit(&with_stub(divide, brk3), RIP, &SOURCE, ALL).is_ok());
        assert!(admit(&with_stub(divide, BRK1), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&with_stub(Site { guest_offset2: 8, ..divide }, brk3), RIP, &SOURCE, ALL).is_err()); // names an access
        assert!(admit(&with_stub(Site { base_reg: 4, ..divide }, brk3), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&with_stub(Site { width: 8, ..divide }, brk3), RIP, &SOURCE, ALL).is_err());
        assert!(admit(&with_stub(Site { segment: object::SEG_SS, ..divide }, brk3), RIP, &SOURCE, ALL).is_err());
        // The base register may be NO_REG only for a divide stub.
        assert!(admit(&with_stub(Site { base_reg: object::NO_REG, ..stub(GUARD, 8) }, BRK1), RIP, &SOURCE, ALL).is_err());
    }

    /// Every object in `$NATEMU_ADMIT_CORPUS` (`NAME.obj` compiled at its header RIP from `NAME.src`) is admitted;
    /// prints the admission time per block. `cargo test --release -p natemu-host corpus -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn corpus() {
        let dir = std::env::var("NATEMU_ADMIT_CORPUS").expect("NATEMU_ADMIT_CORPUS");
        let (mut n, mut bad, mut words, mut time) = (0, 0, 0, std::time::Duration::ZERO);
        let mut entries: Vec<_> = std::fs::read_dir(&dir).unwrap().map(|e| e.unwrap().path()).collect();
        entries.sort();
        for obj in entries.iter().filter(|p| p.extension().is_some_and(|e| e == "obj")) {
            let (bytes, source) = (std::fs::read(obj).unwrap(), std::fs::read(obj.with_extension("src")).unwrap());
            let h: Header = record(&bytes, 0);
            let t = std::time::Instant::now();
            let r = super::admit(&bytes, h.guest_rip, &source, ALL);
            time += t.elapsed();
            n += 1;
            words += h.code_region_end as u64 / 4;
            if let Err(e) = r {
                bad += 1;
                if bad <= 40 {
                    println!("{}: {e}", obj.file_name().unwrap().to_string_lossy());
                }
            }
        }
        println!("{n} objects, {bad} refused; {:.1} us per block, {:.1} ns per code word", time.as_secs_f64() * 1e6 / n as f64, time.as_secs_f64() * 1e9 / words as f64);
        assert_eq!(bad, 0);
    }
}
