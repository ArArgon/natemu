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
    fn hv_vm_config_create() -> *mut c_void;
    fn hv_vm_config_get_ipa_granule(config: *const c_void, granule: *mut u32) -> i32;
    fn hv_vm_protect(ipa: u64, size: usize, flags: u64) -> i32;
    fn os_release(object: *mut c_void);
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

/// Guest memory rights of [`protect`]: as mapped (read/write/execute), or read-write (not executable).
pub(crate) const MAPPED: u64 = 7;
pub(crate) const READ_WRITE: u64 = 3;
/// Sets the stage-2 rights of `[ipa, ipa + size)` (granule-aligned) for every vCPU, running ones included.
pub(crate) fn protect(ipa: u64, size: usize, rights: u64) -> Result<()> {
    unsafe { hv(hv_vm_protect(ipa, size, rights), "hv_vm_protect") }
}

/// The process VM with all of `memory` mapped read/write/execute at `IPA`.
pub(crate) struct Machine {
    /// Bytes of the stage-2 (IPA) granule: the unit of [`protect`].
    pub(crate) granule: usize,
}
impl Machine {
    pub(crate) fn create(memory: &Memory) -> Result<Self> {
        memory.flush_instruction_cache();
        let mut granule = u32::MAX;
        unsafe {
            // The default configuration; its IPA granule is the VM's.
            let config = hv_vm_config_create();
            let read = hv(hv_vm_config_get_ipa_granule(config, &mut granule), "hv_vm_config_get_ipa_granule");
            let created = read.and_then(|()| hv(hv_vm_create(config), "hv_vm_create"));
            os_release(config);
            created?;
        }
        // HV_IPA_GRANULE_4KB, HV_IPA_GRANULE_16KB.
        let machine = Self { granule: if granule == 0 { 4096 } else { 16384 } };
        if granule > 1 {
            return Err(format!("unknown IPA granule {granule}"));
        }
        unsafe { hv(hv_vm_map(memory.0.cast(), IPA, memory.1, MAPPED), "hv_vm_map")? };
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

pub(crate) const X30: u32 = 30;
pub(crate) const PC: u32 = 31;
pub(crate) const CPSR: u32 = 34;
pub(crate) const SPSR_EL1: u16 = 0xc200;
pub(crate) const ELR_EL1: u16 = 0xc201;
pub(crate) const ID_AA64ISAR0_EL1: u16 = 0xc030;
const SCTLR_EL1: u16 = 0xc080;
const ACTLR_EL1: u16 = 0xc081;
const CPACR_EL1: u16 = 0xc082;
const CNTV_CTL_EL0: u16 = 0xdf19;

/// Why `hv_vcpu_run` returned.
pub(crate) enum Exit {
    /// An HVC with this immediate (PC is after it).
    Hvc(u64),
    /// The host forced the exit ([`exit_vcpus`]).
    Cancelled,
    /// A stage-2 data or instruction abort (EC 0x24, 0x20) with its syndrome and IPA; PC is the faulting instruction.
    Stage2 { syndrome: u64, ipa: u64 },
}

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
    /// window's lower private root tagged `asid` (TTBR0_EL1[63:48]; 8-bit ASIDs), x1 = TCR_EL1, x2 = MAIR_EL1; MMU
    /// off, TSO on (ACTLR_EL1 bit 1), FP/SIMD untrapped, virtual timer off.
    pub(crate) fn enter_resident_with_asid(&self, window: usize, tcr: u64, asid: u8) -> Result<()> {
        let base = IPA + window as u64;
        self.set_reg(PC, base)?;
        self.set_reg(CPSR, 0x3c5)?;
        self.set_reg(0, base + shadow::LOWER_ROOT as u64 | u64::from(asid) << 48)?;
        self.set_reg(1, tcr)?;
        self.set_reg(2, 0xff)?;
        self.set_sys(SCTLR_EL1, 0x30d00980)?;
        self.set_sys(ACTLR_EL1, 2)?;
        self.set_sys(CPACR_EL1, 3 << 20)?;
        self.set_sys(CNTV_CTL_EL0, 0)
    }
    /// Runs until the next exit: an HVC, a forced exit ([`exit_vcpus`]) or a stage-2 abort (EC 0x24, 0x20); any
    /// other exit is an error reporting the exit and PC.
    pub(crate) fn run_until_exit(&self) -> Result<Exit> {
        const CANCELED: u32 = 0;
        unsafe {
            hv(hv_vcpu_run(self.id), "hv_vcpu_run")?;
            let exit = &*self.exit;
            let (syndrome, ipa) = (exit.exception.syndrome, exit.exception.physical_address);
            match (exit.reason, syndrome >> 26) {
                (CANCELED, _) => Ok(Exit::Cancelled),
                (1, 0x16) => Ok(Exit::Hvc(syndrome & 0xffff)),
                (1, 0x24 | 0x20) => Ok(Exit::Stage2 { syndrome, ipa }),
                (reason, _) => Err(self.unexpected(reason, syndrome, ipa)),
            }
        }
    }
    fn unexpected(&self, reason: u32, syndrome: u64, ipa: u64) -> String {
        let va = unsafe { (*self.exit).exception.virtual_address };
        format!("unexpected exit: reason={reason} syndrome={syndrome:#x} PC={:#x} VA={va:#x} IPA={ipa:#x}", self.reg(PC).unwrap_or(0))
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

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ffi_layout_matches_sdk() {
        assert_eq!(size_of::<HvExit>(), 32);
        assert_eq!(std::mem::offset_of!(HvExit, exception), 8);
    }
}
