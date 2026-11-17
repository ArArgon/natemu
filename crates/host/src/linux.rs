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
use std::io::{Read, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering::SeqCst};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

/// The disk's request queues over PCI: one per vCPU, at most `virtio::MAX_QUEUES`; `NATEMU_BLK_QUEUES` overrides it
/// (calibration and comparison; 1 is the single queue of the MMIO transport). Measured on an M2 Max (November 14,
/// 2026, 2 vCPUs, against one queue): depth-1 4 KiB direct reads 75-77 -> 111 MB/s for 74 -> 46 us of host CPU per
/// request (a request no longer wakes a second vCPU), four fio readers at depth 16 +37%; but one fio job at depth 16
/// now submits and completes on one vCPU: mixed 4 KiB 54-56k -> 33-37k IOPS, sequential 128 KiB writes about -17%.
fn disk_queues(vcpus: usize) -> usize {
    let max = crate::virtio::MAX_QUEUES;
    let asked = std::env::var("NATEMU_BLK_QUEUES")
        .ok()
        .and_then(|v| v.parse::<usize>().ok());
    asked.unwrap_or(vcpus).clamp(1, max)
}

/// How a virtio device is attached to the guest.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Transport {
    #[default]
    Mmio,
    Pci,
}
/// The transport of each virtio device (naming one that is not enabled does nothing).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Transports {
    pub disk: Transport,
    pub net: Transport,
    pub rng: Transport,
}
impl Transports {
    pub const fn all(t: Transport) -> Self {
        Self {
            disk: t,
            net: t,
            rng: t,
        }
    }
}
/// PCI slots of the first disk, the network device and the entropy device (00:01.0, 00:02.0, 00:03.0); more disks follow.
const BLOCK_SLOT: u8 = 1;
const NET_SLOT: u8 = 2;
const RNG_SLOT: u8 = 3;

/// One disk of the guest: the list order is the order the guest names them (`/dev/vda`, `vdb`, ...).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DiskSpec {
    pub id: String,
    /// Owned writable raw disk; excludes other writers/truncators for this VM's lifetime.
    pub path: PathBuf,
    pub transport: Transport,
}

/// The most disks: each takes an interrupt line, 18 + its index (disk 0 is on 16), and the controller has lines 16-23.
/// ponytail: PCI is capped the same way until PCI functions can share a line (design step 6).
const MAX_DISKS: usize = 6;

