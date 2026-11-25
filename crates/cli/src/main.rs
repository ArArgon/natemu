//! `natemu`, the boot entry: boots a Linux bzImage on the library's [`natemu::linux::Boot`] with the persistent compile
//! cache on by default ([`natemu::compile_cache::Config::new`]).
//!
//!   natemu [--config FILE] [--set KEY=VALUE]... [--fexc FILE] [--kernel FILE | --firmware FILE] [--runtime FILE] [--initrd FILE] [--cmdline TEXT]
//!       [--ram SIZE] [--vcpus N] [--serial FILE] [--serial-stdin] [--speculation-barriers] [--fp fast|accurate] [--pool[=false]]
//!       [--check-reuse] [--compile-cache DIR|off] [--compile-cache-mb N] [--perf-log FILE] [--page-currency on|off|check]
//!       [--writable-on-read on|off] [--rng] [--disk FILE] [--net] [--net-forward [tcp|udp:]HOST_PORT:GUEST_PORT]
//!       [--net-host-loopback] [--virtio-transport SPEC]
//!
//! Named arguments (clap; `natemu --help`); the configuration file is described in docs/configuration.md.
//!
//! **Layers.** A launch is built lowest first from: the built-in defaults, the `--config FILE`, the deprecated environment
//! variables, the flags, then `--set KEY=VALUE` (so `--set` wins). `--kernel` and `--fexc` are required unless the file gives
//! `[boot.kernel] image` and `[engine] fexc`. M2 Part 2: `--firmware FILE` (or `[boot.firmware] image`) boots a firmware image
//! instead of a kernel, at the top of the 4 GiB space, read-only, the boot processor at its reset vector in the power-on state
//! (`[boot.firmware] start = "flat32"`: in flat 32-bit protected mode), the others waiting for a start-up IPI; no initrd,
//! command line or virtio device then. `--disk`, `--net` and `--rng` are refused when the file lists devices of that
//! kind; edit a listed device with `--set disk.ID.path=...`. Paths in the file are relative to the file's folder; paths in
//! flags and `--set` are relative to the working directory.
//!
//! **Transport.** Virtio devices attach over PCI when a `--config` file is given and over MMIO without one (the old command
//! lines); `--virtio-transport` and `machine.transport` override either.
//!
//! **Disks.** `--disk FILE` is the first writable disk (id `disk0`); the image format (raw or qcow2) is detected from its
//! first bytes, not from the name. Disks beyond the first, read-only disks, ids and qcow2 backing files (`backing`) come
//! from the file or `--set disk.ID.backing=FILE`.
//!
//! **Serial.** `--serial FILE` writes COM1 to that file, so `--serial stdout` is a file named `stdout`; the keyword form is
//! `[serial] output = "stdout"` (or `--set serial.output=stdout`), which means standard output. Without either, COM1 goes
//! to standard output. The stop of the vCPU that ended the run is written to standard error, after each vCPU's report
//! (`natemu::execution`'s diagnostics: guest RIP, mode, CPL, instruction bytes, registers, code identity; none at a power-off,
//! a reset, a guest panic or a cancel).
//! SIGINFO (Ctrl-T) prints every vCPU's report while the run goes on (`debug.inspect`, on by default); `debug.watchdog`
//! (seconds, default 60) bounds a vCPU's stay in guest code without a host exit (the run ends with its report); a vCPU that
//! long in the host outside a compile (a blocked serial write, say) is reported once and the run goes on.
//!
//! **Environment variables (deprecated).** Ten flags still fall back to a variable: `NATEMU_SERIAL_STDIN`,
//! `NATEMU_SPECULATION_BARRIERS`, `NATEMU_FP`, `NATEMU_PAGE_CURRENCY`, `NATEMU_WRITABLE_ON_READ`, `NATEMU_POOL`,
//! `NATEMU_CHECK_REUSE`, `NATEMU_COMPILE_CACHE`, `NATEMU_COMPILE_CACHE_MB` and `NATEMU_PERF_LOG`. They work as before (a flag
//! wins over its variable; a switch's variable takes 1/0, true/false, yes/no, on/off) but each one used prints once to
//! standard error `natemu: NAME is deprecated; use [section] field or --set section.field=...`.
//!
//! `--runtime` defaults to the resident built beside this binary (`<target>/aarch64-unknown-none/<profile>/natemu-runtime`
//! for `<target>/<profile>/natemu`) when that file exists. `--ram` takes bytes, 0x hex, or a K/M/G suffix (binary units:
//! 2G = 2048M = 0x80000000). Exits 0 when the guest powered off, 2 at any other stop (a guest panic included; SIGINT or
//! SIGTERM cancels the run: `Cancelled`; a second one exits 130 at once), 1 on a host error (a failed serial write and a
//! refused command line included; `--help` exits 0). `--perf-log FILE` turns on the library's timing log (`natemu::perf`).
//! `--serial-stdin` (off by default) feeds standard input to COM1 (`natemu::linux::serial_stdin`): a terminal in raw mode,
//! Ctrl-C to the guest, Ctrl-A x cancels the run. `--page-currency` and `--writable-on-read` are debug and measurement switches (see their
//! help). `--pool` (default on; `--pool=false` or `NATEMU_POOL=0` turns it off) is the shared template pool.
//! `--speculation-barriers` turns on the resident's page-walk barriers (refused without FEAT_SB; with FEAT_CSV3 they
//! advertise RDCL_NO: `natemu::linux::Boot::speculation_barriers`).
//!
//! **Platform devices.** `machine.pvpanic` (default on; `--set machine.pvpanic=false` turns it off) adds the pvpanic device
//! (`natemu::pvpanic`, `natemu::linux::Boot::pvpanic`): a guest kernel's panic ends the run with `Power(4)` (the
//! driver's `panic`), exit status 2. `machine.smbios` (default on; `--set machine.smbios=false` turns it off) adds SMBIOS 3.0
//! tables (`natemu::boot::smbios`, `natemu::linux::Boot::smbios`): the guest's DMI scan finds their entry point at F0000H;
//! a firmware boot ignores it. `machine.ps2` (default on; `--set machine.ps2=false` turns it off) adds the 8042 keyboard
//! controller with a PS/2 keyboard and mouse (`natemu::ps2`, `natemu::linux::Boot::ps2`); without it port 64H only
//! takes the reset command FEH. Either way `reboot=k` ends the run with `Power(2)`.
mod config;
use clap::{CommandFactory, FromArgMatches};
use natemu::execution::Stop;
use natemu::linux::{Transport, Transports};
use natemu::virtio::{ForwardProtocol, PortForward, SlirpOptions};
use std::path::PathBuf;

