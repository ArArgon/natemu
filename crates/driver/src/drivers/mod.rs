//! The test driver's subcommands, over the natemu library. `run-program` builds a program description's fixture (tables,
//! bytes, initial state, test devices) and calls the emulator mechanisms (vm, memory, owners, execution) through their
//! ordinary operations; its test-only behaviour (tampers, the logging IDT, CPL switches, event sources) is its
//! `execution::Hook`, so no library module knows a test case. `boot-linux` runs a bzImage on the library's boot entry
//! ([`natemu::linux::Boot`]), with no fixture or hook.
use crate::*;

mod linux;
mod program;
mod test_devices;

// The test driver's command line (clap; `natemu-host RUNTIME.elf run-program --help`): the positional layout and option
// names the runners use; a repeated option's last value wins, as before.
#[derive(clap::Parser)]
#[command(name = "natemu-host", about = "natemu's test driver", disable_help_subcommand = true)]
struct Cli {
    /// The resident runtime ELF (`unused` with boot-linux --load-only)
    runtime: String,
    #[command(subcommand)]
    command: Command,
}

#[derive(clap::Subcommand)]
enum Command {
    #[command(args_override_self = true)]
    RunProgram(program::Args),
    #[command(args_override_self = true)]
    BootLinux(linux::Args),
}

/// Dispatches the host CLI (`natemu-host RUNTIME.elf run-program|boot-linux ...`) to its driver. A refused command line
/// exits 1 (`--help` 0), as any driver error.
pub(crate) fn run() -> Result<()> {
    let cli = match <Cli as clap::Parser>::try_parse() {
        Ok(cli) => cli,
        Err(e) if !e.use_stderr() => e.exit(),
        Err(e) => return Err(e.render().to_string().trim_end().into()),
    };
    match cli.command {
        Command::RunProgram(args) => program::run(&cli.runtime, args),
        Command::BootLinux(args) => linux::run(&cli.runtime, args),
    }
}

#[cfg(test)]
mod tests {
    use clap::{ArgMatches, CommandFactory as _};

    /// The runners' forms: RUNTIME before the subcommand, the block size limit after WORKDIR or among the options, a
    /// repeated option's last value, repeatable options, a command line starting with `-`.
    #[test]
    fn command_lines() {
        let parse = |args: &[&str]| -> Result<ArgMatches, clap::Error> {
            let m = super::Cli::command().try_get_matches_from([&["natemu-host", "r"], args].concat())?;
            Ok(m.subcommand().unwrap().1.clone())
        };
        let a = parse(&["boot-linux", "f", "k", "--ram", "0x4000000", "--ram", "0x8000000", "--save", "0", "1", "a", "--save", "0x10", "2", "b",
            "--cmdline", "-- sh", "--load-only"])
        .unwrap();
        assert_eq!(a.get_one::<u64>("ram"), Some(&0x8000000));
        assert_eq!(a.get_many::<String>("save").unwrap().count(), 6);
        assert_eq!((a.get_one::<String>("cmdline").unwrap().as_str(), a.get_flag("load_only")), ("-- sh", true));
        for (args, limit) in [(&["5", "--code-shape"][..], Some(&5)), (&["--code-shape", "5"], Some(&5)), (&["--code-shape"], None)] {
            let p = parse(&[&["run-program", "f", "p", "w"], args].concat()).unwrap();
            assert_eq!((p.get_one::<u32>("max_instructions"), p.get_flag("code_shape")), (limit, true));
        }
        let p = parse(&["run-program", "f", "p", "w", "--device-at", "fee00", "--device-at", "0x1000", "--tamper", "word", "--tamper", "site",
            "--kick-vcpu", "1", "--kick-vcpu", "0"])
        .unwrap();
        assert_eq!(p.get_many::<u64>("device_at").unwrap().copied().collect::<Vec<_>>(), [0xfee00, 0x1000]);
        assert_eq!(p.get_many::<usize>("kicked").unwrap().copied().collect::<Vec<_>>(), [1, 0]);
        assert_eq!(p.get_one::<String>("tamper").unwrap(), "site");
        assert!(parse(&["run-program", "f", "p", "w", "5", "6"]).is_err());
        assert!(parse(&["run-program", "f", "p", "w", "--kill-compiler", "1", "--stall-compiler", "1"]).is_err());
    }
}
