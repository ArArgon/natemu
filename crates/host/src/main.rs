use natemu_abi::*;
#[cfg(test)]
use std::mem::size_of;
use std::{env, ffi::c_void, fs, ptr};

mod concurrent;
#[cfg(test)]
#[path = "../../runtime/src/coordinator.rs"]
mod coordinator_tests;
mod i4v;
mod i4v_controls;
mod i4v_off;
mod i4v_off_steps;
mod i4v_stack;
mod recovery;
mod translation;

type Result<T> = std::result::Result<T, String>;

#[repr(C)]
struct HvException {
    syndrome: u64,
    virtual_address: u64,
    physical_address: u64,
}
#[repr(C)]
struct HvExit {
    reason: u32,
    exception: HvException,
}

#[link(name = "Hypervisor", kind = "framework")]
unsafe extern "C" {
    fn hv_vm_create(config: *const c_void) -> i32;
    fn hv_vm_destroy() -> i32;
    fn hv_vm_map(address: *const c_void, ipa: u64, size: usize, flags: u64) -> i32;
    fn hv_vcpu_create(vcpu: *mut u64, exit: *mut *const HvExit, config: *const c_void) -> i32;
    fn hv_vcpu_destroy(vcpu: u64) -> i32;
    fn hv_vcpu_set_reg(vcpu: u64, reg: u32, value: u64) -> i32;
    fn hv_vcpu_get_reg(vcpu: u64, reg: u32, value: *mut u64) -> i32;
    fn hv_vcpu_set_sys_reg(vcpu: u64, reg: u16, value: u64) -> i32;
    fn hv_vcpu_get_sys_reg(vcpu: u64, reg: u16, value: *mut u64) -> i32;
    fn hv_vcpu_run(vcpu: u64) -> i32;
    fn hv_vcpus_exit(vcpus: *mut u64, count: u32) -> i32;
    fn hv_vm_get_max_vcpu_count(count: *mut u32) -> i32;
}
unsafe extern "C" {
    fn mmap(
        address: *mut c_void,
        length: usize,
        prot: i32,
        flags: i32,
        fd: i32,
        offset: i64,
    ) -> *mut c_void;
    fn munmap(address: *mut c_void, length: usize) -> i32;
    fn sys_icache_invalidate(start: *const c_void, length: usize);
    fn getentropy(buffer: *mut c_void, length: usize) -> i32;
}

fn hv(code: i32, operation: &str) -> Result<()> {
    if code == 0 {
        Ok(())
    } else {
        Err(format!("{operation}: {:#x}", code as u32))
    }
}

struct Memory(*mut u8, usize);
impl Memory {
    fn new() -> Result<Self> {
        Self::with_size(RAM_SIZE)
    }
    fn with_size(size: usize) -> Result<Self> {
        let p = unsafe { mmap(ptr::null_mut(), size, 3, 0x1002, -1, 0) };
        if p as isize == -1 {
            return Err(format!("mmap: {}", std::io::Error::last_os_error()));
        }
        Ok(Self(p.cast(), size))
    }
    fn copy(&self, offset: usize, bytes: &[u8]) {
        assert!(
            offset
                .checked_add(bytes.len())
                .is_some_and(|end| end <= self.1)
        );
        unsafe { ptr::copy_nonoverlapping(bytes.as_ptr(), self.0.add(offset), bytes.len()) };
    }
    fn word(&self, offset: usize, value: u64) {
        self.copy(offset, &value.to_le_bytes());
    }
    fn read<const N: usize>(&self, offset: usize) -> [u8; N] {
        assert!(offset + N <= self.1);
        let mut result = [0; N];
        unsafe { ptr::copy_nonoverlapping(self.0.add(offset), result.as_mut_ptr(), N) };
        result
    }
}
impl Drop for Memory {
    fn drop(&mut self) {
        unsafe {
            munmap(self.0.cast(), self.1);
        }
    }
}

