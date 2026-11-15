//! The one startup probe (CPU-features plan section 2.2): reads the vCPU's ID registers (the ones admission reads, plus
//! ID_AA64MMFR2_EL1 for LSE2) and Apple's TSO bit, checks every `Required` host item at once, and records the exactness
//! facts (AFP). It fails with one message listing everything missing (`natemu: host lacks lse2 pmull`), and answers CSV3
//! and SB from the vCPU's registers (never sysctl: the resident runs SB in the vCPU's view). [`startup`] runs when a vCPU's
//! compiler is set up (after the resident's setup wrote ACTLR_EL1), and every vCPU repeats the check (cheap, identical).
//! The startup line (the guest feature map and a host note) is printed by the Linux boot path only ([`announce`]), once per
//! process: the program driver's stderr stays as it was. The interrupt-controller API is a deployment fact (its functions
//! are strong imports: macOS 15 or later, stated in the README), not probed.
use crate::admission::{
    Class, FEATURES, ID_REGISTERS, MACHINE, detected, supported_features,
};
use crate::vm::{ACTLR_EL1, Vcpu, config_id_register};
use std::sync::OnceLock;

/// `hv_feature_reg_t` of each [`ID_REGISTERS`] entry (ISAR0, ISAR1, PFR0, MMFR0, MMFR1, MMFR2), for the config view.
const CONFIG_REGISTERS: [u32; ID_REGISTERS.len()] = [2, 3, 7, 4, 5, 6];

/// What the host's vCPU shows: the ID registers and whether Apple's TSO bit (ACTLR_EL1 bit 1) reads back set.
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) struct Probe {
    pub id: [u64; ID_REGISTERS.len()],
    pub tso: bool,
}
impl Probe {
    /// Reads a created vCPU, after the resident's setup wrote ACTLR_EL1 (`vm.rs enter_resident_with_asid`).
    pub(crate) fn read(cpu: &Vcpu) -> Result<Self, String> {
        let mut id = [0; ID_REGISTERS.len()];
        for (value, (name, reg)) in id.iter_mut().zip(ID_REGISTERS) {
            *value = cpu.sys(reg).map_err(|e| format!("{name}: {e}"))?;
        }
        Ok(Self {
            id,
            tso: cpu.sys(ACTLR_EL1)? & 2 != 0,
        })
    }
    /// Every `Required` item the host lacks (fexc features by fexc's name, then the machine's rows).
    pub(crate) fn missing(&self) -> Vec<&'static str> {
        self.lacking(Class::Required)
    }
    /// Every `Exactness` feature the host lacks (AFP).
    pub(crate) fn inexact(&self) -> Vec<&'static str> {
        self.lacking(Class::Exactness)
    }
    fn lacking(&self, class: Class) -> Vec<&'static str> {
        let features = FEATURES.iter().map(|f| (f.name, &f.detect, f.class));
        let machine = MACHINE.iter().map(|&(n, ref d, c)| (n, d, c));
        (features.chain(machine))
            .filter(|&(_, d, c)| c == class && !detected(d, &self.id, self.tso))
            .map(|(n, ..)| n)
            .collect()
    }
    /// The refusal, listing everything missing at once (`host lacks lse2 pmull128`; the CLI adds its `natemu: ` prefix);
    /// `None` when the host has all.
    pub(crate) fn refusal(&self) -> Option<String> {
        let missing = self.missing();
        (!missing.is_empty()).then(|| format!("host lacks {}", missing.join(" ")))
    }
    /// The one terse startup line: the guest feature map (x86-64-v3 plus the advertised extensions, from the catalogue's
    /// generated names), then `; host -afp` when the host lacks an exactness feature. The guest CPU is the same on every
    /// supported host (every built feature is core), so only the host note varies.
    pub(crate) fn line(&self) -> String {
        let mut line = String::from("natemu: cpu x86-64-v3");
        for name in natemu_abi::cpu_gates::ADVERTISED_BEYOND_V3 {
            line.push_str(" +");
            line.push_str(name);
        }
        let inexact = self.inexact();
        if !inexact.is_empty() {
            let host: Vec<_> = inexact.iter().map(|n| format!("-{n}")).collect();
            line.push_str("; host ");
            line.push_str(&host.join(" "));
        }
        line
    }
    /// The fexc feature bits this vCPU can execute.
    pub(crate) fn supported(&self) -> u64 {
        supported_features(&self.id)
    }
}