/// Largest image or initrd read (the boot protocol's 32-bit sizes).
const MAX_INPUT: usize = 1 << 32;

/// Boot a Linux bzImage (x86-64) under natemu.
#[derive(clap::Parser)]
#[command(
    name = "natemu",
    after_help = "Settings are layered, lowest first: defaults, --config FILE, environment variables (deprecated), flags, --set. \
    --kernel and --fexc may instead come from the file. Virtio devices use PCI with a --config file and MMIO without one \
    (--virtio-transport overrides). The ten environment variables NATEMU_SERIAL_STDIN, _SPECULATION_BARRIERS, _FP, \
    _PAGE_CURRENCY, _WRITABLE_ON_READ, _POOL, _CHECK_REUSE, _COMPILE_CACHE, _COMPILE_CACHE_MB and _PERF_LOG are deprecated: \
    they still work, and each one used prints a warning to standard error naming the file key to use. \
    See docs/configuration.md.\n\n\
    Exit status: 0 the guest powered off, 2 any other stop (reset, a guest panic, a typed stop, a watchdog, \
    SIGINT/SIGTERM), 1 a host error. A flag wins over its environment variable."
)]
struct Cli {
    /// A configuration file (TOML; see docs/configuration.md); its settings are overridden by the environment variables, the flags and `--set`
    #[arg(long, value_name = "FILE")]
    config: Option<PathBuf>,
    /// Override one setting, e.g. `machine.ram=4G` or `disk.root.readonly=true` (repeatable; wins over everything else; a key set twice is an error; a device is addressed by its id and cannot be created)
    #[arg(long = "set", value_name = "KEY=VALUE", allow_hyphen_values = true)]
    set: Vec<String>,
    /// Which flags clap took from the command line and from the environment (filled by [`parse_cli`])
    #[arg(skip)]
    given: config::Given,
    /// The resident runtime ELF [default: the one built beside this binary]
    #[arg(long, value_name = "FILE")]
    runtime: Option<PathBuf>,
    /// The fexc compiler (required unless the --config file has `[engine] fexc`)
    #[arg(long, value_name = "FILE")]
    fexc: Option<PathBuf>,
    /// The Linux bzImage (required unless the --config file has `[boot.kernel] image`, or a firmware image is booted)
    #[arg(long, value_name = "FILE")]
    kernel: Option<PathBuf>,
    /// Boot a firmware image instead of a kernel: it lies at the top of the 4 GiB space, read-only, and the boot processor
    /// starts at its reset vector (`[boot.firmware]` in the file; docs/configuration.md)
    #[arg(long, value_name = "FILE", conflicts_with_all = ["kernel", "initrd", "cmdline"])]
    firmware: Option<PathBuf>,
    /// An initrd (initramfs) to load
    #[arg(long, value_name = "FILE")]
    initrd: Option<PathBuf>,
    /// The kernel command line
    #[arg(long, value_name = "TEXT", allow_hyphen_values = true)]
    cmdline: Option<String>,
    /// Guest RAM: bytes, 0x hex, or a K/M/G suffix [default: 2G]
    #[arg(long, value_name = "SIZE", value_parser = size)]
    ram: Option<u64>,
    /// Processors [default: 1]
    #[arg(long, value_name = "N")]
    vcpus: Option<usize>,
    /// Write COM1's output to FILE [default: standard output]; the name `stdout` here is a file (the file key `output = "stdout"` means standard output)
    #[arg(long, value_name = "FILE")]
    serial: Option<PathBuf>,
    /// Feed standard input to COM1; Ctrl-A x cancels the run
    #[arg(long, env = "NATEMU_SERIAL_STDIN", value_parser = clap::builder::BoolishValueParser::new())]
    serial_stdin: bool,
    /// The resident's page-walk speculation barriers (needs FEAT_SB)
    #[arg(long, env = "NATEMU_SPECULATION_BARRIERS", value_parser = clap::builder::BoolishValueParser::new())]
    speculation_barriers: bool,
    /// Floating-point mode: fast (the default) or accurate (changes the generated code only on a host without FEAT_AFP)
    #[arg(long, env = "NATEMU_FP", value_name = "fast|accurate", default_value = "fast", action = clap::ArgAction::Set, value_parser = natemu::execution::parse_fp)]
    fp: bool,
    /// Debug: a per-vCPU record of instruction-fetch page walks answers fetch translations (on, the default), none does (off),
    /// or it answers and the walk is made too, any difference counted (check)
    #[arg(long, env = "NATEMU_PAGE_CURRENCY", value_name = "on|off|check", default_value = "on", value_parser = parse_page_currency)]
    page_currency: u64,
    /// Debug: a demand read maps the page writable when the guest entry is dirty and writable, saving a later fault (on, the
    /// default), or read-only (off); for A/B measurement
    #[arg(long, env = "NATEMU_WRITABLE_ON_READ", value_name = "on|off", default_value = "on", action = clap::ArgAction::Set, value_parser = natemu::execution::parse_writable_on_read)]
    writable_on_read: bool,
    /// Expose the virtio entropy device
    #[arg(long)]
    rng: bool,
    /// The first disk, writable (id disk0; the image format is detected from its content: raw or qcow2 (an overlay also needs --set disk.disk0.backing=BASE); exclusive use; a guest power-off flushes it to the device). More disks, read-only disks and ids need a --config file
    #[arg(long, value_name = "FILE")]
    disk: Option<PathBuf>,
    /// Expose a virtio network device on user-mode NAT (libslirp; IPv4, DHCP, DNS)
    #[arg(long)]
    net: bool,
    /// Forward host 127.0.0.1:HOST_PORT to guest 10.0.2.15:GUEST_PORT (repeatable; default TCP)
    /// UDP requires --net-host-loopback; host port 53 and repeated UDP guest ports are unsupported
    #[arg(long, requires = "net", value_name = "[tcp|udp:]HOST_PORT:GUEST_PORT", value_parser = port_forward)]
    net_forward: Vec<PortForward>,
    /// Allow guest 10.0.2.2:PORT to reach host 127.0.0.1:PORT (broad localhost access; default off)
    #[arg(long, requires = "net")]
    net_host_loopback: bool,
    /// Debug: the shared template pool (a read-only 274 MiB span of address space, backed as touched): the host publishes admitted
    /// templates into it and each resident looks in it before asking the host. On by default; `--pool=false` or `NATEMU_POOL=0`
    /// turns it off (`NATEMU_POOL_LOOKUP=off`: publication only)
    #[arg(
        long,
        env = "NATEMU_POOL",
        value_parser = clap::builder::BoolishValueParser::new(),
        default_value_t = true,
        num_args = 0..=1,
        require_equals = true,
        default_missing_value = "true"
    )]
    pool: bool,
    /// Debug: every block the resident copies instead of asking the host (a template of its own, or a pool entry) and every
    /// answer reused at another RIP is also compiled by the host and must be equal (a mismatch stops the run)
    #[arg(long, env = "NATEMU_CHECK_REUSE", value_parser = clap::builder::BoolishValueParser::new())]
    check_reuse: bool,
    /// How virtio devices attach: `mmio` or `pci` for all, or (experimental) per device, e.g. `disk=pci,net=mmio,rng=mmio`
    /// (keys disk, net, rng, console; unnamed devices take mmio) [default: pci with a --config file, mmio without one]
    #[arg(long, value_name = "SPEC", value_parser = transports, default_value = "mmio", hide_default_value = true)]
    virtio_transport: Transports,
    /// The persistent compile cache's directory, or off [default: ~/Library/Caches/natemu/fexc]
    #[arg(long, value_name = "DIR|off", env = "NATEMU_COMPILE_CACHE")]
    compile_cache: Option<PathBuf>,
    /// The compile cache's cap in MiB [default: 512]
    #[arg(long, value_name = "N", env = "NATEMU_COMPILE_CACHE_MB")]
    compile_cache_mb: Option<u64>,
    /// Write the timing log (natemu::perf) to FILE
    #[arg(long, value_name = "FILE", env = "NATEMU_PERF_LOG")]
    perf_log: Option<PathBuf>,
}

