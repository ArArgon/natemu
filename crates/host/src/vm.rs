//! Hypervisor.framework VM lifecycle and vCPU state.
//!
//! The VM is process-global: `Machine` creates it, maps guest RAM and destroys it.
//! A `Vcpu` belongs to the thread that created it; register access, runs and its
//! destruction happen on that thread. Destroy every vCPU before the VM, and keep
//! the mapped `Memory` alive until the VM is gone.
use super::*;

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

pub(crate) fn hv(code: i32, operation: &str) -> Result<()> {
    if code == 0 {
        Ok(())
    } else {
        Err(format!("{operation}: {:#x}", code as u32))
    }
}

/// A fresh nonzero identity for one VM incarnation.
pub(crate) fn new_vm_nonce() -> Result<u64> {
    let mut nonce = 0u64;
    if unsafe { getentropy((&raw mut nonce).cast(), 8) } != 0 || nonce == 0 {
        return Err("VM identity entropy unavailable".into());
    }
    Ok(nonce)
}

/// Fails unless the host can run `n` vCPUs in one VM.
pub(crate) fn require_vcpus(n: usize) -> Result<()> {
    let mut limit = 0;
    unsafe { hv(hv_vm_get_max_vcpu_count(&mut limit), "read host vCPU limit")? };
    if n > limit as usize {
        return Err(format!("{n} vCPUs exceed the host limit of {limit}"));
    }
    Ok(())
}

/// Forces the listed vCPUs (owned by other threads) out of `hv_vcpu_run`.
pub(crate) fn exit_vcpus(ids: &mut [u64]) -> i32 {
    unsafe { hv_vcpus_exit(ids.as_mut_ptr(), ids.len() as u32) }
}

/// The process VM with all of `memory` mapped read/write/execute at `IPA`.
pub(crate) struct Machine(());
impl Machine {
    pub(crate) fn create(memory: &Memory) -> Result<Self> {
        memory.flush_instruction_cache();
        unsafe { hv(hv_vm_create(ptr::null()), "hv_vm_create")? };
        let machine = Self(());
        unsafe { hv(hv_vm_map(memory.0.cast(), IPA, memory.1, 7), "hv_vm_map")? };
        Ok(machine)
    }
    /// Destroys the VM now and returns the framework status.
    pub(crate) fn destroy(self) -> i32 {
        std::mem::forget(self);
        unsafe { hv_vm_destroy() }
    }
}
impl Drop for Machine {
    fn drop(&mut self) {
        unsafe {
            hv_vm_destroy();
        }
    }
}

pub(crate) const PC: u32 = 31;
const CPSR: u32 = 34;
pub(crate) const ID_AA64ISAR0_EL1: u16 = 0xc030;
const SCTLR_EL1: u16 = 0xc080;
const ACTLR_EL1: u16 = 0xc081;
const CPACR_EL1: u16 = 0xc082;
const CNTV_CTL_EL0: u16 = 0xdf19;
/// TCR_EL1 for the single lower private root (TTBR1 walks disabled).
pub(crate) const SINGLE_ROOT_TCR: u64 = 16 | 1 << 8 | 1 << 10 | 3 << 12 | 1 << 23 | 2 << 32;

