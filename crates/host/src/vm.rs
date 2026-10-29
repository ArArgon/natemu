//! Hypervisor.framework VM lifecycle and vCPU state.
//!
//! The VM is process-global: `Machine` creates it with its in-kernel GIC (macOS 15+), maps guest RAM and destroys it.
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
    fn os_release(object: *mut c_void);
    fn hv_gic_config_create() -> *mut c_void;
    fn hv_gic_config_set_distributor_base(config: *mut c_void, ipa: u64) -> i32;
    fn hv_gic_config_set_redistributor_base(config: *mut c_void, ipa: u64) -> i32;
    fn hv_gic_create(config: *mut c_void) -> i32;
    fn hv_gic_get_spi_interrupt_range(base: *mut u32, count: *mut u32) -> i32;
    fn hv_gic_set_spi(intid: u32, level: bool) -> i32;
    fn hv_gic_get_distributor_reg(reg: u16, value: *mut u64) -> i32;
    fn hv_gic_set_distributor_reg(reg: u16, value: u64) -> i32;
    fn hv_gic_set_redistributor_reg(vcpu: u64, reg: u32, value: u64) -> i32;
    fn hv_vm_destroy() -> i32;
    fn hv_vm_map(address: *const c_void, ipa: u64, size: usize, flags: u64) -> i32;
    fn hv_vcpu_create(vcpu: *mut u64, exit: *mut *const HvExit, config: *const c_void) -> i32;
    fn hv_vcpu_destroy(vcpu: u64) -> i32;
    fn hv_vcpu_set_reg(vcpu: u64, reg: u32, value: u64) -> i32;
    fn hv_vcpu_get_reg(vcpu: u64, reg: u32, value: *mut u64) -> i32;
    fn hv_vcpu_set_sys_reg(vcpu: u64, reg: u16, value: u64) -> i32;
    fn hv_vcpu_get_sys_reg(vcpu: u64, reg: u16, value: *mut u64) -> i32;
    fn hv_vcpu_run(vcpu: u64) -> i32;
    fn hv_vcpu_set_vtimer_offset(vcpu: u64, offset: u64) -> i32;
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

/// Serializes HVF's GIC calls with vCPU creation and destruction (from any thread): `hv_gic_set_spi` and
/// `hv_vcpu_create` can deadlock each other when they run at once (seen at 4 vCPUs), and a ring must not race a destroy.
// ponytail: one process lock around every GIC call and vCPU create/destroy; per-vCPU locks if kick rates make it contend.
static GIC: std::sync::Mutex<()> = std::sync::Mutex::new(());
fn gic() -> std::sync::MutexGuard<'static, ()> {
    GIC.lock().unwrap_or_else(|e| e.into_inner())
}

/// Forces the listed vCPUs (owned by other threads) out of `hv_vcpu_run`.
pub(crate) fn exit_vcpus(ids: &mut [u64]) -> i32 {
    unsafe { hv_vcpus_exit(ids.as_mut_ptr(), ids.len() as u32) }
}

/// Guest memory rights: read/write/execute.
const MAPPED: u64 = 7;
/// IPAs of the GIC's distributor (64 KiB) and redistributor region (128 KiB per vCPU, 32 MiB), below guest memory.
const GIC_DISTRIBUTOR: u64 = 0x0800_0000;
const GIC_REDISTRIBUTORS: u64 = 0x0900_0000;
const _: () = assert!(GIC_REDISTRIBUTORS + (32 << 20) <= IPA);
/// Distributor registers (GICv3): control, then the per-INTID arrays (`n` = the register holding INTID `n`).
const GICD_CTLR: u16 = 0;
fn gicd(array: u16, n: u32, bits: u32) -> u16 {
    array + (n / (32 / bits) * 4) as u16
}
const GICD_IGROUPR: u16 = 0x80;
const GICD_ISENABLER: u16 = 0x100;
const GICD_ICENABLER: u16 = 0x180;
const GICD_ISPENDR: u16 = 0x200;
const GICD_IPRIORITYR: u16 = 0x400;
const GICD_ICFGR: u16 = 0xc00;
const GICD_IROUTER: u16 = 0x6000;
/// Redistributor SGI-frame registers of the SGIs and PPIs (`hv_gic_redistributor_reg_t`).
const GICR_IGROUPR0: u32 = 0x1_0080;
const GICR_ISENABLER0: u32 = 0x1_0100;
const MPIDR_EL1: u16 = 0xc005;