/// Parses the command line, recording which flags came from it and which from an environment variable.
fn parse_cli<I, T>(args: I) -> Result<Cli, clap::Error>
where
    I: IntoIterator<Item = T>,
    T: Into<std::ffi::OsString> + Clone,
{
    use clap::parser::ValueSource;
    let matches = Cli::command().try_get_matches_from(args)?;
    let mut cli = Cli::from_arg_matches(&matches)?;
    for id in matches.ids() {
        let id = id.as_str();
        match matches.value_source(id) {
            Some(ValueSource::CommandLine) => cli.given.cmd.insert(id.to_string()),
            Some(ValueSource::EnvVariable) => cli.given.env.insert(id.to_string()),
            _ => false,
        };
    }
    Ok(cli)
}

fn port_forward(text: &str) -> Result<PortForward, String> {
    let bad =
        || format!("bad forward {text:?}: [tcp|udp:]HOST_PORT:GUEST_PORT, decimal ports 1–65535");
    let fields: Vec<_> = text.split(':').collect();
    let (protocol, host, guest) = match fields.as_slice() {
        [host, guest] => (ForwardProtocol::Tcp, *host, *guest),
        ["tcp", host, guest] => (ForwardProtocol::Tcp, *host, *guest),
        ["udp", host, guest] => (ForwardProtocol::Udp, *host, *guest),
        _ => return Err(bad()),
    };
    let port = |digits: &str| {
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return Err(bad());
        }
        digits.parse().map_err(|_| bad())
    };
    Ok(PortForward {
        protocol,
        host_port: port(host)?,
        guest_port: port(guest)?,
    })
}

