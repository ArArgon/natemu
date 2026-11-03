//! The integration entry: boot a Linux bzImage by the 64-bit boot protocol ([`crate::boot`]) on a VM of 1 to
//! `cache::MAX_VCPUS` vCPUs with the platform devices, and run it until one vCPU's run ends. No fixture, no hook.
//!
//! ```text
//! let outcome = natemu::linux::Boot { cmdline: "console=ttyS0".into(), ..Boot::new(runtime, fexc, kernel) }.run()?;
//! ```
//! [`Boot::prepare`] loads the kernel, its initrd and the ACPI tables into guest RAM and builds the vCPUs' state and the
//! devices: COM1, a 16550A at ports 3F8H-3FFH whose output goes to `serial` as it is sent (L3); the I/O APIC and the 8259
//! pair (L4a); the PIT, the RTC and the ACPI PM block (L4b). Every other port and device-range page is unassigned
//! (all-ones reads, ignored writes, counted; with `strict_unassigned` a typed stop). The ACPI tables describe `vcpus`
//! processors and start with the RSDP at `rsdp`; vCPU 0 is the BSP, each other one starts as an AP in the firmware's
//! waiting state on the tables' wakeup mailbox (X4). [`Prepared::run`] runs the VM: the run ends when any vCPU's run ends
//! (the BSP's `stop_at`, a stop, power off or reset, its watchdog), and the others are cancelled.
use crate::boot::{self, Inputs, Layout, Loaded};
use crate::execution::{self, Backing, Compiler, Stop};
use crate::vm::{Machine, Vcpu, hv, require_vcpus};
use crate::{Result, clock, compile_cache, devices, owners, uart};
use natemu_abi::execution::{self as exec, State};
use natemu_abi::guest_memory::GuestRam;
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering::SeqCst};
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// What to boot and how.
pub struct Boot {
    /// The resident (the built `natemu-runtime` ELF) and the fexc binary.
    pub runtime: PathBuf,
    pub fexc: PathBuf,
    /// The bzImage, its initrd and the command line.
    pub kernel: Vec<u8>,
    pub initrd: Option<Vec<u8>>,
    pub cmdline: String,
    /// Guest RAM, also the boot map's low RAM end (M1; default 2 GiB).
    pub ram: u64,
    /// vCPUs (default 1).
    pub vcpus: usize,
    /// The RSDP's guest-physical address (default the ACPI NVS range's start).
    pub rsdp: u64,
    /// Each vCPU's code cache (default the largest, `CODE_BYTES_MAX` less its head).
    pub code_bytes: usize,
    /// Where COM1's output goes as it is sent (default nowhere; it is also kept, [`Prepared::serial_bytes`]).
    pub serial: Option<Box<dyn Write + Send>>,
    /// An unassigned port or page access is a typed stop instead of all-ones (L3; default off).
    pub strict_unassigned: bool,
    /// The periodic sources' lost-tick policies (L5; default `LostTicks::DEFAULT`).
    pub lost_ticks: clock::LostTicks,
    /// A vCPU in the guest this long without a host exit is forced out (default 60 s).
    pub watchdog: Duration,
    /// Block size limit in guest instructions (default fexc's own).
    pub max_block: Option<u32>,
    /// End the run when the BSP is about to execute this linear address (a block boundary, nothing fetched there).
    pub stop_at: Option<u64>,
    /// The persistent compile cache (default off; [`compile_cache::Config::from_env`] opts in).
    pub compile_cache: compile_cache::Config,
}

impl Boot {
    pub fn new(runtime: impl Into<PathBuf>, fexc: impl Into<PathBuf>, kernel: Vec<u8>) -> Self {
        Self {
            runtime: runtime.into(),
            fexc: fexc.into(),
            kernel,
            initrd: None,
            cmdline: String::new(),
            ram: Layout::DEFAULT.low_end,
            vcpus: 1,
            rsdp: boot::ACPI_NVS.0,
            code_bytes: exec::cache::CODE_BYTES_MAX - exec::cache::HEAD_BYTES,
            serial: None,
            strict_unassigned: false,
            lost_ticks: clock::LostTicks::DEFAULT,
            watchdog: Duration::from_secs(60),
            max_block: None,
            stop_at: None,
            compile_cache: compile_cache::Config::OFF,
        }
    }

