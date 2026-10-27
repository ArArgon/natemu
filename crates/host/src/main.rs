use memory::*;
use natemu_abi::*;
use std::mem::{align_of, size_of};
use std::{env, ffi::c_void, fs, ptr};
use vm::*;

mod admission;
mod concurrent;
mod drivers {
    pub(crate) mod program;
}
mod execution;
mod json;
#[cfg(test)]
#[path = "../../runtime/src/coordinator.rs"]
mod coordinator_tests;
mod i4v;
mod i4v_controls;
mod i4v_off;
mod i4v_off_steps;
mod i4v_stack;
mod memory;
mod owners;
mod recovery;
mod translation;
mod vm;

type Result<T> = std::result::Result<T, String>;

unsafe extern "C" {
    fn getentropy(buffer: *mut c_void, length: usize) -> i32;
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
    if (a.len() == 6 || a.len() == 7) && a[2] == "run-program" {
        return drivers::program::run(&a);
    }
    if a.len() == 9 && a[2] == "i4v-containment" {
        return concurrent::containment::run(&a);
    }
    if a.len() == 9 && a[2] == "i4v-progress" {
        return concurrent::run_progress(&a);
    }
    if a.len() == 9 && a[2] == "i4v-coherence" {
        return concurrent::run_coherence(&a);
    }
    if a.len() == 8 && a[2] == "i4v-fence" {
        return concurrent::run_fence(&a);
    }
    if a.len() == 10 && a[2] == "i4v-ordering" {
        return concurrent::run_ordering(&a);
    }
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
    load_resident(&memory, &a[1])?;
    single_owner_space(&memory, false);
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
        schedule: 0,
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
    memory.write_record(SHARED_OFFSET, shared);
    let vm = Vm::new(memory)?;
    let isar0 = vm.cpu.sys(ID_AA64ISAR0_EL1)?;
    let tcr = SINGLE_ROOT_TCR;
    vm.cpu.enter_resident(0, tcr)?;
    vm.run(0x49)?;
    let result = vm.shared();
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