fn network_summary(enabled: bool, options: &SlirpOptions) -> String {
    use std::fmt::Write;
    if !enabled {
        return String::new();
    }
    let mut summary = String::from(
        "natemu-net: libslirp IPv4; DHCP expected 10.0.2.15; gateway 10.0.2.2; DNS 10.0.2.3\n",
    );
    summary.push_str(if options.host_loopback {
        "natemu-net: host loopback enabled: 10.0.2.2:PORT -> host 127.0.0.1:PORT\n"
    } else {
        "natemu-net: host loopback disabled\n"
    });
    for rule in &options.forwards {
        writeln!(&mut summary, "natemu-net: {rule}").unwrap();
    }
    summary
}

/// No success text escapes on a preparation failure, including a refused forward.
fn report_prepared<T>(
    prepared: natemu::Result<T>,
    summary: &str,
    output: &mut impl std::io::Write,
) -> natemu::Result<T> {
    let prepared = prepared?;
    output
        .write_all(summary.as_bytes())
        .map_err(|e| format!("network summary: {e}"))?;
    Ok(prepared)
}

/// `--page-currency`: on, off or check (the library also knows `check-halt`, a test mode that stops the run at a difference).
fn parse_page_currency(text: &str) -> Result<u64, String> {
    if text == "check-halt" {
        return Err("--page-currency check-halt: on, off or check".into());
    }
    natemu::execution::parse_page_currency(text)
}

/// Bytes, 0x hex, or decimal with a K/M/G (binary) suffix.
fn size(text: &str) -> Result<u64, String> {
    let bad = || format!("bad size {text}: bytes, 0x hex, or a K/M/G suffix");
    let (digits, shift) = match text.as_bytes().last() {
        Some(b'K' | b'k') => (&text[..text.len() - 1], 10),
        Some(b'M' | b'm') => (&text[..text.len() - 1], 20),
        Some(b'G' | b'g') => (&text[..text.len() - 1], 30),
        _ => (text, 0),
    };
    let n = match digits.strip_prefix("0x") {
        Some(h) => u64::from_str_radix(h, 16),
        None => digits.parse(),
    }
    .map_err(|_| bad())?;
    n.checked_mul(1 << shift).ok_or_else(bad)
}