// vCPU APIs and destruction stay on this owner thread. Memory outlives the VM.
struct Vm {
    cpu: Option<u64>,
    exit: *const HvExit,
    memory: Memory,
}
impl Vm {
    fn new(memory: Memory) -> Result<Self> {
        unsafe {
            hv(hv_vm_create(ptr::null()), "hv_vm_create")?;
        }
        let mut vm = Self {
            cpu: None,
            exit: ptr::null(),
            memory,
        };
        unsafe {
            hv(hv_vm_map(vm.memory.0.cast(), IPA, RAM_SIZE, 7), "hv_vm_map")?;
            let mut id = 0;
            hv(
                hv_vcpu_create(&mut id, &mut vm.exit, ptr::null()),
                "hv_vcpu_create",
            )?;
            vm.cpu = Some(id);
        }
        Ok(vm)
    }
    fn reg(&self, reg: u32, value: u64) -> Result<()> {
        unsafe {
            hv(
                hv_vcpu_set_reg(self.cpu.unwrap(), reg, value),
                "set register",
            )
        }
    }
    fn sys(&self, reg: u16, value: u64) -> Result<()> {
        unsafe {
            hv(
                hv_vcpu_set_sys_reg(self.cpu.unwrap(), reg, value),
                "set system register",
            )
        }
    }
    fn read_sys(&self, reg: u16) -> Result<u64> {
        let mut value = 0;
        unsafe {
            hv(
                hv_vcpu_get_sys_reg(self.cpu.unwrap(), reg, &mut value),
                "read system register",
            )?;
        }
        Ok(value)
    }
    fn read_reg(&self, reg: u32) -> Result<u64> {
        let mut value = 0;
        unsafe {
            hv(
                hv_vcpu_get_reg(self.cpu.unwrap(), reg, &mut value),
                "read register",
            )?;
        }
        Ok(value)
    }
    fn shared(&self) -> Shared {
        unsafe { ptr::read_volatile(self.memory.0.add(SHARED_OFFSET).cast::<Shared>()) }
    }
    fn run(&self, expected: u64) -> Result<()> {
        let event = self.run_any()?;
        if event != expected {
            return Err(format!(
                "unexpected HVC immediate {event:#x}, expected {expected:#x}"
            ));
        }
        Ok(())
    }
    fn run_any(&self) -> Result<u64> {
        unsafe {
            hv(hv_vcpu_run(self.cpu.unwrap()), "hv_vcpu_run")?;
            let exit = &*self.exit;
            if exit.reason != 1 || exit.exception.syndrome >> 26 != 0x16 {
                let mut pc = 0;
                hv(
                    hv_vcpu_get_reg(self.cpu.unwrap(), 31, &mut pc),
                    "read exit PC",
                )?;
                return Err(format!(
                    "unexpected exit: reason={} syndrome={:#x} PC={pc:#x} VA={:#x} IPA={:#x}",
                    exit.reason,
                    exit.exception.syndrome,
                    exit.exception.virtual_address,
                    exit.exception.physical_address
                ));
            }
            Ok(exit.exception.syndrome & 0xffff)
        }
    }
}
impl Drop for Vm {
    fn drop(&mut self) {
        unsafe {
            if let Some(id) = self.cpu {
                hv_vcpu_destroy(id);
            }
            hv_vm_destroy();
        }
    }
}