/// Which devices a launch has and on what transport. The input of [`place`].
struct Wanted {
    rng: Option<Transport>,
    disks: Vec<Transport>,
    net: Option<Transport>,
}
#[cfg(test)]
impl Wanted {
    /// The old one-of-each form: `enabled` is [rng, disk, net].
    fn old(enabled: [bool; 3], t: Transports) -> Self {
        let [rng, disk, net] = enabled;
        Self {
            rng: rng.then_some(t.rng),
            disks: if disk { vec![t.disk] } else { vec![] },
            net: net.then_some(t.net),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    Rng,
    Disk(usize),
    Net,
}
/// Where one device lives: both the ACPI tables and the device wiring in `prepare` read this, so they cannot disagree.
#[derive(Clone, Copy, Debug)]
struct Placed {
    kind: Kind,
    pci: bool,
    /// PCI slot (used when `pci`).
    slot: u8,
    /// MMIO page, ACPI uid and interrupt line (the line is also the PCI function's INTx line).
    res: boot::acpi::VirtioMmio,
}

/// The placement rule (design section 3). First disk, NIC, entropy: PCI slots 1, 2, 3, pages 0xF000_0000/1000/2000, lines
/// 16/17/18, uids 0/1/2. Disk k >= 1: slot 3 + k, page 0xF000_0000 + (2 + k) * 4 KiB, line 18 + k, uid 2 + k.
/// Returned in wiring order: entropy, disk 0, network, then disks 1 and up.
fn place(w: &Wanted) -> Result<Vec<Placed>> {
    use crate::virtio::{BLOCK_RESOURCE, NET_RESOURCE, RNG_RESOURCE};
    if w.disks.len() > MAX_DISKS {
        return Err(if w.disks.iter().all(|&t| t == Transport::Pci) {
            format!("at most {MAX_DISKS} disks until shared PCI interrupt lines are built")
        } else {
            format!("at most {MAX_DISKS} disks on mmio (interrupt lines 16-23); use pci for more")
        });
    }
    let pci = |t: Transport| t == Transport::Pci;
    let one = |kind, t: Option<Transport>, slot, res| {
        t.map(|t| Placed {
            kind,
            pci: pci(t),
            slot,
            res,
        })
    };
    let disk = |k: usize| {
        let res = if k == 0 {
            BLOCK_RESOURCE
        } else {
            let n = 2 + k as u32;
            boot::acpi::VirtioMmio {
                uid: n as u8,
                base: 0xf000_0000 + n * 4096,
                gsi: 16 + n,
            }
        };
        let slot = if k == 0 { BLOCK_SLOT } else { 3 + k as u8 };
        Placed {
            kind: Kind::Disk(k),
            pci: pci(w.disks[k]),
            slot,
            res,
        }
    };
    let mut out: Vec<_> = one(Kind::Rng, w.rng, RNG_SLOT, RNG_RESOURCE)
        .into_iter()
        .collect();
    out.extend((!w.disks.is_empty()).then(|| disk(0)));
    out.extend(one(Kind::Net, w.net, NET_SLOT, NET_RESOURCE));
    out.extend((1..w.disks.len()).map(disk));
    if let Some(p) = out.iter().find(|p| p.pci && p.slot > 31) {
        return Err(format!("PCI slots 1-31 are all used (slot {})", p.slot));
    }
    Ok(out)
}

/// What the placed devices give the guest: the memory map and the ACPI tables, with the lists the tables were built
/// from. Pure (no host state), so a launch can be pinned by a test.
struct LaunchPlan {
    /// ACPI lists each device once: virtio-MMIO devices here, PCI functions below as (slot, GSI).
    #[allow(dead_code)] // read by the pinning test
    manifest: Vec<boot::acpi::VirtioMmio>,
    #[allow(dead_code)]
    functions: Vec<(u8, u32)>,
    tables: boot::acpi::Tables,
    layout: Layout,
}

/// The memory map for `ram` bytes; `Err` when it is not a valid one.
fn layout_of(placed: &[Placed], ram: u64) -> Result<Layout> {
    let layout = Layout {
        low_end: ram.min(exec::LOW_RAM_END),
        high: ram.saturating_sub(exec::LOW_RAM_END),
        pci: placed.iter().any(|p| p.pci),
    };
    layout.e820().map_err(|e| e.to_string())?;
    Ok(layout)
}
#[cfg(test)]
fn launch_layout(enabled: [bool; 3], t: Transports, ram: u64) -> Result<Layout> {
    layout_of(&place(&Wanted::old(enabled, t))?, ram)
}

/// The ACPI tables and the lists they are built from (panics on a vCPU count outside 1..=254, as `build_with_devices`).
fn plan_tables(vcpus: usize, rsdp: u64, placed: &[Placed], ram: u64) -> Result<LaunchPlan> {
    let layout = layout_of(placed, ram)?;
    let (mut manifest, mut functions) = (Vec::new(), Vec::new());
    for p in placed {
        if p.pci {
            functions.push((p.slot, p.res.gsi));
        } else {
            manifest.push(p.res);
        }
    }
    functions.sort_unstable();
    let tables = boot::acpi::build_with_devices(vcpus, rsdp, &manifest, &functions);
    Ok(LaunchPlan {
        manifest,
        functions,
        tables,
        layout,
    })
}
#[cfg(test)]
fn launch_plan(
    vcpus: usize,
    rsdp: u64,
    enabled: [bool; 3],
    t: Transports,
    ram: u64,
) -> Result<LaunchPlan> {
    plan_tables(vcpus, rsdp, &place(&Wanted::old(enabled, t))?, ram)
}

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
    /// Where COM1's output goes as it is sent (default none: the UART keeps it; [`Prepared::serial_bytes`] counts it either
    /// way, [`Prepared::serial_error`] reports a failed write).
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
    /// The persistent compile cache (default off; [`compile_cache::Config::new`] opts in).
    pub compile_cache: compile_cache::Config,
    /// Debug: every answer used at another RIP than its own is compiled there too and must equal it (`Compiler::check_reuse`).
    pub check_reuse: bool,
    /// Page-currency plan chunk 1: `Compiler::page_currency` (default on; `--page-currency` / `NATEMU_PAGE_CURRENCY`).
    pub page_currency: u64,
    /// Fault-around plan chunk 1: `Compiler::writable_on_read` (default on; `--writable-on-read` / `NATEMU_WRITABLE_ON_READ`).
    pub writable_on_read: bool,
    /// Debug: admission's code-shape check on every object (`Compiler::shape`; the test driver's `boot-linux --code-shape`).
    pub code_shape: bool,
    /// PCID plan P1 (owner, November 5): turn on the resident's page-walk speculation barriers. Off by default; on, the
    /// host must have FEAT_SB (checked in [`Boot::prepare`]) and IA32_ARCH_CAPABILITIES advertises RDCL_NO when the host
    /// also has FEAT_CSV3, so a default command line then boots with PTI off. `--speculation-barriers` / `NATEMU_SPECULATION_BARRIERS`.
    pub speculation_barriers: bool,
    /// AFP accurate plan chunk 1: accurate floating-point mode (`Compiler::accurate_fp`; `--fp accurate` / `NATEMU_FP`). Default fast.
    pub accurate_fp: bool,
    /// Expose the modern virtio entropy device (native entropy, copied completions).
    pub rng: bool,
    /// The disks, in the order the guest sees them (each with its own transport; `transports.disk` is not read here).
    pub disks: Vec<DiskSpec>,
    /// Expose the virtio network device on user-mode NAT ([`crate::virtio::Slirp`]).
    pub net: bool,
    /// Static localhost forwards and explicit host-loopback access; requires `net`.
    pub net_options: crate::virtio::SlirpOptions,
    /// The shared template pool (cross-vCPU reuse plan): back a read-only span of about 274 MiB of address space before guest
    /// RAM (pages are backed as touched), map it into every vCPU, publish admitted templates into it and let each resident look
    /// in it before asking the host. On by default (the gate passed, November 14: configure -3.3%, make -1.8% wall);
    /// `--pool=false` / `NATEMU_POOL=0` turn it off.
    pub pool: bool,
    /// Per device, virtio-MMIO (default) or virtio-PCI.
    pub transports: Transports,
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
            check_reuse: false,
            page_currency: exec::cache::PAGES_ON,
            writable_on_read: true,
            code_shape: false,
            speculation_barriers: false,
            accurate_fp: false,
            rng: false,
            disks: Vec::new(),
            net: false,
            net_options: crate::virtio::SlirpOptions::default(),
            pool: true,
            transports: Transports::all(Transport::Mmio),
        }
    }

    /// Loads everything into a new backing and builds the devices; nothing runs yet.
    pub fn prepare(self) -> Result<Prepared> {
        if !self.net && (self.net_options.host_loopback || !self.net_options.forwards.is_empty()) {
            return Err("network: forwarding and host-loopback options require --net".into());
        }
        self.net_options.validate()?;
        // PCID plan P1: the barriers' leaf SB needs FEAT_SB (the vCPU's ID_AA64ISAR1_EL1); refuse to turn them on without it
        // (default off needs nothing).
        if self.speculation_barriers && !crate::probe::sb()? {
            return Err(
                "--speculation-barriers: this host lacks FEAT_SB (ID_AA64ISAR1_EL1.SB)".into(),
            );
        }
        let t = self.transports;
        let placed = place(&Wanted {
            rng: self.rng.then_some(t.rng),
            disks: self.disks.iter().map(|d| d.transport).collect(),
            net: self.net.then_some(t.net),
        })?;
        layout_of(&placed, self.ram)?; // before require_vcpus and before allocating the RAM
        require_vcpus(self.vcpus)?;
        let backing = execution::prepare_with_pool(
            &self.runtime.to_string_lossy(),
            self.code_bytes,
            self.vcpus,
            self.ram as usize,
            self.pool,
        )?;
        let plan = plan_tables(self.vcpus, self.rsdp, &placed, self.ram)?;
        let (layout, tables) = (plan.layout, plan.tables);
        let inputs = Inputs {
            image: &self.kernel,
            cmdline: &self.cmdline,
            initrd: self.initrd.as_deref(),
            acpi: Some(&tables),
        };
        let loaded = boot::load(&backing.ram, &layout, &inputs).map_err(|e| e.to_string())?;
        execution::guest_ram_written(&backing.memory);
        let lost = self.lost_ticks;
        for v in 0..self.vcpus {
            execution::set_cpu(
                backing.vcpu(v),
                if v == 0 { loaded.cpu } else { loaded.ap },
                self.speculation_barriers,
            )?;
            execution::set_lapic(
                backing.vcpu(v),
                natemu_abi::lapic::Lapic {
                    reinject: u64::from(lost.lapic),
                    ..natemu_abi::lapic::RESET
                },
            );
            if v > 0 {
                let mut state: State = backing.vcpu(v).read_record(exec::STATE_OFFSET);
                state.mailbox = tables.mailbox;
                backing.vcpu(v).write_record(exec::STATE_OFFSET, state);
            }
        }
        // L3: COM1 and the unassigned rule.
        let uart = Arc::new(Mutex::new(uart::Uart::new()));
        uart.lock().unwrap().sink = self.serial;
        let mut devices = devices::Devices::new(self.ram);
        // L4a: the I/O APIC, the 8259 pair and their wires.
        let irqs = crate::irq::install(&mut devices, &backing.memory, self.vcpus, &uart)?;
        // L4b: the PIT and port 61H, the RTC, the ACPI PM block and reset register.
        let board = clock::install(&mut devices, &irqs, lost, &uart)?;
        let mut virtio = Vec::new();
        let mut bus = crate::pci::Bus::new();
        let mut net = None;
        for p in &placed {
            let gsi = p.res.gsi as usize;
            let v = match p.kind {
                Kind::Rng => crate::virtio::Virtio::rng(irqs.clone(), gsi),
                Kind::Disk(k) => {
                    let disk =
                        crate::virtio::Virtio::block(irqs.clone(), gsi, &self.disks[k].path)?;
                    if p.pci {
                        // One request queue per vCPU: the guest then takes each completion on the vCPU that sent the request.
                        disk.block_queues(disk_queues(self.vcpus))?;
                    }
                    disk
                }
                Kind::Net => {
                    let (v, port) = crate::virtio::Virtio::network(
                        irqs.clone(),
                        gsi,
                        crate::virtio::Slirp::MAC,
                        crate::virtio::Slirp::OFFLOADS,
                    )?;
                    net = Some(crate::virtio::Slirp::new(port, &self.net_options)?);
                    v
                }
            };
            if p.pci {
                bus.add(p.slot, v.pci(p.slot))?;
            } else {
                devices.register(p.res.base.into(), 4096, v.device())?;
            }
            virtio.push(v);
        }
        if layout.pci {
            bus.install(&mut devices)?;
        }
        devices.strict = self.strict_unassigned;
        Ok(Prepared {
            backing,
            loaded,
            uart,
            devices: devices.freeze(),
            irqs,
            board,
            virtio,
            net: Mutex::new(net),
            fexc: self.fexc,
            max_block: self.max_block,
            stop_at: self.stop_at,
            watchdog: self.watchdog,
            launch: crate::probe::Launch {
                speculation_barriers: self.speculation_barriers,
                accurate_fp: self.accurate_fp,
                pool: self.pool,
                compile_cache: self
                    .compile_cache
                    .dir
                    .clone()
                    .map(|d| (d, self.compile_cache.cap)),
            },
            compile_cache: self.compile_cache,
            check_reuse: self.check_reuse,
            page_currency: self.page_currency,
            writable_on_read: self.writable_on_read,
            code_shape: self.code_shape,
            accurate_fp: self.accurate_fp,
            ran: AtomicBool::new(false),
            cancel: Arc::default(),
            serial_input: Mutex::new(None),
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
    virtio: Vec<Arc<crate::virtio::Virtio>>,
    /// The network backend, taken by the run.
    net: Mutex<Option<crate::virtio::Slirp>>,
    fexc: PathBuf,
    max_block: Option<u32>,
    stop_at: Option<u64>,
    watchdog: Duration,
    compile_cache: compile_cache::Config,
    /// The options the startup lines report.
    launch: crate::probe::Launch,
    check_reuse: bool,
    page_currency: u64,
    writable_on_read: bool,
    code_shape: bool,
    accurate_fp: bool,
    /// [`Prepared::run`] has started: the backing is used.
    ran: AtomicBool,
    /// The run's cancellation flag ([`Prepared::canceller`]).
    cancel: Arc<AtomicBool>,
    /// [`Prepared::serial_input`], taken by the run.
    serial_input: Mutex<Option<Box<dyn Read + Send>>>,
}

/// Stops a [`Prepared::run`] from any thread (an integrator's stop button, the stub's SIGINT): every vCPU stops at its next
/// host exit (the supervisor forces running ones out) and the run returns with each stop `Cancelled`; a run not yet
/// started ends at once.
#[derive(Clone)]
pub struct Canceller(Arc<AtomicBool>);
impl Canceller {
    pub fn cancel(&self) {
        owners::cancel_owners(&self.0);
    }
    /// SIGINT and SIGTERM cancel the run from now on (a second one exits the process at once with 130): the handler only
    /// sets a flag, a detached thread acts on it (`cancel` in the performance log). For a process's command-line entry
    /// (the `natemu` stub, the test driver's `boot-linux`); once per process.
    pub fn on_signals(self) {
        static INTERRUPTED: AtomicBool = AtomicBool::new(false);
        extern "C" fn interrupted(_: i32) {
            unsafe extern "C" {
                fn _exit(status: i32) -> !;
            }
            if INTERRUPTED.swap(true, SeqCst) {
                restore_terminal();
                unsafe { _exit(130) };
            }
        }
        unsafe extern "C" {
            fn signal(signal: i32, handler: extern "C" fn(i32)) -> usize;
        }
        const SIGINT: i32 = 2;
        const SIGTERM: i32 = 15;
        unsafe {
            signal(SIGINT, interrupted);
            signal(SIGTERM, interrupted);
        }
        std::thread::spawn(move || {
            while !INTERRUPTED.load(SeqCst) {
                std::thread::sleep(Duration::from_millis(5));
            }
            crate::perf::mark("cancel");
            self.cancel();
        });
    }
}

/// The terminal settings [`serial_stdin`] replaced.
static TERMINAL: OnceLock<Termios> = OnceLock::new();
/// Darwin's `struct termios` (`tcflag_t` and `speed_t` are `unsigned long`, `NCCS` 20).
#[repr(C)]
#[derive(Clone, Copy)]
struct Termios {
    iflag: u64,
    oflag: u64,
    cflag: u64,
    lflag: u64,
    cc: [u8; 20],
    ispeed: u64,
    ospeed: u64,
}
unsafe extern "C" {
    fn isatty(fd: i32) -> i32;
    fn tcgetattr(fd: i32, t: *mut Termios) -> i32;
    fn tcsetattr(fd: i32, action: i32, t: *const Termios) -> i32;
    fn cfmakeraw(t: *mut Termios);
    fn atexit(f: extern "C" fn()) -> i32;
}
/// Puts back the terminal settings [`serial_stdin`] replaced (async-signal-safe: `tcsetattr` only).
extern "C" fn restore_terminal() {
    if let Some(t) = TERMINAL.get() {
        unsafe { tcsetattr(0, 0, t) }; // TCSANOW
    }
}
/// The process's standard input as COM1's input ([`Prepared::serial_input`]). On a terminal, raw mode first, as QEMU's
/// `-serial stdio` (`char-stdio.c`: output processing kept, no echo, no line editing, no signal keys: Ctrl-C goes to the
/// guest), restored at exit, at a panic and at the second SIGINT/SIGTERM's exit ([`Canceller::on_signals`]: call this
/// after it, so no signal can end the process with the terminal raw). Once per process.
pub fn serial_stdin() -> Box<dyn Read + Send> {
    let mut t = std::mem::MaybeUninit::<Termios>::uninit();
    if unsafe { isatty(0) == 1 && tcgetattr(0, t.as_mut_ptr()) == 0 } {
        let saved = unsafe { t.assume_init() };
        if TERMINAL.set(saved).is_ok() {
            unsafe { atexit(restore_terminal) };
            let panic = std::panic::take_hook();
            std::panic::set_hook(Box::new(move |info| {
                restore_terminal();
                panic(info)
            }));
            let mut raw = saved;
            unsafe { cfmakeraw(&mut raw) };
            raw.oflag |= 1; // OPOST
            unsafe { tcsetattr(0, 0, &raw) };
        }
    }
    Box::new(std::io::stdin())
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
        crate::perf::mark("run");
        let (backing, vcpus) = (&self.backing, self.backing.n);
        let machine = Machine::create(backing.memory.clone())?;
        self.irqs.attach(machine.doorbell());
        // COM1's host input: a detached thread (it blocks in its read), holding a busy guard while it may still feed (the
        // rule `irq.rs` sets for asynchronous sources; no Linux boot reads it today).
        if let Some(input) = self
            .serial_input
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take()
        {
            let (uart, busy, cancel) = (self.uart.clone(), self.irqs.busy(), self.canceller());
            std::thread::spawn(move || {
                uart::feed(&uart, input, || cancel.cancel());
                drop(busy);
            });
        }
        let stops = execution::Stops::new(backing);
        let prepare = |v: usize| -> Result<Vcpu> {
            let cpu = Vcpu::create()?;
            execution::enter(&cpu, backing.vcpu(v), true)?;
            crate::probe::announce(&cpu, &self.launch);
            machine.route(v)?;
            Ok(cpu)
        };
        let ended_first = AtomicUsize::new(usize::MAX);
        // Perf chunk 13: one map of fexc's answers for every vCPU.
        let answers = std::sync::OnceLock::new();
        let body = |v: usize, cpu: &Vcpu, cancelled: &AtomicBool| -> Result<(Stop, u64)> {
            // Perf chunk 14: the vCPU thread (`hv_vcpu_run` runs on it) at the user-interactive QoS class, so macOS keeps
            // it on a performance core (default class: 93-99% there at N = 4, `locks` N = 4 4% slower). The fexc servers
            // keep the default class: a vCPU waits for its server's reply, so a lower class only delays the vCPU.
            unsafe extern "C" {
                fn pthread_set_qos_class_self_np(class: u32, priority: i32) -> i32;
            }
            const QOS_CLASS_USER_INTERACTIVE: u32 = 0x21;
            unsafe { pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0) };
            let start = crate::perf::now();
            let mut compiler = Compiler::new(&self.fexc, cpu, self.max_block)?;
            compiler.disk = compile_cache::shared(&self.fexc, &self.compile_cache)?; // P3: the persistent compile cache
            compiler.perf = crate::perf::Recorder::new(v, start)?;
            compiler.vcpu = v as u8;
            compiler.pool = backing.pool.clone();
            compiler.share(&answers);
            compiler.check_reuse = self.check_reuse;
            compiler.page_currency = self.page_currency;
            compiler.writable_on_read = self.writable_on_read;
            compiler.shape = self.code_shape;
            compiler.accurate_fp = self.accurate_fp;
            let mut requests = execution::Requests {
                devices: Some(self.devices.clone()),
                ..Default::default()
            };
            let stop_at = if v == 0 { self.stop_at } else { None };
            let stop = execution::run(
                cpu,
                backing.vcpu(v),
                &mut compiler,
                stop_at,
                self.watchdog,
                &mut requests,
                &stops,
                cancelled,
                &mut (),
            );
            let end = crate::perf::now();
            // Close device admission before a participant disappears. Pending copies must be
            // canceled rather than waiting for an acknowledgement from the ended vCPU.
            for device in &self.virtio {
                device.stop();
            }
            stops.end(v);
            // X4: one vCPU's end ends the machine's run; the first to end (not cancelled) is reported as the run's cause.
            if !matches!(stop, Ok(Stop::Cancelled)) {
                let _ = ended_first.compare_exchange(usize::MAX, v, SeqCst, SeqCst);
            }
            owners::cancel_owners(cancelled);
            if let Some(log) = compiler.perf.take() {
                let fexc = compiler.finish();
                let area = backing.vcpu(v);
                let s: State = area.read_record(exec::STATE_OFFSET);
                let state = VcpuState {
                    blocks: s.blocks,
                    l2_hits: s.l2_hits,
                    requests: s.requests,
                    retries: s.retries,
                    upgrades: s.upgrades,
                    evictions: s.evictions,
                    linker: s.linker,
                    links: s.links,
                    unlinks: s.unlinks,
                    invalidations: s.invalidations,
                    steps: s.steps,
                    shootdowns: s.shootdowns,
                    systems: s.systems,
                    halts: s.halts,
                    interrupts: s.interrupts,
                    deliveries: s.deliveries,
                    stops: s.stops,
                    shadow_resets: execution::shadow_resets(area),
                    compiles: compiler.compiles,
                    hits: compiler.hits,
                    disk_hits: compiler.disk_hits,
                    relocated: compiler.relocated,
                    relocation_refused: compiler.relocation_refused,
                    relocation_fallbacks: compiler.relocation_fallbacks,
                    reuse_checks: compiler.reuse_checks,
                    hits_by_source: compiler.hits_by_source.to_vec(),
                    duplicates: compiler.duplicates,
                    lock_wait: compiler.lock_wait.into(),
                    recompiled: compiler.recompiled,
                    admissions: crate::execution::why::NAMES
                        .into_iter()
                        .zip(&compiler.admissions)
                        .map(|(k, n)| (k, n.get()))
                        .collect(),
                    pool: execution::K1Pool::new(
                        &area.read_record(exec::cache::LOOKUP_OFFSET + exec::cache::CONTROL),
                    ),
                };
                match log.finish(
                    end,
                    fexc,
                    &area.read_record(exec::perf::OFFSET),
                    &area.read_record(exec::perf::WAKES_OFFSET),
                    &execution::k1_stats(area),
                    &state,
                ) {
                    (0, 0) => {}
                    (failed, dropped) => eprintln!(
                        "natemu: performance log of vCPU {v}: {failed} failed writes so far, {dropped} request lines past the cap"
                    ),
                }
            }
            Ok((stop?, compiler.compiles))
        };
        // Perf chunk 14: with the log on, every 5 ms each vCPU's resident time so far by bucket (the current one counted to
        // now; zero without a `perf` resident): `snap NS` and `perf::BUCKETS` ns values per vCPU.
        let sampling = AtomicBool::new(crate::perf::on());
        let (supervision, io_result) = std::thread::scope(|scope| {
            let io: Vec<_> = self
                .virtio
                .iter()
                .map(|device| {
                    scope.spawn(|| {
                        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                            device.run(backing, &machine, &stops, &self.cancel)
                        }))
                        .unwrap_or_else(|_| Err("virtio owner panicked".into()));
                        if result.is_err() {
                            owners::cancel_owners(&self.cancel);
                        }
                        result
                    })
                })
                .collect();
            // The network backend ends once the device stops or drains.
            let net = self.net.lock().unwrap().take().map(|backend| {
                scope.spawn(|| {
                    let result =
                        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| backend.run()))
                            .unwrap_or_else(|_| Err("network backend panicked".into()));
                    if result.is_err() {
                        owners::cancel_owners(&self.cancel);
                    }
                    result
                })
            });
            if sampling.load(SeqCst) {
                scope.spawn(|| {
                    use exec::perf as rp;
                    while sampling.load(SeqCst) {
                        let t = crate::perf::now();
                        let mut text = format!("snap {t}");
                        for v in 0..vcpus {
                            let p: rp::Perf = backing.vcpu(v).read_record(rp::OFFSET);
                            let mut ticks = p.ticks;
                            let now = crate::clock::now();
                            if p.magic == rp::MAGIC && (p.current as usize) < rp::BUCKETS {
                                ticks[p.current as usize] += now.saturating_sub(p.since);
                            }
                            for x in ticks {
                                text += &format!(" {}", execution::ticks_ns(x));
                            }
                        }
                        crate::perf::line(&text);
                        std::thread::sleep(std::time::Duration::from_millis(5));
                    }
                });
            }
            let supervision = owners::supervise(vcpus, &prepare, &body, &self.cancel);
            sampling.store(false, SeqCst);
            for device in &self.virtio {
                device.stop();
            }
            let mut io_result = Ok(());
            for thread in io.into_iter().chain(net) {
                let result = thread
                    .join()
                    .unwrap_or_else(|_| Err("virtio owner panicked".into()));
                if io_result.is_ok() {
                    io_result = result;
                }
            }
            (supervision, io_result)
        });
        if let Some(a) = answers.get() {
            crate::perf::line(&a.report());
        }
        if let Some(p) = &backing.pool {
            crate::perf::line(&p.report());
        }
        if crate::perf::on() {
            crate::perf::line(&backing.io_window_counts().perf_line());
        }
        self.end();
        let destroyed = machine.destroy();
        crate::perf::mark("destroyed");
        let supervision = supervision?;
        hv(destroyed, "destroy VM")?;
        io_result?;
        // A resident built with its `perf` feature keeps its accounting on every entry whether or not anything reads it:
        // say so when the log is off (read after the run, never on its path).
        if !crate::perf::on()
            && (0..vcpus).any(|v| {
                backing
                    .vcpu(v)
                    .read_record::<exec::perf::Perf>(exec::perf::OFFSET)
                    .magic
                    == exec::perf::MAGIC
            })
        {
            eprintln!(
                "natemu: the resident was built with its `perf` feature but the performance log is off (NATEMU_PERF_LOG): its accounting slows every resident entry"
            );
        }
        // AFP accurate plan chunk 6: `NATEMU_FP_TRAP_COUNTS=1` reports each vCPU's trapped-FP counts on stderr after the run, for `tools/bench/run.py`
        // (`fp_hist`: cause * 8 + class, see `cache::Control`).
        if std::env::var_os("NATEMU_FP_TRAP_COUNTS").is_some() {
            for v in 0..vcpus {
                let c: exec::cache::Control = backing.vcpu(v).read_record(exec::cache::LOOKUP_OFFSET + exec::cache::CONTROL);
                eprintln!("natemu-fp-traps vcpu {v} traps {} steps {} hist {:?}", c.fp_traps, c.fp_steps, c.fp_hist);
            }
        }
        if let Some(e) = supervision.error {
            return Err(e);
        }
        if supervision.outputs.first().is_none_or(Option::is_none) {
            return Err("vCPU without a result".into());
        }
        Ok(Outcome {
            each: supervision.outputs,
            ended_first: ended_first.load(SeqCst),
        })
    }
    /// The run's end, before the VM is destroyed (under the GIC lock, `Machine::destroy`). R2 review: no source may ring
    /// this VM's doorbells once it is gone (they would reach the process's next VM): the chipset detaches them under its
    /// lock, and the PIT/RTC/PM threads end.
    fn end(&self) {
        self.irqs.detach();
        self.board.stop();
    }
    /// Where COM1's input comes from (default none), set before [`Prepared::run`]: a thread feeds it to the UART for the
    /// run ([`uart::feed`]; Ctrl-A x cancels the run). [`serial_stdin`] gives the process's standard input
    /// (`--serial-stdin` / `NATEMU_SERIAL_STDIN`).
    pub fn serial_input(&self, input: Box<dyn Read + Send>) {
        *self.serial_input.lock().unwrap_or_else(|e| e.into_inner()) = Some(input);
    }
    /// A handle that cancels this boot's run ([`Canceller`]).
    pub fn canceller(&self) -> Canceller {
        Canceller(self.cancel.clone())
    }
    /// Guest RAM through the host's alias (the loaded image, a dump).
    ///
    /// # Safety
    /// As [`Backing::ram`]: the value and its copies must not be used once this `Prepared` is dropped.
    pub unsafe fn guest(&self) -> GuestRam {
        self.backing.ram
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
        self.uart.lock().unwrap_or_else(|e| e.into_inner()).sent as usize
    }
    /// The serial sink's write error, if one failed (it is dropped then: COM1's later output goes nowhere).
    pub fn serial_error(&self) -> Option<String> {
        self.uart
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .sink_error
            .clone()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use natemu_abi::lapic::{RESET, SVR_ENABLE};

    #[test]
    fn network_defaults_keep_forwarding_and_host_loopback_off() {
        let boot = Boot::new("missing-runtime", "missing-fexc", vec![]);
        assert!(!boot.net);
        assert!(boot.net_options.forwards.is_empty());
        assert!(!boot.net_options.host_loopback);
    }

    #[test]
    fn udp_policy_is_checked_before_ram_or_vm_preparation() {
        let mut boot = Boot::new("missing-runtime", "missing-fexc", vec![]);
        boot.net = true;
        boot.ram = 0;
        boot.net_options.forwards = vec![crate::virtio::PortForward {
            protocol: crate::virtio::ForwardProtocol::Udp,
            host_port: 15201.try_into().unwrap(),
            guest_port: 5201.try_into().unwrap(),
        }];
        let error = boot.prepare().err().expect("UDP needs explicit opt-in");
        assert!(error.contains("requires --net-host-loopback"), "{error}");
    }

    #[test]
    fn network_options_are_validated_before_ram_or_vm_preparation() {
        use crate::virtio::{ForwardProtocol, PortForward, SlirpOptions};
        let forward = PortForward {
            protocol: ForwardProtocol::Tcp,
            host_port: 2222.try_into().unwrap(),
            guest_port: 22.try_into().unwrap(),
        };
        for options in [
            SlirpOptions {
                host_loopback: true,
                ..Default::default()
            },
            SlirpOptions {
                forwards: vec![forward],
                ..Default::default()
            },
        ] {
            let mut boot = Boot::new("missing-runtime", "missing-fexc", vec![]);
            boot.ram = 0; // A later preparation error must not mask the option error.
            boot.net_options = options;
            let error = boot.prepare().err().expect("options require networking");
            assert!(error.contains("require --net"), "{error}");
        }
        let mut boot = Boot::new("missing-runtime", "missing-fexc", vec![]);
        boot.ram = 0;
        boot.net = true;
        boot.net_options.forwards = vec![
            forward,
            PortForward {
                guest_port: 80.try_into().unwrap(),
                ..forward
            },
        ];
        let error = boot.prepare().err().expect("duplicate listener");
        assert!(
            error.contains("duplicate tcp listener 127.0.0.1:2222"),
            "{error}"
        );
    }

    /// Writes 64 KiB of zeros below the caller's frame: whatever a returned function left on the stack is gone.
    #[inline(never)]
    fn clobber_stack() {
        std::hint::black_box([0u8; 64 << 10]);
    }

    /// R2 review: the end of a run detaches the doorbells and stops the timer threads (a PIT programmed at 1 kHz rises no
    /// more). No VM: the doorbell is never rung (nothing routes IRQ 0 to a LAPIC).
    #[test]
    fn the_end_of_a_run_detaches_and_stops_the_timers() {
        let resident =
            std::env::temp_dir().join(format!("natemu-linux-end-{}.elf", std::process::id()));
        std::fs::write(
            &resident,
            crate::memory::tests::resident_elf(&[(crate::PRIVATE, 4)]),
        )
        .unwrap();
        let mut boot = Boot::new(&resident, "fexc", boot::tests::image(0x1000));
        (boot.ram, boot.code_bytes) = (64 << 20, exec::cache::MAX_BLOCK_BYTES);
        let prepared = boot.prepare();
        std::fs::remove_file(&resident).unwrap();
        let prepared = prepared.unwrap();
        prepared.irqs.attach(crate::vm::Doorbell::none());
        // PIT channel 0, lobyte/hibyte, mode 2, count 1193 (1 kHz).
        for (port, value) in [(0x43, 0x34), (0x40, 0xa9), (0x40, 0x04)] {
            prepared.devices().port(0, port, 1, Some(value)).unwrap();
        }
        let rises = || prepared.board().pit.rises.load(SeqCst);
        std::thread::sleep(Duration::from_millis(30));
        assert!(rises() > 5, "the PIT runs: {}", rises());
        prepared.end();
        assert!(!prepared.irqs.attached());
        let at_end = rises();
        std::thread::sleep(Duration::from_millis(30));
        assert_eq!(rises(), at_end);
    }

    /// An entropy device on PCI installs the bus (the memory window and the ports at 0xCF8) and leaves the MMIO
    /// register page unregistered; on MMIO, as before, there is no window.
    #[test]
    fn a_pci_device_installs_the_bus_and_an_mmio_one_does_not() {
        let resident =
            std::env::temp_dir().join(format!("natemu-linux-pci-{}.elf", std::process::id()));
        std::fs::write(
            &resident,
            crate::memory::tests::resident_elf(&[(crate::PRIVATE, 4)]),
        )
        .unwrap();
        for (transport, pci) in [(Transport::Pci, true), (Transport::Mmio, false)] {
            let mut boot = Boot::new(&resident, "fexc", boot::tests::image(0x1000));
            (boot.ram, boot.code_bytes) = (64 << 20, exec::cache::MAX_BLOCK_BYTES);
            boot.rng = true;
            boot.transports.rng = transport;
            let prepared = boot.prepare().unwrap();
            let found = |gpa: u64| prepared.devices().find(gpa).is_some();
            assert_eq!(found(crate::pci::platform::WINDOW.0.into()), pci);
            assert_eq!(found(crate::virtio::RNG_RESOURCE.base.into()), !pci);
        }
        std::fs::remove_file(&resident).unwrap();
    }

    /// R2's blocker: the chipset must post into the guest memory of the `Prepared` it ends up in, after `prepare`
    /// returned and the value moved (it once kept a pointer to `prepare`'s local backing). No VM: COM1's THRE interrupt
    /// (IER bit 1) raises ISA IRQ 4, which the I/O APIC routes to vCPU 0 as vector 0x41.
    #[test]
    fn interrupts_reach_the_backing_after_prepare_moves_it() {
        let resident =
            std::env::temp_dir().join(format!("natemu-linux-test-{}.elf", std::process::id()));
        std::fs::write(
            &resident,
            crate::memory::tests::resident_elf(&[(crate::PRIVATE, 4)]),
        )
        .unwrap();
        let mut boot = Boot::new(&resident, "fexc", boot::tests::image(0x1000));
        (boot.ram, boot.code_bytes) = (64 << 20, exec::cache::MAX_BLOCK_BYTES);
        let prepared = boot.prepare();
        std::fs::remove_file(&resident).unwrap();
        let prepared = Box::new(prepared.unwrap());
        clobber_stack();
        execution::set_lapic(
            prepared.backing().vcpu(0),
            natemu_abi::lapic::Lapic {
                svr: SVR_ENABLE | 0xff,
                ..RESET
            },
        );
        let (ioapic, _) = prepared
            .devices()
            .find(boot::acpi::platform::IOAPIC_BASE.into())
            .unwrap();
        for (select, value) in [(0x18, 0x41), (0x19, 0)] {
            let mut d = ioapic.lock().unwrap();
            d.write(0, 0, 4, select);
            d.write(0, 0x10, 4, value);
        }
        assert!(!execution::pending(&prepared.backing().memory, 0, 0x41));
        prepared
            .devices()
            .port(0, boot::acpi::platform::COM1_PORT + 1, 1, Some(2))
            .unwrap();
        assert!(execution::pending(&prepared.backing().memory, 0, 0x41));
    }
}