/// `mmio`, `pci`, or `key=value,...` with keys disk, net, rng, console and values mmio or pci (unnamed keys: mmio).
fn transports(text: &str) -> Result<Transports, String> {
    let value = |v: &str| match v {
        "mmio" => Ok(Transport::Mmio),
        "pci" => Ok(Transport::Pci),
        _ => Err(format!("bad transport {v:?}: mmio or pci")),
    };
    if !text.contains('=') {
        return value(text).map(Transports::all);
    }
    let mut t = Transports::default();
    let mut seen = Vec::new();
    for part in text.split(',') {
        let (key, v) = part
            .split_once('=')
            .ok_or_else(|| format!("bad transport {part:?}: expected key=value"))?;
        let slot = match key {
            "disk" => &mut t.disk,
            "net" => &mut t.net,
            "rng" => &mut t.rng,
            "console" => &mut t.console,
            _ => return Err(format!("unknown device {key:?}: disk, net, rng or console")),
        };
        if seen.contains(&key) {
            return Err(format!("device {key:?} named twice"));
        }
        seen.push(key);
        *slot = value(v)?;
    }
    Ok(t)
}

/// Warnings from the PCI layer (`log` calls), a guest can provoke them: 16 lines, then one notice.
struct PciLog(std::sync::atomic::AtomicUsize);
impl log::Log for PciLog {
    fn enabled(&self, m: &log::Metadata) -> bool {
        m.level() <= log::Level::Warn
    }
    fn log(&self, r: &log::Record) {
        const MAX: usize = 16;
        if !self.enabled(r.metadata()) || !r.target().starts_with("natemu::pci") {
            return;
        }
        match self.0.fetch_add(1, std::sync::atomic::Ordering::Relaxed) {
            n if n < MAX => eprintln!("natemu: pci: {}", r.args()),
            n if n == MAX => eprintln!("natemu: pci: further messages suppressed"),
            _ => {}
        }
    }
    fn flush(&self) {}
}

/// The resident beside this binary, if built: `<target>/aarch64-unknown-none/<profile>/natemu-runtime`.
fn built_runtime() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let profile = exe.parent()?;
    let path = profile
        .parent()?
        .join("aarch64-unknown-none")
        .join(profile.file_name()?)
        .join("natemu-runtime");
    path.is_file().then_some(path)
}

fn boot(cli: Cli) -> natemu::Result<Stop> {
    for warning in config::deprecations(&cli) {
        eprintln!("{warning}");
    }
    let cfg = config::load(&cli)?;
    let net_options = cfg.net_options();
    net_options.validate()?;
    // Performance phase (perf plan §2): the library's timing log (`natemu::perf`), off unless set.
    if let Some(path) = &cfg.perf_log {
        natemu::perf::enable(path)?;
        natemu::perf::mark("main");
    }
    let runtime = cfg
        .runtime
        .clone()
        .or_else(built_runtime)
        .ok_or("--runtime FILE: no resident built beside this binary")?;
    let fexc = cfg.fexc.as_ref().unwrap(); // `config::validate` checked
    for (what, path) in [("--runtime", &runtime), ("--fexc", fexc)] {
        std::fs::metadata(path).map_err(|e| format!("{what} {}: {e}", path.display()))?;
    }
    let read = |path: &PathBuf| natemu::memory::read_input(&path.to_string_lossy(), MAX_INPUT);
    // A kernel, or (M2 Part 2) a firmware image: `config::validate` checked there is exactly one.
    let kernel = cfg.kernel.as_ref().map(read).transpose()?.unwrap_or_default();
    let firmware = cfg.firmware.as_ref().map(read).transpose()?;
    let initrd = cfg.initrd.as_ref().map(read).transpose()?;
    let mut boot = config::to_boot(&cfg, runtime, kernel, initrd)?;
    boot.firmware = firmware.map(|image| natemu::boot::firmware::Firmware {
        image,
        start: cfg.firmware_start,
    });
    boot.serial = Some(match &cfg.serial_output {
        Some(path) => Box::new(
            std::fs::File::create(path).map_err(|e| format!("--serial {}: {e}", path.display()))?,
        ),
        None => Box::new(std::io::stdout()),
    });
    let summary = network_summary(boot.net, &boot.net_options);
    let vcpus = boot.vcpus;
    let prepared = report_prepared(boot.prepare(), &summary, &mut std::io::stderr().lock())?;
    // SIGINT/SIGTERM cancel the run (a second one exits at once).
    prepared.canceller().on_signals();
    // M2 task D: SIGINFO (Ctrl-T) prints every vCPU's state and the run goes on; `debug.inspect = false` turns it off.
    if cfg.inspect {
        natemu::execution::inspect_on_signal();
    }
    if cfg.serial_stdin {
        prepared.serial_input(natemu::linux::serial_stdin()); // raw mode only once the signals are handled
    }
    // `[control] socket`: the control API (docs/control-api.md) for the run; without it, no listener.
    let control = match &cfg.control_socket {
        Some(path) => Some(natemu::control::serve(
            path,
            natemu::control::Target {
                ps2: prepared.ps2().cloned(),
                power_button: prepared.power_button(),
                cancel: prepared.canceller(),
                status: natemu::control::StatusSource {
                    vcpus,
                    started: std::time::Instant::now(),
                },
                irqs: prepared.irqs().clone(),
                display: prepared.display(),
            },
        )?),
        None => None,
    };
    // The stop of the vCPU whose end ended the run (any vCPU may power off), else the BSP's.
    let stop = prepared.run().and_then(|mut outcome| {
        // `debug.mapped_testdev`: the region table at the end and each vCPU's installs and remap shootdowns.
        if let Some(mapped) = prepared.mapped_report() {
            eprintln!("natemu: mapped {mapped}");
        }
        if let Some(e) = prepared.serial_error() {
            return Err(format!("serial output: {e}"));
        }
        let first = if outcome.ended_first < outcome.each.len() {
            outcome.ended_first
        } else {
            0
        };
        Ok(outcome.each.swap_remove(first).unwrap().0)
    });
    if let Some(server) = control {
        server.stopped(stop_reason(&stop));
    } // dropped: the socket is removed
    stop
}