    /// Loads everything into a new backing and builds the devices; nothing runs yet.
    pub fn prepare(self) -> Result<Prepared> {
        let layout = Layout { low_end: self.ram, high: 0 };
        layout.e820().map_err(|e| e.to_string())?; // before allocating the RAM
        let backing = execution::prepare(&self.runtime.to_string_lossy(), self.code_bytes, self.vcpus, layout.low_end as usize)?;
        require_vcpus(self.vcpus)?;
        let tables = boot::acpi::build(self.vcpus, self.rsdp);
        let inputs = Inputs { image: &self.kernel, cmdline: &self.cmdline, initrd: self.initrd.as_deref(), acpi: Some(&tables) };
        let loaded = boot::load(&backing.ram, &layout, &inputs).map_err(|e| e.to_string())?;
        execution::guest_ram_written(&backing.memory);
        let lost = self.lost_ticks;
        for v in 0..self.vcpus {
            execution::set_cpu(backing.vcpu(v), if v == 0 { loaded.cpu } else { loaded.ap });
            execution::set_lapic(backing.vcpu(v), natemu_abi::lapic::Lapic { reinject: u64::from(lost.lapic), ..natemu_abi::lapic::RESET });
            if v > 0 {
                let mut state: State = backing.vcpu(v).read_record(exec::STATE_OFFSET);
                state.mailbox = tables.mailbox;
                backing.vcpu(v).write_record(exec::STATE_OFFSET, state);
            }
        }
        // L3: COM1 and the unassigned rule.
        let uart = Arc::new(Mutex::new(uart::Uart::new()));
        uart.lock().unwrap().sink = self.serial;
        let mut devices = devices::Devices::new(layout.low_end);
        devices.register_ports(boot::acpi::platform::COM1_PORT.into(), 8, uart.clone())?;
        // L4a: the I/O APIC, the 8259 pair and their wires.
        let irqs = crate::irq::install(&mut devices, &backing.memory, self.vcpus, &uart)?;
        // L4b: the PIT and port 61H, the RTC, the ACPI PM block and reset register.
        let board = clock::install(&mut devices, &irqs, lost)?;
        devices.strict = self.strict_unassigned;
        Ok(Prepared {
            backing,
            loaded,
            uart,
            devices: Arc::new(devices),
            irqs,
            board,
            fexc: self.fexc,
            max_block: self.max_block,
            stop_at: self.stop_at,
            watchdog: self.watchdog,
            compile_cache: self.compile_cache,
            ran: AtomicBool::new(false),
        })
    }

    /// [`Boot::prepare`], then [`Prepared::run`].
    pub fn run(self) -> Result<Outcome> {
        self.prepare()?.run()
    }
}

/// A loaded VM, ready to run.
pub struct Prepared {
    backing: Backing,
    loaded: Loaded,
    uart: Arc<Mutex<uart::Uart>>,
    devices: Arc<devices::Devices>,
    irqs: crate::irq::Irqs,
    board: clock::Board,
    fexc: PathBuf,
    max_block: Option<u32>,
    stop_at: Option<u64>,
    watchdog: Duration,
    compile_cache: compile_cache::Config,
    /// [`Prepared::run`] has started: the backing is used.
    ran: AtomicBool,
}

/// How a run ended.
pub struct Outcome {
    /// Each vCPU's stop and the compiles it asked for (vCPU 0, the BSP, always has one; `None`: the vCPU never ran).
    pub each: Vec<Option<(Stop, u64)>>,
    /// The vCPU whose end ended the run (not cancelled); `usize::MAX` when none did.
    pub ended_first: usize,
}
impl Outcome {
    /// The BSP's stop.
    pub fn stop(&self) -> &Stop {
        &self.each[0].as_ref().unwrap().0
    }
}