/// The process VM with all of `memory` mapped read/write/execute at `IPA`, and its GIC: each vCPU's doorbells are the
/// SGI `exec::KICK_SGI` (from peers' residents) and its own SPI `spi + v` (from the host), group 1, edge-triggered.
pub(crate) struct Machine {
    /// The first SPI: vCPU v's host doorbell is `spi + v`.
    spi: u32,
}
impl Machine {
    pub(crate) fn create(memory: &Memory) -> Result<Self> {
        memory.flush_instruction_cache();
        unsafe {
            let config = hv_vm_config_create();
            let created = hv(hv_vm_create(config), "hv_vm_create");
            os_release(config);
            created?;
            // After the VM, before any vCPU (hv_gic.h).
            let gic = hv_gic_config_create();
            let created = hv(hv_gic_config_set_distributor_base(gic, GIC_DISTRIBUTOR), "GIC distributor base")
                .and_then(|()| hv(hv_gic_config_set_redistributor_base(gic, GIC_REDISTRIBUTORS), "GIC redistributor base"))
                .and_then(|()| hv(hv_gic_create(gic), "hv_gic_create (macOS 15 or later)"));
            os_release(gic);
            created?;
        }
        let (mut spi, mut count) = (0, 0);
        unsafe { hv(hv_gic_get_spi_interrupt_range(&mut spi, &mut count), "GIC SPI range")? };
        if (count as usize) < natemu_abi::execution::cache::MAX_VCPUS {
            return Err(format!("{count} GIC SPIs: one per vCPU needed"));
        }
        let machine = Self { spi };
        // Affinity routing, both groups enabled.
        let ctlr = machine.distributor(GICD_CTLR)?;
        machine.set_distributor(GICD_CTLR, ctlr | 0x13)?;
        // Every vCPU's SPI (one register write each: these registers hold several INTIDs, so all are set at once).
        let spis = spi..spi + natemu_abi::execution::cache::MAX_VCPUS as u32;
        let mut words: std::collections::BTreeMap<u16, u64> = Default::default();
        for n in spis.clone() {
            *words.entry(gicd(GICD_IGROUPR, n, 1)).or_default() |= 1 << (n % 32);
            *words.entry(gicd(GICD_ICFGR, n, 2)).or_default() |= 2 << (2 * (n % 16)); // edge
            *words.entry(gicd(GICD_IPRIORITYR, n, 8)).or_default() |= 0x80 << (8 * (n % 4));
        }
        for (reg, value) in words {
            machine.set_distributor(reg, value)?;
        }
        for n in spis {
            machine.set_distributor(gicd(GICD_ISENABLER, n, 1), 1 << (n % 32))?;
        }
        unsafe { hv(hv_vm_map(memory.0.cast(), IPA, memory.1, MAPPED), "hv_vm_map")? };
        Ok(machine)
    }
    fn distributor(&self, reg: u16) -> Result<u64> {
        let mut value = 0;
        let _gic = gic();
        unsafe { hv(hv_gic_get_distributor_reg(reg, &mut value), "read GIC distributor")? };
        Ok(value)
    }
    fn set_distributor(&self, reg: u16, value: u64) -> Result<()> {
        let _gic = gic();
        unsafe { hv(hv_gic_set_distributor_reg(reg, value), "write GIC distributor") }
    }
    /// Disables vCPU `v`'s host doorbell (a test driver's peer that never answers).
    pub(crate) fn silence(&self, v: usize) -> Result<()> {
        let n = self.spi + v as u32;
        self.set_distributor(gicd(GICD_ICENABLER, n, 1), 1 << (n % 32))
    }
    /// Routes vCPU `v`'s SPI to it (Aff0 = `v`). Written after the vCPU exists: HVF delivers an SPI whose route was
    /// written before then to no one (`experiments/track03/native-interrupts/`, the IROUTER quirk).
    pub(crate) fn route(&self, v: usize) -> Result<()> {
        self.set_distributor(GICD_IROUTER + 8 * (self.spi + v as u32) as u16, v as u64)
    }
    /// Rings vCPU `v`'s host doorbell (an edge on its SPI), from any thread.
    pub(crate) fn ring(&self, v: usize) -> Result<()> {
        self.doorbell().ring(v)
    }
    /// The doorbells alone (X2: what a vCPU's service loop rings for a test driver's event source).
    pub(crate) fn doorbell(&self) -> Doorbell {
        Doorbell { spi: self.spi }
    }
    /// vCPU `v`'s host doorbell is pending (rung, not yet acknowledged).
    pub(crate) fn ringing(&self, v: usize) -> Result<bool> {
        let n = self.spi + v as u32;
        Ok(self.distributor(gicd(GICD_ISPENDR, n, 1))? >> (n % 32) & 1 != 0)
    }
    /// Destroys the VM now and returns the framework status.
    pub(crate) fn destroy(self) -> i32 {
        std::mem::forget(self);
        unsafe { hv_vm_destroy() }
    }
}
/// The VM's host doorbells ([`Machine::doorbell`]); valid while the machine exists.
#[derive(Clone, Copy, Debug)]
pub(crate) struct Doorbell {
    spi: u32,
}
impl Doorbell {
    /// Rings vCPU `v`'s host doorbell (an edge on its SPI), from any thread.
    pub(crate) fn ring(&self, v: usize) -> Result<()> {
        let _gic = gic();
        unsafe { hv(hv_gic_set_spi(self.spi + v as u32, true), "hv_gic_set_spi") }
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
pub(crate) const CPSR: u32 = 34;
pub(crate) const ID_AA64ISAR0_EL1: u16 = 0xc030;
const SCTLR_EL1: u16 = 0xc080;
const ACTLR_EL1: u16 = 0xc081;
const CPACR_EL1: u16 = 0xc082;
const CNTV_CTL_EL0: u16 = 0xdf19;
const MDSCR_EL1: u16 = 0x8012;
const CNTKCTL_EL1: u16 = 0xc708;
/// MDSCR_EL1.TDCC: EL0 accesses to the debug communication channel (MDCCSR_EL0, DBGDTR*_EL0) trap to EL1. Clear, HVF
/// traps them to the host instead, and the resident never sees them.
const MDSCR_TDCC: u64 = 1 << 12;

/// Why `hv_vcpu_run` returned.
pub(crate) enum Exit {
    /// An HVC with this immediate (PC is after it).
    Hvc(u64),
    /// The host forced the exit ([`exit_vcpus`]).
    Cancelled,
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
            let _gic = gic();
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
    /// Makes this vCPU vCPU `v` of the GIC (MPIDR_EL1 Aff0 = `v`) and enables its peer doorbell, SGI `KICK_SGI`, in
    /// group 1 (on the owning thread, before it first runs). Its virtual count is the host's (CNTVCT =
    /// `mach_absolute_time`), so the resident's stop times compare with the host's kick times.
    /// `enabled` false leaves the SGI disabled (a test driver's peer that never answers).
    pub(crate) fn doorbell(&self, v: usize, enabled: bool) -> Result<()> {
        // GICR_IGROUPR0 holds the group of every SGI and PPI: this write sets the kick SGI to group 1 and leaves the
        // rest in group 0. X2 (the LAPIC timer on the virtual timer, PPI 27) must add bit 27 here, or the timer's IRQ is
        // group 0 and never reaches the resident (only group 1 is enabled, ICC_IGRPEN1_EL1).
        let bit = 1 << natemu_abi::execution::KICK_SGI;
        self.set_sys(MPIDR_EL1, v as u64)?;
        let _gic = gic();
        unsafe {
            hv(hv_vcpu_set_vtimer_offset(self.id, 0), "hv_vcpu_set_vtimer_offset")?;
            hv(hv_gic_set_redistributor_reg(self.id, GICR_IGROUPR0, bit), "GICR_IGROUPR0")?;
            if enabled {
                hv(hv_gic_set_redistributor_reg(self.id, GICR_ISENABLER0, bit), "GICR_ISENABLER0")?;
            }
            Ok(())
        }
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
    /// off, TSO on (ACTLR_EL1 bit 1), FP/SIMD untrapped, virtual timer off. Generated code runs at EL0: every EL0 access
    /// the resident does not grant traps to EL1 (SCTLR_EL1 has UCI, UCT, DZE, UMA, nTWI, nTWE clear; MDSCR_EL1.TDCC set
    /// for the debug channel; CNTKCTL_EL1 = 0 for the counters and timers), never to the host.
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
        self.set_sys(MDSCR_EL1, MDSCR_TDCC)?;
        self.set_sys(CNTKCTL_EL1, 0)?;
        self.set_sys(CNTV_CTL_EL0, 0)
    }
    /// Runs until the next exit: an HVC or a forced exit ([`exit_vcpus`]); any other exit is an error reporting the
    /// exit and PC.
    pub(crate) fn run_until_exit(&self) -> Result<Exit> {
        const CANCELED: u32 = 0;
        unsafe {
            hv(hv_vcpu_run(self.id), "hv_vcpu_run")?;
            let exit = &*self.exit;
            let (syndrome, ipa) = (exit.exception.syndrome, exit.exception.physical_address);
            match (exit.reason, syndrome >> 26) {
                (CANCELED, _) => Ok(Exit::Cancelled),
                (1, 0x16) => Ok(Exit::Hvc(syndrome & 0xffff)),
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
        let result = {
            let _gic = gic();
            unsafe { hv_vcpu_destroy(self.id) }
        };
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
