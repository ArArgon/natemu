//! `natemu`, the boot entry: boots a Linux bzImage on the library's [`natemu::linux::Boot`] with the persistent compile
//! cache on by default ([`natemu::compile_cache::Config::new`]).
//!
//!   natemu --fexc FILE --kernel FILE [--runtime FILE] [--initrd FILE] [--cmdline TEXT] [--ram SIZE] [--vcpus N]
//!       [--serial FILE] [--serial-stdin] [--speculation-barriers] [--compile-cache DIR|off] [--compile-cache-mb N]
//!       [--perf-log FILE]
//!
//! Named arguments (clap; `natemu --help`). The last five fall back to `NATEMU_SERIAL_STDIN`,
//! `NATEMU_SPECULATION_BARRIERS`, `NATEMU_COMPILE_CACHE`, `NATEMU_COMPILE_CACHE_MB` and `NATEMU_PERF_LOG` (a flag wins
//! over its variable; a switch's variable takes 1/0, true/false, yes/no, on/off). `--runtime` defaults to the resident built beside this binary
//! (`<target>/aarch64-unknown-none/<profile>/natemu-runtime` for `<target>/<profile>/natemu`) when that file exists.
//! `--ram` takes bytes, 0x hex, or a K/M/G suffix (binary units: 2G = 2048M = 0x80000000). COM1's output goes to
//! `--serial FILE` (default standard output). The stop of the vCPU that ended the run is written to standard error.
//! Exits 0 when the guest powered off, 2 at any other stop (SIGINT or SIGTERM cancels the run: `Cancelled`; a second one
//! exits 130 at once), 1 on a host error (a failed serial write and a refused command line included; `--help` exits 0).
//! `--perf-log FILE` turns on the library's timing log (`natemu::perf`). `--serial-stdin` (off by default) feeds standard
//! input to COM1 (`natemu::linux::serial_stdin`): a terminal in raw mode, Ctrl-C to the guest, Ctrl-A x cancels the run.
//! `--speculation-barriers` turns on the resident's page-walk barriers (refused without FEAT_SB; with FEAT_CSV3 they
//! advertise RDCL_NO: `natemu::linux::Boot::speculation_barriers`).
use clap::Parser;
use natemu::execution::Stop;
use natemu::linux::Boot;
use std::path::PathBuf;

/// Largest image or initrd read (the boot protocol's 32-bit sizes).
const MAX_INPUT: usize = 1 << 32;

/// Boot a Linux bzImage (x86-64) under natemu.
#[derive(Parser)]
#[command(
    name = "natemu",
    after_help = "Exit status: 0 the guest powered off, 2 any other stop (reset, a typed stop, a watchdog, \
    SIGINT/SIGTERM), 1 a host error. A flag wins over its environment variable."
)]
struct Cli {
    /// The resident runtime ELF [default: the one built beside this binary]
    #[arg(long, value_name = "FILE")]
    runtime: Option<PathBuf>,
    /// The fexc compiler
    #[arg(long, value_name = "FILE")]
    fexc: PathBuf,
    /// The Linux bzImage
    #[arg(long, value_name = "FILE")]
    kernel: PathBuf,
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
    /// Write COM1's output to FILE [default: standard output]
    #[arg(long, value_name = "FILE")]
    serial: Option<PathBuf>,
    /// Feed standard input to COM1; Ctrl-A x cancels the run
    #[arg(long, env = "NATEMU_SERIAL_STDIN", value_parser = clap::builder::BoolishValueParser::new())]
    serial_stdin: bool,
    /// The resident's page-walk speculation barriers (needs FEAT_SB)
    #[arg(long, env = "NATEMU_SPECULATION_BARRIERS", value_parser = clap::builder::BoolishValueParser::new())]
    speculation_barriers: bool,
    /// Expose the virtio-MMIO entropy device
    #[arg(long)]
    rng: bool,
    /// Writable raw disk image (exclusive use; successful S5 includes full-sync)
    #[arg(long, value_name = "FILE")]
    disk: Option<PathBuf>,
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
    // Performance phase (perf plan §2): the library's timing log (`natemu::perf`), off unless set.
    if let Some(path) = cli.perf_log {
        natemu::perf::enable(path)?;
        natemu::perf::mark("main");
    }
    let runtime = cli
        .runtime
        .or_else(built_runtime)
        .ok_or("--runtime FILE: no resident built beside this binary")?;
    for (what, path) in [("--runtime", &runtime), ("--fexc", &cli.fexc)] {
        std::fs::metadata(path).map_err(|e| format!("{what} {}: {e}", path.display()))?;
    }
    let read = |path: &PathBuf| natemu::memory::read_input(&path.to_string_lossy(), MAX_INPUT);
    let mut boot = Boot::new(runtime, cli.fexc, read(&cli.kernel)?);
    boot.compile_cache = natemu::compile_cache::Config::new(
        cli.compile_cache.as_deref().map(|p| p.as_os_str()),
        cli.compile_cache_mb,
    )?;
    boot.speculation_barriers = cli.speculation_barriers;
    boot.rng = cli.rng;
    boot.disk = cli.disk;
    boot.initrd = cli.initrd.as_ref().map(read).transpose()?;
    boot.cmdline = cli.cmdline.unwrap_or_default();
    boot.ram = cli.ram.unwrap_or(boot.ram);
    boot.vcpus = cli.vcpus.unwrap_or(boot.vcpus);
    boot.serial = Some(match &cli.serial {
        Some(path) => Box::new(
            std::fs::File::create(path).map_err(|e| format!("--serial {}: {e}", path.display()))?,
        ),
        None => Box::new(std::io::stdout()),
    });
    // SIGINT/SIGTERM cancel the run (a second one exits at once).
    let prepared = boot.prepare()?;
    prepared.canceller().on_signals();
    if cli.serial_stdin {
        prepared.serial_input(natemu::linux::serial_stdin()); // raw mode only once the signals are handled
    }
    // The stop of the vCPU whose end ended the run (any vCPU may power off), else the BSP's.
    let mut outcome = prepared.run()?;
    if let Some(e) = prepared.serial_error() {
        return Err(format!("serial output: {e}"));
    }
    let first = if outcome.ended_first < outcome.each.len() {
        outcome.ended_first
    } else {
        0
    };
    Ok(outcome.each.swap_remove(first).unwrap().0)
}

fn main() {
    let cli = match Cli::try_parse() {
        Ok(cli) => cli,
        Err(e) if !e.use_stderr() => e.exit(), // --help: standard output, exit 0
        Err(e) => {
            let _ = e.print();
            std::process::exit(1); // a refused command line is a host error, not a stop (2)
        }
    };
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

#[cfg(test)]
mod tests {
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