impl Prepared {
    /// Creates the VM, runs every vCPU on its own thread until one's run ends, and destroys the VM. Once: a second call
    /// is an error (the backing holds the first run's state); the views below stay valid after it.
    pub fn run(&self) -> Result<Outcome> {
        if self.ran.swap(true, SeqCst) {
            return Err("this boot already ran: prepare a new one".into());
        }
        let (backing, vcpus) = (&self.backing, self.backing.n);
        let machine = Machine::create(&backing.memory)?;
        self.irqs.attach(machine.doorbell());
        let stops = execution::Stops::new(backing);
        let prepare = |v: usize| -> Result<Vcpu> {
            let cpu = Vcpu::create()?;
            execution::enter(&cpu, backing.vcpu(v), true)?;
            machine.route(v)?;
            Ok(cpu)
        };
        let ended_first = AtomicUsize::new(usize::MAX);
        let body = |v: usize, cpu: &Vcpu, cancelled: &AtomicBool| -> Result<(Stop, u64)> {
            let mut compiler = Compiler::new(&self.fexc, cpu, self.max_block)?;
            compiler.disk = compile_cache::shared(&self.fexc, &self.compile_cache)?; // P3: the persistent compile cache
            let mut requests = execution::Requests { devices: Some(self.devices.clone()), ..Default::default() };
            let stop_at = if v == 0 { self.stop_at } else { None };
            let stop = execution::run(cpu, backing.vcpu(v), &mut compiler, stop_at, self.watchdog, &mut requests, &stops, cancelled, &mut ());
            stops.end(v);
            // X4: one vCPU's end ends the machine's run; the first to end (not cancelled) is reported as the run's cause.
            if !matches!(stop, Ok(Stop::Cancelled)) {
                let _ = ended_first.compare_exchange(usize::MAX, v, SeqCst, SeqCst);
            }
            owners::cancel_owners(cancelled);
            Ok((stop?, compiler.compiles))
        };
        let supervision = owners::supervise(vcpus, &prepare, &body);
        let destroyed = machine.destroy();
        let supervision = supervision?;
        hv(destroyed, "destroy VM")?;
        if let Some(e) = supervision.error {
            return Err(e);
        }
        if supervision.outputs.first().is_none_or(Option::is_none) {
            return Err("vCPU without a result".into());
        }
        Ok(Outcome { each: supervision.outputs, ended_first: ended_first.load(SeqCst) })
    }
    pub fn guest(&self) -> &GuestRam {
        &self.backing.ram
    }
    pub fn loaded(&self) -> &Loaded {
        &self.loaded
    }
    pub fn backing(&self) -> &Backing {
        &self.backing
    }
    pub fn board(&self) -> &clock::Board {
        &self.board
    }
    pub fn devices(&self) -> &devices::Devices {
        &self.devices
    }
    /// The bytes COM1 has sent so far.
    pub fn serial_bytes(&self) -> usize {
        self.uart.lock().unwrap_or_else(|e| e.into_inner()).output.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use natemu_abi::lapic::{RESET, SVR_ENABLE};

    /// Writes 64 KiB of zeros below the caller's frame: whatever a returned function left on the stack is gone.
    #[inline(never)]
    fn clobber_stack() {
        std::hint::black_box([0u8; 64 << 10]);
    }

    /// R2's blocker: the chipset must post into the guest memory of the `Prepared` it ends up in, after `prepare`
    /// returned and the value moved (it once kept a pointer to `prepare`'s local backing). No VM: COM1's THRE interrupt
    /// (IER bit 1) raises ISA IRQ 4, which the I/O APIC routes to vCPU 0 as vector 0x41.
    #[test]
    fn interrupts_reach_the_backing_after_prepare_moves_it() {
        let resident = std::env::temp_dir().join(format!("natemu-linux-test-{}.elf", std::process::id()));
        std::fs::write(&resident, crate::memory::tests::resident_elf(&[(crate::PRIVATE, 4)])).unwrap();
        let mut boot = Boot::new(&resident, "fexc", boot::tests::image(0x1000));
        (boot.ram, boot.code_bytes) = (64 << 20, exec::cache::MAX_BLOCK_BYTES);
        let prepared = boot.prepare();
        std::fs::remove_file(&resident).unwrap();
        let prepared = Box::new(prepared.unwrap());
        clobber_stack();
        execution::set_lapic(prepared.backing().vcpu(0), natemu_abi::lapic::Lapic { svr: SVR_ENABLE | 0xff, ..RESET });
        let (ioapic, _) = prepared.devices().find(boot::acpi::platform::IOAPIC_BASE.into()).unwrap();
        for (select, value) in [(0x18, 0x41), (0x19, 0)] {
            let mut d = ioapic.lock().unwrap();
            d.write(0, 0, 4, select);
            d.write(0, 0x10, 4, value);
        }
        assert!(!execution::pending(&prepared.backing().memory, 0, 0x41));
        prepared.devices().port(0, boot::acpi::platform::COM1_PORT + 1, 1, Some(2)).unwrap();
        assert!(execution::pending(&prepared.backing().memory, 0, 0x41));
    }
}