fn u64_at(data: &[u8], offset: usize) -> Result<u64> {
    let bytes = data
        .get(offset..offset.checked_add(8).ok_or("ELF offset overflow")?)
        .ok_or("truncated ELF")?;
    Ok(u64::from_le_bytes(bytes.try_into().unwrap()))
}
fn u16_at(data: &[u8], offset: usize) -> Result<u16> {
    let bytes = data.get(offset..offset + 2).ok_or("truncated ELF")?;
    Ok(u16::from_le_bytes(bytes.try_into().unwrap()))
}
fn read_input(path: &str, max: usize) -> Result<Vec<u8>> {
    if fs::metadata(path).map_err(|e| e.to_string())?.len() > max as u64 {
        return Err("oversized input".into());
    }
    let bytes = fs::read(path).map_err(|e| e.to_string())?;
    if bytes.len() > max {
        return Err("input grew beyond limit".into());
    }
    Ok(bytes)
}
fn load_elf(memory: &Memory, data: &[u8]) -> Result<()> {
    if data.get(..7) != Some(b"\x7fELF\x02\x01\x01")
        || u16_at(data, 16)? != 2
        || u16_at(data, 18)? != 183
        || u64_at(data, 24)? != PRIVATE
    {
        return Err("expected static little-endian AArch64 ELF at private entry".into());
    }
    let phoff = usize::try_from(u64_at(data, 32)?).map_err(|_| "ELF offset")?;
    if u16_at(data, 54)? != 56 {
        return Err("unexpected ELF program header size".into());
    }
    let mut spans = Vec::new();
    for i in 0..u16_at(data, 56)? as usize {
        let at = phoff.checked_add(i * 56).ok_or("ELF offset overflow")?;
        let header = data
            .get(at..at.checked_add(56).ok_or("ELF header overflow")?)
            .ok_or("truncated program header")?;
        if u32::from_le_bytes(header[..4].try_into().unwrap()) != 1 {
            continue;
        }
        let file = u64_at(header, 8)?;
        let va = u64_at(header, 16)?;
        let filesz = u64_at(header, 32)?;
        let memsz = u64_at(header, 40)?;
        let end = va.checked_add(memsz).ok_or("ELF address overflow")?;
        let fileend = file.checked_add(filesz).ok_or("ELF file overflow")?;
        if u32::from_le_bytes(header[4..8].try_into().unwrap()) != 5
            || !resident::contains(va, memsz)
            || filesz > memsz
            || fileend > data.len() as u64
        {
            return Err("ELF segment outside resident text".into());
        }
        if spans.iter().any(|&(lo, hi)| va < hi && lo < end) {
            return Err("overlapping ELF segments".into());
        }
        spans.push((va, end));
        memory.copy(
            (va - PRIVATE) as usize,
            &data[file as usize..fileend as usize],
        );
    }
    if !spans.iter().any(|&(lo, hi)| lo == PRIVATE && hi > PRIVATE) {
        return Err("no resident entry segment".into());
    }
    Ok(())
}

fn page_tables(memory: &Memory) {
    let table = |offset| IPA + offset + 3;
    let attrs = 3 | 1 << 10 | 3 << 8;
    memory.word(0x10000, table(0x11000));
    memory.word(0x10000 + 256 * 8, table(0x12000));
    // Low alias exists only for MMU enable and is removed by startup.
    memory.word(0x11000 + 8, IPA | 1 | 1 << 10 | 3 << 8);
    memory.word(0x11000, table(0x15000));
    memory.word(0x15000, table(0x16000));
    memory.word(
        0x16000 + 256 * 8,
        (IPA + DATA_OFFSET as u64) | attrs | (3 << 53),
    );
    memory.word(0x16000 + 258 * 8, (IPA + 0x72000) | attrs | (3 << 53));
    memory.word(0x12000 + 8, table(0x13000));
    memory.word(0x13000, table(0x14000));
    // Private aliases cover only declared text, tables, state and stacks.
    for offset in (0..RAM_SIZE).step_by(4096) {
        let text = resident::executable_offset(offset) || offset == CODE_OFFSET;
        let writable = (0x10000..0x17000).contains(&offset)
            || (SHARED_OFFSET..SHARED_OFFSET + 0x8000).contains(&offset)
            || (BANK_OFFSET..BANK_OFFSET + 0x2000).contains(&offset)
            || (0x70000..0x84000).contains(&offset)
            || (0x50000..0x54000).contains(&offset)
            || (0x60000..0x64000).contains(&offset);
        if text || writable {
            let protection = if text { 1 << 7 } else { 3 << 53 };
            memory.word(
                0x14000 + offset / 4096 * 8,
                (IPA + offset as u64) | attrs | protection,
            );
        }
    }
}