/// A vCPU's resident and compiler counters for its performance-log line (`state`).
#[derive(serde::Serialize)]
struct VcpuState {
    blocks: u64,
    l2_hits: u64,
    requests: u64,
    retries: u64,
    upgrades: u64,
    evictions: u64,
    linker: u64,
    links: u64,
    unlinks: u64,
    invalidations: u64,
    steps: u64,
    shootdowns: u64,
    systems: u64,
    halts: u64,
    interrupts: u64,
    deliveries: u64,
    stops: u64,
    shadow_resets: u64,
    compiles: u64,
    hits: u64,
    disk_hits: u64,
    relocated: u64,
    relocation_refused: u64,
    relocation_fallbacks: u64,
    reuse_checks: u64,
    hits_by_source: Vec<u64>,
    duplicates: u64,
    lock_wait: [u64; 2],
    recompiled: [u64; 2],
    /// Object answers by why they were admitted again or not ([`execution::why`]; their sum is every object answer).
    admissions: crate::report::Members<&'static str, u64>,
    /// The template pool's lookups, copies and refusals (cross-vCPU reuse plan chunk 3; zero without a pool).
    pool: execution::K1Pool,
}

#[cfg(test)]
mod report_tests {
    #[test]
    fn vcpu_state_matches_the_former_format() {
        let mut n = 0;
        let mut next = || {
            n += 1_000_003u64;
            n
        };
        let v = super::VcpuState {
            blocks: next(),
            l2_hits: next(),
            requests: next(),
            retries: next(),
            upgrades: next(),
            evictions: next(),
            linker: next(),
            links: next(),
            unlinks: next(),
            invalidations: next(),
            steps: next(),
            shootdowns: next(),
            systems: next(),
            halts: next(),
            interrupts: next(),
            deliveries: next(),
            stops: next(),
            shadow_resets: next(),
            compiles: next(),
            hits: next(),
            disk_hits: next(),
            relocated: next(),
            relocation_refused: next(),
            relocation_fallbacks: next(),
            reuse_checks: next(),
            hits_by_source: (0..64).map(|_| next()).collect(),
            duplicates: next(),
            lock_wait: [next(), next()],
            recompiled: [next(), next()],
            admissions: [("skipped", 1), ("fresh", 2)].into_iter().collect(),
            pool: Default::default(),
        };
        let old = format!(
            "{{\"blocks\":{},\"l2_hits\":{},\"requests\":{},\"retries\":{},\"upgrades\":{},\"evictions\":{},\"linker\":{},\"links\":{},\"unlinks\":{},\"invalidations\":{},\"steps\":{},\"shootdowns\":{},\"systems\":{},\"halts\":{},\"interrupts\":{},\"deliveries\":{},\"stops\":{},\"shadow_resets\":{},\"compiles\":{},\"hits\":{},\"disk_hits\":{},\"relocated\":{},\"relocation_refused\":{},\"relocation_fallbacks\":{},\"reuse_checks\":{},\"hits_by_source\":{:?},\"duplicates\":{},\"lock_wait\":[{},{}],\"recompiled\":[{},{}]}}",
            v.blocks,
            v.l2_hits,
            v.requests,
            v.retries,
            v.upgrades,
            v.evictions,
            v.linker,
            v.links,
            v.unlinks,
            v.invalidations,
            v.steps,
            v.shootdowns,
            v.systems,
            v.halts,
            v.interrupts,
            v.deliveries,
            v.stops,
            v.shadow_resets,
            v.compiles,
            v.hits,
            v.disk_hits,
            v.relocated,
            v.relocation_refused,
            v.relocation_fallbacks,
            v.reuse_checks,
            v.hits_by_source,
            v.duplicates,
            v.lock_wait[0],
            v.lock_wait[1],
            v.recompiled[0],
            v.recompiled[1]
        );
        let mut old: serde_json::Value = serde_json::from_str(&old).unwrap();
        old["admissions"] = serde_json::json!({"skipped": 1, "fresh": 2});
        old["pool"] = serde_json::json!({"probes": 0, "hits": 0, "candidates": 0, "refused": {}, "recheck": {}});
        assert_eq!(old, serde_json::to_value(&v).unwrap());
    }
}

