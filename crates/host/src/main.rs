//! `natemu`, the boot entry: boots a Linux bzImage on the library's [`natemu::linux::Boot`] with the persistent compile
//! cache from the environment ([`natemu::compile_cache::Config::from_env`]).
//!
//!   natemu RUNTIME.elf FEXC BZIMAGE [--initrd FILE] [--cmdline TEXT] [--ram BYTES] [--vcpus N] [--serial FILE]
//!
//! COM1's output goes to `--serial FILE` (default standard output). The stop of the vCPU that ended the run is written to
//! standard error. Exits 0 when the guest powered off, 2 at any other stop, 1 on a host error.
use natemu::execution::Stop;
use natemu::linux::Boot;

const USAGE: &str = "usage: natemu RUNTIME.elf FEXC BZIMAGE [--initrd FILE] [--cmdline TEXT] [--ram BYTES] [--vcpus N] [--serial FILE]";
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
        return Err(USAGE.into());
    };
    let mut boot = Boot::new(runtime, fexc, natemu::memory::read_input(image, MAX_INPUT)?);
    boot.compile_cache = natemu::compile_cache::Config::from_env();
    boot.serial = Some(Box::new(std::io::stdout()));
    let mut options = options.iter();
    while let Some(option) = options.next() {
        let value = options.next().ok_or(USAGE)?;
        match option.as_str() {
            "--initrd" => boot.initrd = Some(natemu::memory::read_input(value, MAX_INPUT)?),
            "--cmdline" => boot.cmdline = value.clone(),
            "--ram" => boot.ram = number(value)?,
            "--vcpus" => boot.vcpus = number(value)? as usize,
            "--serial" => boot.serial = Some(Box::new(std::fs::File::create(value).map_err(|e| format!("{value}: {e}"))?)),
            _ => return Err(USAGE.into()),
        }
    }
    // The stop of the vCPU whose end ended the run (any vCPU may power off), else the BSP's.
    let mut outcome = boot.run()?;
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