fn frame_json(f: &NativeFrame) -> String {
    format!(
        "{{\"x\":{:?},\"q\":{:?},\"sp\":{},\"nzcv\":{},\"fpcr\":{},\"fpsr\":{}}}",
        f.x, f.q, f.sp, f.nzcv, f.fpcr, f.fpsr
    )
}
fn seed(index: usize) -> (NativeFrame, [u8; 512], [u8; 256]) {
    let (left, right) = [(0, 0), (u64::MAX, 1), (i64::MAX as u64, 1), (1, 2)][index];
    let mut bank = [0; 512];
    for (i, b) in bank.iter_mut().enumerate() {
        *b = (i.wrapping_mul(37) + 19) as u8;
    }
    let mut data = [0; 256];
    for (i, b) in data.iter_mut().enumerate() {
        *b = (i.wrapping_mul(13) + 7) as u8;
    }
    data[..8].copy_from_slice(&(0xcafe_1234_5678_9000u64 + index as u64).to_le_bytes());
    let mut frame = NativeFrame::ZERO;
    let initial = &mut frame;
    for (i, x) in initial.x.iter_mut().enumerate() {
        *x = 0x1234_0000_0000_0000 + (i as u64) * 0x0101_0101;
    }
    for (i, q) in initial.q.iter_mut().enumerate() {
        for (j, b) in q.iter_mut().enumerate() {
            *b = (i * 17 + j * 3 + 1) as u8;
        }
    }
    initial.sp = STACK_TOP;
    initial.x[28] = BANK_VA;
    initial.x[6] = DATA_VA;
    initial.x[11] = DATA_VA + 128;
    initial.x[7] = left;
    initial.x[5] = right;
    initial.nzcv = 0xa0000000;
    initial.fpcr = 0x01000000;
    initial.fpsr = 0x08000001;
    (frame, bank, data)
}
fn run() -> Result<()> {
    let a: Vec<_> = env::args().collect();
    if a.len() == 9 && a[2] == "i4v-address-concurrent" {
        return concurrent::run_address(&a);
    }
    if a.len() == 9 && a[2] == "i4v-stack-boundary" {
        return concurrent::run(&a);
    }
    if a.len() == 9 && a[2] == "i4v-controls-concurrent" {
        return concurrent::run_controls(&a);
    }
    if a.len() == 8 && a[2] == "i4v-controls" {
        return i4v_controls::run(&a);
    }
    if (a.len() == 8 || a.len() == 9) && a[2] == "i4v-off-steps" {
        return i4v_off_steps::run(&a);
    }
    if (a.len() == 9 || a.len() == 10) && a[2] == "i4v-off-live" {
        return i4v_off::run(&a);
    }
    if (a.len() == 9 || a.len() == 10) && a[2] == "i4v-stack-live" {
        return i4v_stack::run(&a);
    }
    if (a.len() == 9 || a.len() == 10) && a[2] == "i4v-live" {
        return i4v::run(&a);
    }
    if a.len() != 7 {
        return Err("usage: natemu-host RUNTIME.elf FEX.arm64 control|live_load|fault VECTOR none|version|profile|size|target|entry|code|address|vcpu N".into());
    }
    if a[2] == "i4" || a[2] == "i4v-mixed" {
        return concurrent::run(&a);
    }
    if a[2] == "i4v" {
        return i4v::run(&a);
    }
    if a[2] == "i2"
        || a[2] == "i3"
        || a[2] == "i3-pressure"
        || a[2] == "i3-fp"
        || a[2] == "i3-helper"
        || a[2] == "i3-partial"
    {
        return translation::run(&a);
    }
    let configured: usize = a[6].parse().map_err(|_| "invalid vCPU count")?;
    if configured != 1 {
        return Err("I1 admits exactly one vCPU; N-vCPU execution is not tested".into());
    }
    let kind = match a[3].as_str() {
        "control" => CONTROL,
        "live_load" => LIVE_LOAD,
        "fault" => FAULT,
        _ => return Err("unknown case".into()),
    };
    let index: usize = a[4].parse().map_err(|_| "invalid vector")?;
    if index >= 4 {
        return Err("unknown input vector".into());
    }
    let fex = read_input(&a[2], 44)?;
    if fex != FEX_BODY {
        return Err("FEX artifact does not match pinned I1 producer bytes".into());
    }
    let memory = Memory::new()?;
    load_elf(&memory, &read_input(&a[1], 2 * 1024 * 1024)?)?;
    page_tables(&memory);
    let mut code = match kind {
        CONTROL => NOP.to_vec(),
        LIVE_LOAD => fex,
        FAULT => UDF.to_vec(),
        _ => unreachable!(),
    };
    code.extend_from_slice(&RET);
    let (frame, bank, data) = seed(index);
    memory.copy(BANK_OFFSET, &bank);
    memory.copy(DATA_OFFSET, &data);
    let mut frames = vec![frame; configured];
    let initial = &mut frames[0];
    let mut object = Object::new(kind);
    match a[5].as_str() {
        "none" => {}
        "version" => object.version += 1,
        "profile" => object.profile += 1,
        "size" => object.size -= 1,
        "target" => object.code_address = DATA_VA,
        "entry" => object.entry_offset = u64::MAX,
        "code" => code[0] ^= 1,
        "address" => initial.x[6] = PRIVATE,
        "vcpu" => object.vcpu_id = 1,
        _ => return Err("unknown rejection mutation".into()),
    }
    memory.copy(CODE_OFFSET, &code);
    let shared = Shared {
        object,
        initial: *initial,
        captured: NativeFrame::ZERO,
        status: 0,
        detail: 0,
        capture_pc: 0,
        esr: 0,
        far: 0,
        elr: 0,
        spsr: 0,
        current_el: 0,
        sctlr: 0,
        actlr: 0,
        tcr: 0,
        service_pc: 0,
        mode: 0,
        lifecycle: natemu_abi::translation::Lifecycle::new(0),
    };
    unsafe {
        ptr::write(memory.0.add(SHARED_OFFSET).cast::<Shared>(), shared);
        sys_icache_invalidate(memory.0.cast(), RAM_SIZE);
    }
    let vm = Vm::new(memory)?;
    let isar0 = vm.read_sys(0xc030)?;
    if (isar0 >> 52) & 15 == 0 {
        return Err("FlagM required for pinned FEX CFINV".into());
    }
    let tcr = 16 | 1 << 8 | 1 << 10 | 3 << 12 | 1 << 23 | 2 << 32;
    vm.reg(31, IPA)?;
    vm.reg(34, 0x3c5)?;
    vm.reg(0, IPA + 0x10000)?;
    vm.reg(1, tcr)?;
    vm.reg(2, 0xff)?;
    vm.sys(0xc080, 0x30d00980)?;
    vm.sys(0xc081, 2)?;
    vm.sys(0xc082, 3 << 20)?;
    vm.sys(0xdf19, 0)?;
    vm.run(0x49)?;
    let result = unsafe { ptr::read_volatile(vm.memory.0.add(SHARED_OFFSET).cast::<Shared>()) };
    if result.status == SUCCESS || result.status == NATIVE_FAULT {
        let mut expected_initial = shared.initial;
        expected_initial.x[16] = CODE_VA;
        expected_initial.x[30] = result.capture_pc;
        if result.initial != expected_initial {
            return Err("resident modified seeded entry state".into());
        }
    }
    let case = if a[5] == "none" {
        a[3].as_str()
    } else {
        "reject"
    };
    println!(
        "{{\"case\":\"{case}\",\"status\":{},\"detail\":{},\"initial\":{},\"captured\":{},\"memory_base\":{DATA_VA},\"memory_before\":{data:?},\"memory_after\":{:?},\"bank_before\":{bank:?},\"bank_after\":{:?},\"object_pc\":{CODE_VA},\"capture_pc\":{},\"fault\":{{\"esr\":{},\"far\":{},\"elr\":{},\"spsr\":{}}},\"profile\":{{\"configured_vcpus\":{configured},\"tested_vcpus\":1,\"current_el\":{},\"sctlr\":{},\"actlr\":{},\"tcr\":{},\"isar0\":{isar0}}}}}",
        result.status,
        result.detail,
        frame_json(&shared.initial),
        frame_json(&result.captured),
        vm.memory.read::<256>(DATA_OFFSET),
        vm.memory.read::<512>(BANK_OFFSET),
        result.capture_pc,
        result.esr,
        result.far,
        result.elr,
        result.spsr,
        result.current_el,
        result.sctlr,
        result.actlr,
        result.tcr
    );
    if result.current_el != 4
        || result.sctlr & 0x1005 != 0x1005
        || result.actlr & 2 != 2
        || result.tcr != tcr
    {
        return Err("runtime machine profile mismatch".into());
    }
    if result.status == 0 || result.status == PANIC {
        return Err(format!(
            "resident failure status={} detail={}",
            result.status, result.detail
        ));
    }
    Ok(())
}
fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn malformed_elf_rejected_without_loading() {
        let memory = Memory::new().unwrap();
        for length in 0..64 {
            assert!(load_elf(&memory, &vec![0; length]).is_err());
        }
    }
    fn resident_elf(segments: &[(u64, u64)]) -> Vec<u8> {
        let payload = 64 + segments.len() * 56;
        let mut data = vec![0; payload + segments.len() * 4];
        data[..7].copy_from_slice(b"\x7fELF\x02\x01\x01");
        data[16..18].copy_from_slice(&2u16.to_le_bytes());
        data[18..20].copy_from_slice(&183u16.to_le_bytes());
        data[24..32].copy_from_slice(&PRIVATE.to_le_bytes());
        data[32..40].copy_from_slice(&64u64.to_le_bytes());
        data[54..56].copy_from_slice(&56u16.to_le_bytes());
        data[56..58].copy_from_slice(&(segments.len() as u16).to_le_bytes());
        for (i, &(address, size)) in segments.iter().enumerate() {
            let at = 64 + i * 56;
            data[at..at + 4].copy_from_slice(&1u32.to_le_bytes());
            data[at + 4..at + 8].copy_from_slice(&5u32.to_le_bytes());
            data[at + 8..at + 16].copy_from_slice(&((payload + i * 4) as u64).to_le_bytes());
            data[at + 16..at + 24].copy_from_slice(&address.to_le_bytes());
            data[at + 32..at + 40].copy_from_slice(&4u64.to_le_bytes());
            data[at + 40..at + 48].copy_from_slice(&size.to_le_bytes());
            data[payload + i * 4..payload + i * 4 + 4].fill(0x40 + i as u8);
        }
        data
    }

    #[test]
    fn resident_elf_loads_boot_and_extended_text() {
        let memory = Memory::new().unwrap();
        let data = resident_elf(&[
            (PRIVATE, resident::BOOT_END as u64),
            (
                PRIVATE + resident::TEXT_OFFSET as u64,
                (resident::TEXT_END - resident::TEXT_OFFSET) as u64,
            ),
        ]);
        load_elf(&memory, &data).unwrap();
        assert_eq!(memory.read::<4>(0), [0x40; 4]);
        assert_eq!(memory.read::<4>(resident::TEXT_OFFSET), [0x41; 4]);
        assert_eq!(memory.read::<4>(resident::BOOT_END), [0; 4]);
        assert_eq!(memory.read::<4>(resident::TEXT_END - 4), [0; 4]);
    }

    #[test]
    fn resident_elf_rejects_gaps_private_tables_and_overflow() {
        for (address, size) in [
            (PRIVATE, resident::BOOT_END as u64 + 1),
            (PRIVATE + resident::BOOT_END as u64, 4),
            (PRIVATE + 0x10000, 4096),
            (PRIVATE + resident::TEXT_OFFSET as u64 - 4, 8),
            (PRIVATE + resident::TEXT_END as u64 - 4, 8),
            (PRIVATE + resident::TEXT_END as u64, 4),
            (u64::MAX - 3, 8),
            (PRIVATE + resident::TEXT_OFFSET as u64, 0),
        ] {
            let memory = Memory::new().unwrap();
            let data = resident_elf(&[(PRIVATE, 4), (address, size)]);
            assert!(
                load_elf(&memory, &data).is_err(),
                "admitted {address:#x}+{size:#x}"
            );
        }
        let memory = Memory::new().unwrap();
        assert!(
            load_elf(
                &memory,
                &resident_elf(&[(PRIVATE + resident::TEXT_OFFSET as u64, 4)])
            )
            .is_err()
        );
        assert!(load_elf(&memory, &resident_elf(&[(PRIVATE, 8), (PRIVATE + 4, 4)])).is_err());
        for flags in [0u32, 4, 6, 7] {
            let mut data = resident_elf(&[(PRIVATE, 4)]);
            data[64 + 4..64 + 8].copy_from_slice(&flags.to_le_bytes());
            assert!(load_elf(&memory, &data).is_err());
        }
    }

    #[test]
    fn private_resident_mapping_has_no_executable_gap() {
        let memory = Memory::new().unwrap();
        page_tables(&memory);
        let leaf = |offset| u64::from_le_bytes(memory.read::<8>(0x14000 + offset / 4096 * 8));
        for offset in (0..RAM_SIZE).step_by(4096) {
            let pte = leaf(offset);
            if resident::executable_offset(offset) {
                assert_eq!(pte, (IPA + offset as u64) | 3 | 1 << 10 | 3 << 8 | 1 << 7);
            } else if (resident::BOOT_END..0x10000).contains(&offset) {
                assert_eq!(pte, 0);
            } else if pte != 0 && offset != CODE_OFFSET {
                assert_eq!(pte & (3 << 53), 3 << 53);
            }
        }
    }

    #[test]
    fn ffi_layout_matches_sdk() {
        assert_eq!(size_of::<HvExit>(), 32);
        assert_eq!(std::mem::offset_of!(HvExit, exception), 8);
    }
}