pub(crate) struct Vcpu {
    id: u64,
    exit: *const HvExit,
    alive: bool,
}
impl Vcpu {
    /// Creates a vCPU on the calling thread. FEAT_FlagM is an emulator requirement:
    /// FEX-generated code uses CFINV.
    pub(crate) fn create() -> Result<Self> {
        let mut cpu = Self {
            id: 0,
            exit: ptr::null(),
            alive: false,
        };
        unsafe {
            hv(
                hv_vcpu_create(&mut cpu.id, &mut cpu.exit, ptr::null()),
                "hv_vcpu_create",
            )?
        };
        cpu.alive = true;
        if (cpu.sys(ID_AA64ISAR0_EL1)? >> 52) & 15 == 0 {
            return Err("FlagM required for FEX-generated code (CFINV)".into());
        }
        Ok(cpu)
    }
    pub(crate) fn id(&self) -> u64 {
        self.id
    }
    pub(crate) fn set_reg(&self, reg: u32, value: u64) -> Result<()> {
        unsafe { hv(hv_vcpu_set_reg(self.id, reg, value), "set register") }
    }
    pub(crate) fn reg(&self, reg: u32) -> Result<u64> {
        let mut value = 0;
        unsafe { hv(hv_vcpu_get_reg(self.id, reg, &mut value), "read register")? };
        Ok(value)
    }
    pub(crate) fn set_sys(&self, reg: u16, value: u64) -> Result<()> {
        unsafe {
            hv(
                hv_vcpu_set_sys_reg(self.id, reg, value),
                "set system register",
            )
        }
    }
    pub(crate) fn sys(&self, reg: u16) -> Result<u64> {
        let mut value = 0;
        unsafe {
            hv(
                hv_vcpu_get_sys_reg(self.id, reg, &mut value),
                "read system register",
            )?
        };
        Ok(value)
    }
    /// Sets the architectural EL1 state the resident `_start` consumes for the
    /// owner window at `window`: PC at the window, EL1h with DAIF masked, x0 = the
    /// window's lower private root, x1 = TCR_EL1, x2 = MAIR_EL1; MMU off, TSO on
    /// (ACTLR_EL1 bit 1), FP/SIMD untrapped, virtual timer off.
    pub(crate) fn enter_resident(&self, window: usize, tcr: u64) -> Result<()> {
        let base = IPA + window as u64;
        self.set_reg(PC, base)?;
        self.set_reg(CPSR, 0x3c5)?;
        self.set_reg(0, base + shadow::LOWER_ROOT as u64)?;
        self.set_reg(1, tcr)?;
        self.set_reg(2, 0xff)?;
        self.set_sys(SCTLR_EL1, 0x30d00980)?;
        self.set_sys(ACTLR_EL1, 2)?;
        self.set_sys(CPACR_EL1, 3 << 20)?;
        self.set_sys(CNTV_CTL_EL0, 0)
    }
    /// Runs until the next exit and returns its HVC immediate; any other exit is
    /// an error reporting the exit and PC.
    pub(crate) fn run_to_hvc(&self) -> Result<u64> {
        unsafe {
            hv(hv_vcpu_run(self.id), "hv_vcpu_run")?;
            let exit = &*self.exit;
            if exit.reason != 1 || exit.exception.syndrome >> 26 != 0x16 {
                return Err(format!(
                    "unexpected exit: reason={} syndrome={:#x} PC={:#x} VA={:#x} IPA={:#x}",
                    exit.reason,
                    exit.exception.syndrome,
                    self.reg(PC)?,
                    exit.exception.virtual_address,
                    exit.exception.physical_address
                ));
            }
            Ok(exit.exception.syndrome & 0xffff)
        }
    }
    /// Destroys the vCPU now and returns the framework status.
    pub(crate) fn destroy(&mut self) -> i32 {
        let result = unsafe { hv_vcpu_destroy(self.id) };
        if result == 0 {
            self.alive = false;
        }
        result
    }
}
impl Drop for Vcpu {
    fn drop(&mut self) {
        if self.alive {
            self.destroy();
        }
    }
}

/// A one-vCPU machine over window 0. Fields drop in teardown order: the vCPU,
/// then the VM, then the backing.
pub(crate) struct Vm {
    pub(crate) cpu: Vcpu,
    _machine: Machine,
    pub(crate) memory: Memory,
}
impl Vm {
    pub(crate) fn new(memory: Memory) -> Result<Self> {
        let machine = Machine::create(&memory)?;
        Ok(Self {
            cpu: Vcpu::create()?,
            _machine: machine,
            memory,
        })
    }
    pub(crate) fn shared(&self) -> Shared {
        self.memory.read_record(SHARED_OFFSET)
    }
    pub(crate) fn run(&self, expected: u64) -> Result<()> {
        let event = self.cpu.run_to_hvc()?;
        if event != expected {
            return Err(format!(
                "unexpected HVC immediate {event:#x}, expected {expected:#x}"
            ));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ffi_layout_matches_sdk() {
        assert_eq!(size_of::<HvExit>(), 32);
        assert_eq!(std::mem::offset_of!(HvExit, exception), 8);
    }
}
