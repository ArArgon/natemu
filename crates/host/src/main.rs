//! `natemu`, the boot entry: boots a Linux bzImage on the library's [`natemu::linux::Boot`] with the persistent compile
//! cache from the environment ([`natemu::compile_cache::Config::from_env`]).
//!
//!   natemu RUNTIME.elf FEXC BZIMAGE [--initrd FILE] [--cmdline TEXT] [--ram BYTES] [--vcpus N] [--serial FILE]
//!       [--serial-stdin]
//!
//! COM1's output goes to `--serial FILE` (default standard output). The stop of the vCPU that ended the run is written to
//! standard error. Exits 0 when the guest powered off, 2 at any other stop (SIGINT or SIGTERM cancels the run: `Cancelled`;
//! a second one exits 130 at once), 1 on a host error (a failed serial write included). `NATEMU_PERF_LOG=FILE` turns on
//! the library's timing log (`natemu::perf`). `--serial-stdin` (or `NATEMU_SERIAL_STDIN=1`; off by default) feeds standard
//! input to COM1 (`natemu::linux::serial_stdin`): a terminal in raw mode, Ctrl-C to the guest, Ctrl-A x cancels the run.
use natemu::execution::Stop;
use natemu::linux::Boot;

const USAGE: &str = "usage: natemu RUNTIME.elf FEXC BZIMAGE [--initrd FILE] [--cmdline TEXT] [--ram BYTES] [--vcpus N] [--serial FILE] [--serial-stdin]";
/// Largest image or initrd read (the boot protocol's 32-bit sizes).
const MAX_INPUT: usize = 1 << 32;

fn number(text: &str) -> natemu::Result<u64> {
    match text.strip_prefix("0x") {
        Some(h) => u64::from_str_radix(h, 16),
        None => text.parse(),
    }
    .map_err(|_| format!("bad number {text}"))
}

fn boot(args: &[String]) -> natemu::Result<Stop> {
    let [_, runtime, fexc, image, options @ ..] = args else {
        return Err(format!("expected RUNTIME.elf FEXC BZIMAGE, got {} argument(s)\n{USAGE}", args.len().saturating_sub(1)));
    };
    // Performance phase (perf plan §2): the library's timing log (`natemu::perf`), off unless set.
    if let Some(path) = std::env::var_os("NATEMU_PERF_LOG") {
        natemu::perf::enable(path)?;
        natemu::perf::mark("main");
    }
    for (what, path) in [("RUNTIME.elf", runtime), ("FEXC", fexc)] {
        std::fs::metadata(path).map_err(|e| format!("{what} {path}: {e}"))?;
    }
    let mut boot = Boot::new(runtime, fexc, natemu::memory::read_input(image, MAX_INPUT)?);
    boot.compile_cache = natemu::compile_cache::Config::from_env()?;
    boot.serial = Some(Box::new(std::io::stdout()));
    boot.speculation_barriers = natemu::execution::env_speculation_barriers();
    let mut serial_stdin = natemu::linux::env_serial_stdin();
    let mut options = options.iter();
    while let Some(option) = options.next() {
        if option == "--serial-stdin" {
            serial_stdin = true;
            continue;
        }
        if !matches!(option.as_str(), "--initrd" | "--cmdline" | "--ram" | "--vcpus" | "--serial") {
            return Err(format!("unknown option {option}\n{USAGE}"));
        }
        let value = options.next().ok_or_else(|| format!("{option} needs a value\n{USAGE}"))?;
        match option.as_str() {
            "--initrd" => boot.initrd = Some(natemu::memory::read_input(value, MAX_INPUT)?),
            "--cmdline" => boot.cmdline = value.clone(),
            "--ram" => boot.ram = number(value)?,
            "--vcpus" => boot.vcpus = number(value)? as usize,
            _ => boot.serial = Some(Box::new(std::fs::File::create(value).map_err(|e| format!("{value}: {e}"))?)),
        }
    }
    // SIGINT/SIGTERM cancel the run (a second one exits at once).
    let prepared = boot.prepare()?;
    prepared.canceller().on_signals();
    if serial_stdin {
        prepared.serial_input(natemu::linux::serial_stdin()); // raw mode only once the signals are handled
    }
    // The stop of the vCPU whose end ended the run (any vCPU may power off), else the BSP's.
    let mut outcome = prepared.run()?;
    if let Some(e) = prepared.serial_error() {
        return Err(format!("serial output: {e}"));
    }
    let first = if outcome.ended_first < outcome.each.len() { outcome.ended_first } else { 0 };
    Ok(outcome.each.swap_remove(first).unwrap().0)
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    match boot(&args) {
        Ok(stop) => {
            eprintln!("natemu: {stop:?}");
            std::process::exit(if matches!(stop, Stop::Power(natemu::pm::POWEROFF)) { 0 } else { 2 });
        }
        Err(error) => {
            eprintln!("natemu: {error}");
            std::process::exit(1);
        }
    }
}