/// The control API's `stopped` reason for how the run ended.
fn stop_reason(stop: &natemu::Result<Stop>) -> natemu::control::protocol::StopReason {
    use natemu::control::protocol::StopReason;
    match stop {
        Ok(Stop::Power(natemu::pm::POWEROFF)) => StopReason::PowerOff,
        Ok(Stop::Power(natemu::pm::RESET)) => StopReason::Reset,
        Ok(Stop::Power(natemu::pm::PANIC)) => StopReason::Panic,
        Ok(Stop::Cancelled) => StopReason::Cancelled,
        _ => StopReason::Error,
    }
}

fn main() {
    let cli = match parse_cli(std::env::args_os()) {
        Ok(cli) => cli,
        Err(e) if !e.use_stderr() => e.exit(), // --help: standard output, exit 0
        Err(e) => {
            let _ = e.print();
            std::process::exit(1); // a refused command line is a host error, not a stop (2)
        }
    };
    static LOG: PciLog = PciLog(std::sync::atomic::AtomicUsize::new(0));
    let _ = log::set_logger(&LOG).map(|()| log::set_max_level(log::LevelFilter::Warn));
    match boot(cli) {
        Ok(stop) => {
            eprintln!("natemu: {stop:?}");
            std::process::exit(if matches!(stop, Stop::Power(natemu::pm::POWEROFF)) {
                0
            } else {
                2
            });
        }
        Err(error) => {
            eprintln!("natemu: {error}");
            std::process::exit(1);
        }
    }
}

// CLI parsing reads the environment, which some tests temporarily change; they hold this lock.
#[cfg(test)]
static CLI_ENV: std::sync::Mutex<()> = std::sync::Mutex::new(());

#[cfg(test)]
mod tests {
    use super::CLI_ENV;

    #[test]
    fn udp_policy_is_checked_before_cli_input_files_are_opened() {
        let _env = CLI_ENV.lock().unwrap();
        let base = [
            "natemu",
            "--runtime",
            "missing-runtime",
            "--fexc",
            "missing-fexc",
            "--kernel",
            "missing-kernel",
            "--net",
        ];
        for (extra, expected) in [
            (
                vec!["--net-forward", "udp:15201:5201"],
                "requires --net-host-loopback",
            ),
            (
                vec!["--net-host-loopback", "--net-forward", "udp:53:5201"],
                "UDP forwarding from host port 53 is unsupported",
            ),
            (
                vec![
                    "--net-host-loopback",
                    "--net-forward",
                    "udp:15201:5201",
                    "--net-forward",
                    "udp:15202:5201",
                ],
                "duplicate UDP guest port 5201",
            ),
        ] {
            let cli = super::parse_cli(base.iter().chain(&extra)).unwrap();
            let error = super::boot(cli).err().expect("invalid UDP configuration");
            assert!(error.contains(expected), "{error}");
        }
        for extra in [
            vec!["--net-forward", "15201:5201"],
            vec!["--net-host-loopback", "--net-forward", "udp:15201:5201"],
        ] {
            let cli = super::parse_cli(base.iter().chain(&extra)).unwrap();
            let error = super::boot(cli)
                .err()
                .expect("fixture input does not exist");
            assert!(
                error.contains("--runtime missing-runtime"),
                "accepted policy must reach input checks: {error}"
            );
        }
    }