/// The startup probe for a created vCPU: refuses with one message, prints the startup line once per process, and
/// returns the probe. Called at the end of `execution::enter` (after the resident's setup wrote ACTLR_EL1), which is in
/// the owners' setup error path: one setup error reaches the caller, and the CLI prints it once as `natemu: host lacks ...`.
pub(crate) fn startup(cpu: &Vcpu) -> Result<Probe, String> {
    startup_with(Probe::read(cpu)?)
}
/// [`startup`] on a probe already read: the bare refusal string.
fn startup_with(probe: Probe) -> Result<Probe, String> {
    match probe.refusal() {
        Some(refusal) => Err(refusal),
        None => Ok(probe),
    }
}
/// Prints the startup line to stderr, once per process (it carries its own `natemu: ` prefix). Called by the Linux boot
/// path after a vCPU's setup passed [`startup`]; the program driver never calls it.
pub(crate) fn announce(cpu: &Vcpu) {
    static PRINTED: OnceLock<()> = OnceLock::new();
    if let Ok(probe) = Probe::read(cpu) {
        PRINTED.get_or_init(|| eprintln!("{}", probe.line()));
    }
}

/// The hypervisor's configuration view of the ID registers (no VM or vCPU needed; they agree on every register natemu
/// reads), for the answers needed before any vCPU exists: CSV3 and SB. Cached, the error too: a hypervisor failure is
/// reported as such, never as an absent feature.
fn config_view() -> Result<&'static [u64; ID_REGISTERS.len()], String> {
    static VIEW: OnceLock<Result<[u64; ID_REGISTERS.len()], String>> = OnceLock::new();
    VIEW.get_or_init(|| {
        let mut id = [0; ID_REGISTERS.len()];
        for ((value, reg), (name, _)) in id.iter_mut().zip(CONFIG_REGISTERS).zip(ID_REGISTERS) {
            *value = config_id_register(reg).map_err(|e| format!("{name}: {e}"))?;
        }
        Ok(id)
    })
    .as_ref()
    .map_err(Clone::clone)
}
fn machine(name: &str) -> Result<bool, String> {
    let row = MACHINE.iter().find(|r| r.0 == name).expect("machine row");
    Ok(detected(&row.1, config_view()?, true))
}
/// FEAT_CSV3 (ID_AA64PFR0_EL1[63:60]): the host's loads do not pass faulting data on (the only condition for RDCL_NO
/// besides `--speculation-barriers`). An error when the hypervisor cannot be read.
pub(crate) fn csv3() -> Result<bool, String> {
    machine("csv3")
}
/// FEAT_SB (ID_AA64ISAR1_EL1[39:36]): the page-walk barriers' leaf instruction.
pub(crate) fn sb() -> Result<bool, String> {
    machine("sb")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::admission::index;

    /// ID registers of a host with everything required (bits set only for what natemu reads).
    const FULL: [u64; ID_REGISTERS.len()] = [
        2 << 20 | 2 << 52 | 1 << 16 | 2 << 4 | 1 << 8 | 1 << 12 | 1 << 44, // LSE, FlagM2, CRC32, PMULL, SHA1/2, DotProd
        1 << 16 | 1 << 32 | 1 << 36 | 1 << 20,                              // FCMA, FRINTTS, SB, LRCPC
        1 << 60,                                                            // CSV3
        0,
        1 << 44,      // AFP
        1 << 32,      // LSE2
    ];
    fn probe(f: impl FnOnce(&mut Probe)) -> Probe {
        let mut p = Probe { id: FULL, tso: true };
        f(&mut p);
        p
    }

    #[test]
    fn a_full_host_passes() {
        let p = probe(|_| {});
        assert_eq!(p.refusal(), None);
        assert_eq!(
            p.line(),
            "natemu: cpu x86-64-v3 +adx +aes +clflushopt +clwb +fsgsbase +pclmulqdq +rdrand +rdtscp +sha +smep +umip +xsavec +xsaveopt"
        );
        assert_ne!(p.supported() & 1 << index("flagm"), 0);
    }

    #[test]
    fn one_refusal_lists_everything_missing() {
        // FlagM, LSE and LSE2 missing; the order is FEATURES' then the machine rows'.
        let p = probe(|p| {
            p.id[0] &= !(15 << 52) & !(15 << 20);
            p.id[5] = 0;
        });
        assert_eq!(
            p.refusal().as_deref(),
            Some("host lacks atomics_lse flagm lse2")
        );
        // Each alone, and the TSO bit and PMULL (AES field 1: AES yes, PMULL no).
        assert_eq!(probe(|p| p.id[5] = 0).missing(), ["lse2"]);
        assert_eq!(probe(|p| p.tso = false).missing(), ["apple_tso"]);
        assert_eq!(
            probe(|p| p.id[0] = FULL[0] & !(15 << 4) | 1 << 4).missing(),
            ["pmull128"]
        );
        assert_eq!(
            probe(|p| p.id[0] = 0).refusal().as_deref(),
            Some("host lacks atomics_lse flagm crc32 aes sha pmull128")
        );
    }

    #[test]
    fn the_startup_refusal_is_the_bare_string() {
        // One line `natemu: host lacks ...` reaches the user: the CLI (and the driver's main) add the prefix once.
        let bad = probe(|p| p.id[5] = 0);
        assert_eq!(startup_with(bad).unwrap_err(), "host lacks lse2");
        assert!(startup_with(probe(|_| {})).is_ok());
    }

    #[test]
    fn fp_and_advsimd_are_probed() {
        // PFR0.FP [19:16] and AdvSIMD [23:20] read 0xF when absent (0 and 1 are implemented).
        assert_eq!(probe(|p| p.id[2] |= 0xf << 16).missing(), ["fp"]);
        assert_eq!(probe(|p| p.id[2] |= 0xf << 20).missing(), ["advsimd"]);
        assert_eq!(probe(|p| p.id[2] |= 1 << 16 | 1 << 20).missing(), Vec::<&str>::new());
        assert_eq!(
            probe(|p| p.id[2] |= 0xff << 16).refusal().as_deref(),
            Some("host lacks fp advsimd")
        );
    }

    #[test]
    fn a_host_without_afp_runs_with_a_note() {
        let p = probe(|p| p.id[4] = 0);
        assert_eq!(p.refusal(), None);
        assert!(p.line().starts_with("natemu: cpu x86-64-v3 +adx "));
        assert!(p.line().ends_with(" +xsaveopt; host -afp"), "{}", p.line());
        assert_eq!(p.line().matches('\n').count(), 0); // one line
        assert_eq!(p.supported() & 1 << index("afp"), 0);
    }

    /// The startup probe reads the vCPU's registers; so does [`config_view`], and CSV3 and SB answered from it equal sysctl's
    /// (plan chunk 2 gate). Needs the real host and no entitlement: `cargo test -p natemu probe -- --ignored`.
    #[test]
    #[ignore = "real host: compares the hypervisor's registers with sysctl"]
    fn csv3_and_sb_from_registers_equal_sysctl() {
        let sysctl = |n: &str| crate::execution::host_feature(n);
        assert_eq!(csv3(), Ok(sysctl("FEAT_CSV3")));
        assert_eq!(sb(), Ok(sysctl("FEAT_SB")));
    }
}
