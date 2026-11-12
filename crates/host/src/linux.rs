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
    /// Debug: admission's code-shape check on every object (`Compiler::shape`; the test driver's `boot-linux --code-shape`).
    pub code_shape: bool,
    /// PCID plan P1 (owner, November 5): turn on the resident's page-walk speculation barriers. Off by default; on, the
    /// host must have FEAT_SB (checked in [`Boot::prepare`]) and IA32_ARCH_CAPABILITIES advertises RDCL_NO when the host
    /// also has FEAT_CSV3, so a default command line then boots with PTI off. `--speculation-barriers` / `NATEMU_SPECULATION_BARRIERS`.
    pub speculation_barriers: bool,
    /// Expose the modern virtio-MMIO entropy device (native entropy, copied completions).
    pub rng: bool,
    /// Owned writable raw disk; excludes other writers/truncators for this VM's lifetime.
    pub disk: Option<PathBuf>,
    /// Expose the virtio-MMIO network device on user-mode NAT ([`crate::virtio::Slirp`]).
    pub net: bool,
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
            code_shape: false,
            speculation_barriers: false,
            rng: false,
            disk: None,
            net: false,
        }
    }

    /// Loads everything into a new backing and builds the devices; nothing runs yet.
    pub fn prepare(self) -> Result<Prepared> {
        // PCID plan P1: the barriers' leaf SB needs FEAT_SB; refuse to turn them on without it (default off needs nothing).
        if self.speculation_barriers && !execution::host_feature("FEAT_SB") {
            return Err(
                "--speculation-barriers: this host lacks FEAT_SB (sysctl hw.optional.arm.FEAT_SB)"
                    .into(),
            );
        }
        let layout = Layout {
            low_end: self.ram.min(exec::LOW_RAM_END),
            high: self.ram.saturating_sub(exec::LOW_RAM_END),
        };
        layout.e820().map_err(|e| e.to_string())?; // before allocating the RAM
        require_vcpus(self.vcpus)?;
        let backing = execution::prepare(
            &self.runtime.to_string_lossy(),
            self.code_bytes,
            self.vcpus,
            self.ram as usize,
        )?;
        let mut manifest = Vec::new();
        if self.rng {
            manifest.push(crate::virtio::RNG_RESOURCE);
        }
        if self.disk.is_some() {
            manifest.push(crate::virtio::BLOCK_RESOURCE);
        }
        if self.net {
            manifest.push(crate::virtio::NET_RESOURCE);
        }
        let tables = boot::acpi::build_with_virtio(self.vcpus, self.rsdp, &manifest);
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
            );
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
        if self.rng {
            let resource = crate::virtio::RNG_RESOURCE;
            let rng = crate::virtio::Virtio::rng(irqs.clone(), resource.gsi as usize);
            devices.register(resource.base.into(), 4096, rng.device())?;
            virtio.push(rng);
        }
        if let Some(path) = &self.disk {
            let resource = crate::virtio::BLOCK_RESOURCE;
            let disk = crate::virtio::Virtio::block(irqs.clone(), resource.gsi as usize, path)?;
            devices.register(resource.base.into(), 4096, disk.device())?;
            virtio.push(disk);
        }
        let mut net = None;
        if self.net {
            let resource = crate::virtio::NET_RESOURCE;
            let mac = crate::virtio::Slirp::MAC;
            let (device, port) =
                crate::virtio::Virtio::network(irqs.clone(), resource.gsi as usize, mac)?;
            net = Some(crate::virtio::Slirp::new(port)?);
            devices.register(resource.base.into(), 4096, device.device())?;
            virtio.push(device);
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
            compile_cache: self.compile_cache,
            check_reuse: self.check_reuse,
            code_shape: self.code_shape,
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
    check_reuse: bool,
    code_shape: bool,
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
            compiler.share(&answers);
            compiler.check_reuse = self.check_reuse;
            compiler.shape = self.code_shape;
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
        let old: serde_json::Value = serde_json::from_str(&old).unwrap();
        assert_eq!(old, serde_json::to_value(&v).unwrap());
    }
}