    /// How the run ended, as the control API's `stopped` reason.
    #[test]
    fn stop_reasons() {
        use natemu::control::protocol::StopReason;
        use natemu::execution::Stop;
        for (stop, reason) in [
            (Ok(Stop::Power(natemu::pm::POWEROFF)), StopReason::PowerOff),
            (Ok(Stop::Power(natemu::pm::RESET)), StopReason::Reset),
            (Ok(Stop::Power(natemu::pm::PANIC)), StopReason::Panic),
            (Ok(Stop::Cancelled), StopReason::Cancelled),
            (Ok(Stop::Watchdog { pc: 0, el0: true, asked: 0 }), StopReason::Error),
            (Ok(Stop::Power(natemu::pm::DEBUG_EXIT)), StopReason::Error),
            (Err("serial output: broken pipe".to_string()), StopReason::Error),
        ] {
            assert_eq!(super::stop_reason(&stop), reason, "{:?}", stop.as_ref().err());
        }
    }

    #[test]
    fn network_summary_describes_policy_and_static_mappings() {
        let mut options = super::SlirpOptions::default();
        assert_eq!(super::network_summary(false, &options), "");
        assert_eq!(
            super::network_summary(true, &options),
            "natemu-net: libslirp IPv4; DHCP expected 10.0.2.15; gateway 10.0.2.2; DNS 10.0.2.3\nnatemu-net: host loopback disabled\n"
        );
        options.host_loopback = true;
        options.forwards = vec![
            super::port_forward("2222:22").unwrap(),
            super::port_forward("udp:19000:9000").unwrap(),
        ];
        assert_eq!(
            super::network_summary(true, &options),
            "natemu-net: libslirp IPv4; DHCP expected 10.0.2.15; gateway 10.0.2.2; DNS 10.0.2.3\nnatemu-net: host loopback enabled: 10.0.2.2:PORT -> host 127.0.0.1:PORT\nnatemu-net: tcp 127.0.0.1:2222 -> 10.0.2.15:22\nnatemu-net: udp 127.0.0.1:19000 -> 10.0.2.15:9000\n"
        );
        assert_eq!(super::network_summary(false, &options), "");
    }

    #[test]
    fn network_summary_is_written_only_for_successful_preparation() {
        let summary = "natemu-net: host loopback disabled\n";
        let mut output = Vec::new();
        let bind_error =
            "network: could not install tcp 127.0.0.1:2222 -> 10.0.2.15:22".to_string();
        assert_eq!(
            super::report_prepared::<()>(Err(bind_error.clone()), summary, &mut output),
            Err(bind_error)
        );
        assert!(
            output.is_empty(),
            "failed preparation must not announce a configured network"
        );
        assert_eq!(super::report_prepared(Ok(7), summary, &mut output), Ok(7));
        assert_eq!(output, summary.as_bytes());
        output.clear();
        assert_eq!(super::report_prepared(Ok(7), "", &mut output), Ok(7));
        assert!(output.is_empty());
    }

    #[test]
    fn network_options_accept_static_forwards_and_require_networking() {
        let _env = CLI_ENV.lock().unwrap();
        let base = ["natemu", "--fexc", "f", "--kernel", "k"];
        let parse = |extra: &[&str]| super::parse_cli(base.iter().chain(extra));
        let defaults = parse(&[]).unwrap();
        assert!(!defaults.net && !defaults.net_host_loopback && defaults.net_forward.is_empty());
        for rule in ["2222:22", "tcp:1:65535", "udp:65535:1"] {
            let cli = parse(&["--net", "--net-forward", rule]).unwrap();
            assert_eq!(cli.net_forward.len(), 1);
            let forward = cli.net_forward[0];
            assert_eq!(
                forward.protocol,
                if rule.starts_with("udp:") {
                    super::ForwardProtocol::Udp
                } else {
                    super::ForwardProtocol::Tcp
                }
            );
            let (host, guest) = if rule == "2222:22" {
                (2222, 22)
            } else if rule.starts_with("tcp:") {
                (1, 65535)
            } else {
                (65535, 1)
            };
            assert_eq!(
                (forward.host_port.get(), forward.guest_port.get()),
                (host, guest)
            );
        }
        let cli = parse(&[
            "--net",
            "--net-host-loopback",
            "--net-forward",
            "2222:22",
            "--net-forward",
            "udp:2222:22",
        ])
        .unwrap();
        assert!(cli.net_host_loopback);
        assert_eq!(
            cli.net_forward
                .iter()
                .map(|f| f.protocol)
                .collect::<Vec<_>>(),
            vec![super::ForwardProtocol::Tcp, super::ForwardProtocol::Udp]
        );
        for extra in [
            vec!["--net-host-loopback"],
            vec!["--net-forward", "2222:22"],
        ] {
            assert!(parse(&extra).is_err());
        }
        for rule in [
            "",
            "1",
            "1:",
            ":1",
            "tcp:1",
            "tcp:1:2:3",
            "sctp:1:2",
            "TCP:1:2",
            "0:1",
            "1:0",
            "65536:1",
            "1:65536",
            "+1:2",
            "1:-2",
            " 1:2",
            "1:2 ",
            "1:2\n",
            "0x10:2",
            "１:2",
        ] {
            assert!(
                parse(&["--net", "--net-forward", rule]).is_err(),
                "{rule:?}"
            );
        }
    }