/// Pins what old command lines produce, before any refactor of how devices are configured. Each line of `EXPECTED` is
/// `manifest|PCI functions|digest of the ACPI blobs (address and bytes)|mailbox|(low_end, high, pci)|digest of the e820
/// map`, for 1 and 4 vCPUs, the 8 subsets of {rng, disk, net} (bit 0 rng, 1 disk, 2 net) and four transport mixes (all
/// MMIO, all PCI, disk=pci only, net and rng pci with disk mmio), at 1 GiB, then one 5 GiB all-PCI case. RECORDED on the
/// unmodified `prepare()` (commit 33a99fb8: the same logic inline in a throwaway test, printing these strings), then
/// the table building was extracted into `launch_plan` and this test, now calling it, passes unchanged.
#[cfg(test)]
mod pin_tests {
    use super::*;
    fn fnv(parts: impl IntoIterator<Item = u8>) -> u64 {
        parts.into_iter().fold(0xcbf2_9ce4_8422_2325, |h, b| {
            (h ^ u64::from(b)).wrapping_mul(0x100_0000_01b3)
        })
    }
    fn describe(vcpus: usize, en: [bool; 3], t: Transports, ram: u64) -> String {
        let p = launch_plan(vcpus, boot::ACPI_NVS.0, en, t, ram).unwrap();
        let tdig = fnv(p
            .tables
            .blobs
            .iter()
            .flat_map(|(a, b)| a.to_le_bytes().into_iter().chain(b.iter().copied())));
        let edig = fnv(p.layout.e820().unwrap().iter().flat_map(|e| {
            [e.addr, e.size, e.kind.into()]
                .into_iter()
                .flat_map(u64::to_le_bytes)
        }));
        let m: Vec<_> = p.manifest.iter().map(|r| (r.uid, r.base, r.gsi)).collect();
        format!(
            "{m:?}|{:?}|{tdig}|{}|{:?}|{edig}",
            p.functions,
            p.tables.mailbox,
            (p.layout.low_end, p.layout.high, p.layout.pci)
        )
    }
    #[test]
    fn launch_plans_are_unchanged() {
        use Transport::{Mmio, Pci};
        let mk = |disk, net, rng| Transports { disk, net, rng };
        let mixes = [
            Transports::all(Mmio),
            Transports::all(Pci),
            mk(Pci, Mmio, Mmio),
            mk(Mmio, Pci, Pci),
        ];
        let mut got = Vec::new();
        for vcpus in [1usize, 4] {
            for bits in 0..8u8 {
                let en = [bits & 1 != 0, bits & 2 != 0, bits & 4 != 0];
                for t in mixes {
                    got.push(describe(vcpus, en, t, 1 << 30));
                }
            }
        }
        got.push(describe(2, [true; 3], Transports::all(Pci), 5 << 30));
        assert_eq!(got.len(), EXPECTED.len());
        for (i, (g, e)) in got.iter().zip(EXPECTED).enumerate() {
            assert_eq!(g, e, "case {i}");
        }
    }
    /// Bad RAM sizes give the memory map's error (before any vCPU check in `prepare`), and 0 or 255 vCPUs the table
    /// builder's assert, as on the original code (checked at 33a99fb8 with a throwaway test on the original calls). The
    /// host-limit error is `require_vcpus`'s, which `prepare` still calls between the two.
    #[test]
    fn bad_sizes_give_the_original_errors() {
        let t = Transports::default();
        for ram in [1u64 << 20, (1 << 30) + 4096, 0] {
            let want = format!(
                "load refused: BAD_LAYOUT: BadLayout(\"low RAM end {:#x}: a 2 MiB multiple above the map's RAM start 0x1000000, at most 3 GiB\")",
                ram.min(exec::LOW_RAM_END)
            );
            assert_eq!(launch_layout([true; 3], t, ram).unwrap_err(), want);
            assert_eq!(
                launch_plan(1, boot::ACPI_NVS.0, [true; 3], t, ram).err(),
                Some(want)
            );
        }
        for v in [0usize, 255] {
            let r = std::panic::catch_unwind(|| {
                launch_plan(v, boot::ACPI_NVS.0, [false; 3], t, 1 << 30).map(|_| ())
            });
            let msg = *r.unwrap_err().downcast::<&str>().unwrap();
            assert_eq!(msg, "xAPIC MADT entries hold APIC IDs below 255");
        }
    }
    fn describe_original(
        vcpus: usize,
        rsdp: u64,
        en: [bool; 3],
        t: Transports,
        ram: u64,
    ) -> String {
        let [rng, disk, net] = en;
        let on_pci = |enabled: bool, t: Transport| enabled && t == Transport::Pci;
        let pci_rng = on_pci(rng, t.rng);
        let pci_disk = on_pci(disk, t.disk);
        let pci_net = on_pci(net, t.net);
        let layout = Layout {
            low_end: ram.min(exec::LOW_RAM_END),
            high: ram.saturating_sub(exec::LOW_RAM_END),
            pci: pci_rng || pci_disk || pci_net,
        };
        let e820 = layout.e820().unwrap();
        let (mut manifest, mut functions) = (Vec::new(), Vec::new());
        for (enabled, pci, resource, slot) in [
            (rng, pci_rng, crate::virtio::RNG_RESOURCE, RNG_SLOT),
            (disk, pci_disk, crate::virtio::BLOCK_RESOURCE, BLOCK_SLOT),
            (net, pci_net, crate::virtio::NET_RESOURCE, NET_SLOT),
        ] {
            if pci {
                functions.push((slot, resource.gsi));
            } else if enabled {
                manifest.push(resource);
            }
        }
        functions.sort_unstable();
        let tables = boot::acpi::build_with_devices(vcpus, rsdp, &manifest, &functions);
        let tdig = fnv(tables
            .blobs
            .iter()
            .flat_map(|(a, b)| a.to_le_bytes().into_iter().chain(b.iter().copied())));
        let edig = fnv(e820.iter().flat_map(|e| {
            [e.addr, e.size, e.kind.into()]
                .into_iter()
                .flat_map(u64::to_le_bytes)
        }));
        let m: Vec<_> = manifest.iter().map(|r| (r.uid, r.base, r.gsi)).collect();
        format!(
            "{m:?}|{functions:?}|{tdig}|{}|{:?}|{edig}",
            tables.mailbox,
            (layout.low_end, layout.high, layout.pci)
        )
    }
    /// Provenance: `describe_original` is the table-building code of the unmodified `prepare()` (33a99fb8), inline and
    /// verbatim, with the real RSDP (`boot::ACPI_NVS.0`). `cargo test -p natemu --lib -- --ignored original_logic`
    /// regenerates every pinned string from it and compares; `--nocapture` prints them.
    #[test]
    #[ignore]
    fn original_logic_matches_the_pins() {
        let mk = |disk, net, rng| Transports { disk, net, rng };
        use Transport::{Mmio, Pci};
        let mixes = [
            Transports::all(Mmio),
            Transports::all(Pci),
            mk(Pci, Mmio, Mmio),
            mk(Mmio, Pci, Pci),
        ];
        let mut got = Vec::new();
        for vcpus in [1usize, 4] {
            for bits in 0..8u8 {
                let en = [bits & 1 != 0, bits & 2 != 0, bits & 4 != 0];
                for t in mixes {
                    got.push(describe_original(vcpus, boot::ACPI_NVS.0, en, t, 1 << 30));
                }
            }
        }
        got.push(describe_original(
            2,
            boot::ACPI_NVS.0,
            [true; 3],
            Transports::all(Pci),
            5 << 30,
        ));
        for g in &got {
            println!("    \"{g}\",");
        }
        assert_eq!(got, EXPECTED);
    }
    const EXPECTED: [&str; 65] = [
        "[]|[]|18415784877665836832|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[]|18415784877665836832|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[]|18415784877665836832|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[]|18415784877665836832|6295552|(1073741824, 0, false)|11246234427793253812",
        "[(2, 4026540032, 18)]|[]|3444692196014619991|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(3, 18)]|17472892613165306677|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18)]|[]|3444692196014619991|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(3, 18)]|17472892613165306677|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[]|2944441749038479963|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(1, 16)]|12622583267433883005|6295552|(1073741824, 0, true)|10185747342727102902",
        "[]|[(1, 16)]|12622583267433883005|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[]|2944441749038479963|6295552|(1073741824, 0, false)|11246234427793253812",
        "[(2, 4026540032, 18), (0, 4026531840, 16)]|[]|10735271959007425989|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(1, 16), (3, 18)]|13792071679463081151|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18)]|[(1, 16)]|18131088272321461727|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[(3, 18)]|13311620853055878131|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(1, 4026535936, 17)]|[]|16074747690373841533|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(2, 17)]|3790317606079308799|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(1, 4026535936, 17)]|[]|16074747690373841533|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(2, 17)]|3790317606079308799|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18), (1, 4026535936, 17)]|[]|6048492150710054675|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(2, 17), (3, 18)]|17097972880732849769|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18), (1, 4026535936, 17)]|[]|6048492150710054675|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(2, 17), (3, 18)]|17097972880732849769|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16), (1, 4026535936, 17)]|[]|11636300326577872741|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(1, 16), (2, 17)]|10138172638859746085|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(1, 4026535936, 17)]|[(1, 16)]|18209356342807899333|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[(2, 17)]|2438782909292830041|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18), (0, 4026531840, 16), (1, 4026535936, 17)]|[]|11879701209480616447|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(1, 16), (2, 17), (3, 18)]|704756924329953899|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18), (1, 4026535936, 17)]|[(1, 16)]|15113218587310913917|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[(2, 17), (3, 18)]|18086586185133981191|6295552|(1073741824, 0, true)|10185747342727102902",
        "[]|[]|150797512702890517|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[]|150797512702890517|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[]|150797512702890517|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[]|150797512702890517|6295552|(1073741824, 0, false)|11246234427793253812",
        "[(2, 4026540032, 18)]|[]|17257018383566105557|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(3, 18)]|7668949667313669293|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18)]|[]|17257018383566105557|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(3, 18)]|7668949667313669293|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[]|8125311451615939063|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(1, 16)]|2561036390405840357|6295552|(1073741824, 0, true)|10185747342727102902",
        "[]|[(1, 16)]|2561036390405840357|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[]|8125311451615939063|6295552|(1073741824, 0, false)|11246234427793253812",
        "[(2, 4026540032, 18), (0, 4026531840, 16)]|[]|685955031401469039|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(1, 16), (3, 18)]|2282458943409544929|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18)]|[(1, 16)]|9922772884338512113|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[(3, 18)]|12990299782277238375|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(1, 4026535936, 17)]|[]|5065958628211627487|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(2, 17)]|590807252964984123|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(1, 4026535936, 17)]|[]|5065958628211627487|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(2, 17)]|590807252964984123|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18), (1, 4026535936, 17)]|[]|6743207254693383149|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(2, 17), (3, 18)]|10017376746478338695|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18), (1, 4026535936, 17)]|[]|6743207254693383149|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(2, 17), (3, 18)]|10017376746478338695|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16), (1, 4026535936, 17)]|[]|11477464473974112751|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(1, 16), (2, 17)]|3563416199795316955|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(1, 4026535936, 17)]|[(1, 16)]|15518724622879309227|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[(2, 17)]|17559797663622712537|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18), (0, 4026531840, 16), (1, 4026535936, 17)]|[]|14458187756917140890|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(1, 16), (2, 17), (3, 18)]|10527073856450925885|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18), (1, 4026535936, 17)]|[(1, 16)]|8807839146401585182|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[(2, 17), (3, 18)]|11823347122285390973|6295552|(1073741824, 0, true)|10185747342727102902",
        "[]|[(1, 16), (2, 17), (3, 18)]|2398346617000464241|6295552|(3221225472, 2147483648, true)|161888604255309990",
    ];
}