    /// `--check-reuse` and `NATEMU_CHECK_REUSE` (a flag wins, a switch's variable takes 1/0, true/false, on/off) reach
    /// `Boot::check_reuse`'s switch; off by default.
    #[test]
    fn check_reuse_option() {
        let _env = CLI_ENV.lock().unwrap();
        let base = ["natemu", "--fexc", "f", "--kernel", "k"];
        let parse = |extra: &[&str]| {
            super::parse_cli(base.iter().chain(extra))
                .unwrap()
                .check_reuse
        };
        assert!(!parse(&[]));
        assert!(parse(&["--check-reuse"]));
        assert!(!parse(&["--pool"]));
        // The pool: on by default, `--pool` on, `--pool=false` off, `NATEMU_POOL` 1/0 either way (a flag wins).
        let pool = |extra: &[&str]| super::parse_cli(base.iter().chain(extra)).unwrap().pool;
        assert!(
            pool(&[]) && pool(&["--pool"]) && pool(&["--pool=true"]) && !pool(&["--pool=false"])
        );
        assert!(super::parse_cli(base.iter().chain(&["--pool", "false"])).is_err());
        // The variable, set for this test only (CLI_ENV excludes other CLI parsing tests).
        for (value, want) in [
            ("1", true),
            ("true", true),
            ("on", true),
            ("0", false),
            ("off", false),
        ] {
            // SAFETY: this is the only test touching the process environment, and it runs no threads of its own.
            unsafe { std::env::set_var("NATEMU_CHECK_REUSE", value) };
            assert_eq!(parse(&[]), want, "{value}");
        }
        unsafe { std::env::set_var("NATEMU_CHECK_REUSE", "sometimes") };
        assert!(super::parse_cli(base).is_err());
        unsafe { std::env::remove_var("NATEMU_CHECK_REUSE") };
        for (value, want) in [
            ("0", false),
            ("off", false),
            ("false", false),
            ("1", true),
            ("on", true),
        ] {
            // SAFETY: as above.
            unsafe { std::env::set_var("NATEMU_POOL", value) };
            assert_eq!(pool(&[]), want, "NATEMU_POOL={value}");
            assert!(pool(&["--pool"]), "a flag wins over NATEMU_POOL={value}");
            assert!(!pool(&["--pool=false"]));
        }
        unsafe { std::env::remove_var("NATEMU_POOL") };
    }
    #[test]
    fn transport_spec() {
        use natemu::linux::{Transport::*, Transports};
        let t = |s| super::transports(s);
        assert_eq!(t("mmio"), Ok(Transports::all(Mmio)));
        assert_eq!(t("pci"), Ok(Transports::all(Pci)));
        assert_eq!(
            t("disk=pci,net=mmio"),
            Ok(Transports {
                disk: Pci,
                net: Mmio,
                rng: Mmio,
                console: Mmio
            })
        );
        assert_eq!(
            t("console=pci"),
            Ok(Transports {
                console: Pci,
                ..Transports::all(Mmio)
            })
        );
        for bad in [
            "",
            "tcp",
            "disk=tcp",
            "gpu=pci",
            "disk=pci,disk=mmio",
            "disk=pci,net",
            "disk=pci,",
        ] {
            assert!(t(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn size() {
        for (text, bytes) in [
            ("2G", 2 << 30),
            ("2048M", 2 << 30),
            ("0x80000000", 2 << 30),
            ("4096", 4096),
            ("64k", 64 << 10),
        ] {
            assert_eq!(super::size(text), Ok(bytes), "{text}");
        }
        for text in ["", "G", "2T", "0x", "-1", "99999999999G"] {
            assert!(super::size(text).is_err(), "{text}");
        }
    }
}
