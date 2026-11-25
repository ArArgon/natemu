use super::*;
use crate::{CLI_ENV, parse_cli};
use natemu::linux::Transport::{Mmio, Pci};
use std::sync::atomic::{AtomicUsize, Ordering};

fn lock() -> std::sync::MutexGuard<'static, ()> {
    CLI_ENV.lock().unwrap_or_else(|e| e.into_inner())
}

/// A fresh temporary folder.
fn dir() -> PathBuf {
    static N: AtomicUsize = AtomicUsize::new(0);
    let d = std::env::temp_dir().join(format!(
        "natemu-cfg-{}-{}",
        std::process::id(),
        N.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// Loads the configuration of `natemu ARGS`.
fn load_args(args: &[&str]) -> Res<Config> {
    let cli = parse_cli(std::iter::once("natemu").chain(args.iter().copied()))
        .map_err(|e| e.to_string())?;
    load(&cli)
}
const BASE: [&str; 4] = ["--fexc", "f", "--kernel", "k"];
fn load_old(extra: &[&str]) -> Res<Config> {
    load_args(&[&BASE[..], extra].concat())
}
/// Writes `toml` as vm.toml in a new folder and loads `natemu --config vm.toml ARGS`.
fn load_file(toml: &str, args: &[&str]) -> (PathBuf, Res<Config>) {
    let d = dir();
    let f = d.join("vm.toml");
    std::fs::write(&f, toml).unwrap();
    let mut a = vec!["--config", f.to_str().unwrap()];
    a.extend(args);
    (d, load_args(&a))
}
const MIN: &str = "[engine]\nfexc = \"fexc\"\n[boot.kernel]\nimage = \"vmlinuz\"\n";
fn err(r: Res<Config>) -> String {
    r.expect_err("expected a refusal")
}

// ---- equivalence with the old command line (33a99fb8) ------------------------------------------------------------------

/// What the library sees, for comparison.
#[derive(Debug, PartialEq)]
struct Fields {
    runtime: PathBuf,
    fexc: PathBuf,
    kernel: Vec<u8>,
    initrd: Option<Vec<u8>>,
    cmdline: String,
    ram: u64,
    vcpus: usize,
    cache: (Option<PathBuf>, u64),
    speculation_barriers: bool,
    accurate_fp: bool,
    page_currency: u64,
    writable_on_read: bool,
    rng: bool,
    disks: Vec<natemu::linux::DiskSpec>,
    net: bool,
    forwards: Vec<(bool, u16, u16)>,
    host_loopback: bool,
    pool: bool,
    check_reuse: bool,
    transports: Transports,
    perf_log: Option<PathBuf>,
    serial: Option<PathBuf>,
    serial_stdin: bool,
}
fn fields(
    b: &Boot,
    perf_log: Option<PathBuf>,
    serial: Option<PathBuf>,
    serial_stdin: bool,
) -> Fields {
    Fields {
        runtime: b.runtime.clone(),
        fexc: b.fexc.clone(),
        kernel: b.kernel.clone(),
        initrd: b.initrd.clone(),
        cmdline: b.cmdline.clone(),
        ram: b.ram,
        vcpus: b.vcpus,
        cache: (b.compile_cache.dir.clone(), b.compile_cache.cap),
        speculation_barriers: b.speculation_barriers,
        accurate_fp: b.accurate_fp,
        page_currency: b.page_currency,
        writable_on_read: b.writable_on_read,
        rng: b.rng,
        disks: b.disks.clone(),
        net: b.net,
        forwards: b
            .net_options
            .forwards
            .iter()
            .map(|f| {
                (
                    f.protocol == natemu::virtio::ForwardProtocol::Udp,
                    f.host_port.get(),
                    f.guest_port.get(),
                )
            })
            .collect(),
        host_loopback: b.net_options.host_loopback,
        pool: b.pool,
        check_reuse: b.check_reuse,
        transports: b.transports,
        perf_log,
        serial,
        serial_stdin,
    }
}
/// The expectation: the field mapping of `boot()` in crates/cli/src/main.rs at commit 33a99fb8, copied statement for
/// statement (the same `Boot::new` and the old defaults) onto the clap struct, which still has the old flag fields.
fn legacy(cli: &Cli) -> Fields {
    let net_options = SlirpOptions {
        forwards: cli.net_forward.clone(),
        host_loopback: cli.net_host_loopback,
    };
    let mut boot = Boot::new("RT", cli.fexc.clone().unwrap(), vec![7]);
    boot.compile_cache = natemu::compile_cache::Config::new(
        cli.compile_cache.as_deref().map(|p| p.as_os_str()),
        cli.compile_cache_mb,
    )
    .unwrap();
    boot.speculation_barriers = cli.speculation_barriers;
    boot.accurate_fp = cli.fp;
    boot.page_currency = cli.page_currency;
    boot.writable_on_read = cli.writable_on_read;
    boot.rng = cli.rng;
    boot.disks = cli
        .disk
        .iter()
        .map(|path| natemu::linux::DiskSpec {
            id: "disk0".into(),
            path: path.clone(),
            transport: cli.virtio_transport.disk,
            readonly: false,
            backing: None,
            snapshot: false,
        })
        .collect();
    boot.net = cli.net;
    boot.net_options = net_options;
    boot.pool = cli.pool;
    boot.check_reuse = cli.check_reuse;
    boot.transports = cli.virtio_transport;
    boot.initrd = cli.initrd.as_ref().map(|_| vec![8]);
    boot.cmdline = cli.cmdline.clone().unwrap_or_default();
    boot.ram = cli.ram.unwrap_or(boot.ram);
    boot.vcpus = cli.vcpus.unwrap_or(boot.vcpus);
    fields(
        &boot,
        cli.perf_log.clone(),
        cli.serial.clone(),
        cli.serial_stdin,
    )
}
fn new_way(cli: &Cli) -> Fields {
    let cfg = load(cli).unwrap();
    let boot = to_boot(
        &cfg,
        "RT".into(),
        vec![7],
        cfg.initrd.as_ref().map(|_| vec![8]),
    )
    .unwrap();
    fields(
        &boot,
        cfg.perf_log.clone(),
        cfg.serial_output.clone(),
        cfg.serial_stdin,
    )
}

#[test]
fn old_command_lines_build_the_same_boot() {
    let _env = lock();
    let every_flag = "--runtime r --initrd i --cmdline console=ttyS0 --ram 4G --vcpus 4 --serial out.txt --serial-stdin \
        --speculation-barriers --fp accurate --page-currency off --writable-on-read off --rng --disk d.raw --net \
        --net-forward 2222:22 --net-forward udp:19000:9000 --net-host-loopback --pool=false --check-reuse \
        --virtio-transport disk=pci,net=mmio --compile-cache /c --compile-cache-mb 64 --perf-log p.log";
    let lines: &[&str] = &[
        "",
        every_flag,
        "--cmdline 3",
        "--cmdline -x",
        "--ram 0x80000000 --vcpus 2",
        "--ram 2048M",
        "--ram 64k",
        "--pool",
        "--pool=true",
        "--virtio-transport pci",
        "--virtio-transport mmio",
        "--virtio-transport disk=pci",
        "--virtio-transport net=pci --net --rng --disk d",
        "--virtio-transport rng=pci,disk=mmio --rng",
        "--compile-cache off",
        "--compile-cache-mb 1",
        "--fp fast --page-currency check --writable-on-read on",
        "--net --net-forward tcp:1:65535",
        "--net --net-host-loopback",
        "--disk /abs/d.raw",
        "--serial-stdin --serial s",
        "--rng",
    ];
    for line in lines {
        let args: Vec<&str> = BASE
            .iter()
            .copied()
            .chain(line.split_whitespace())
            .collect();
        let cli = parse_cli(std::iter::once("natemu").chain(args.iter().copied())).unwrap();
        assert_eq!(new_way(&cli), legacy(&cli), "{line}");
    }
    // The environment variables: each, on its own and beaten by its flag.
    let vars = [
        ("NATEMU_SERIAL_STDIN", "yes"),
        ("NATEMU_SPECULATION_BARRIERS", "on"),
        ("NATEMU_FP", "accurate"),
        ("NATEMU_PAGE_CURRENCY", "off"),
        ("NATEMU_WRITABLE_ON_READ", "off"),
        ("NATEMU_POOL", "0"),
        ("NATEMU_CHECK_REUSE", "true"),
        ("NATEMU_COMPILE_CACHE", "/envcache"),
        ("NATEMU_COMPILE_CACHE_MB", "9"),
        ("NATEMU_PERF_LOG", "env.log"),
    ];
    for (name, value) in vars {
        // SAFETY: CLI_ENV is held, and every test that reads the environment holds it.
        unsafe { std::env::set_var(name, value) };
        for line in [
            "",
            "--fp fast --page-currency on --writable-on-read on --pool --compile-cache off --compile-cache-mb 1 --perf-log f.log",
        ] {
            let args: Vec<&str> = BASE
                .iter()
                .copied()
                .chain(line.split_whitespace())
                .collect();
            let cli = parse_cli(std::iter::once("natemu").chain(args.iter().copied())).unwrap();
            assert_eq!(new_way(&cli), legacy(&cli), "{name} {line}");
        }
        unsafe { std::env::remove_var(name) };
    }
}

#[test]
fn old_command_line_without_a_kernel_or_fexc_fails_clearly() {
    let _env = lock();
    assert!(err(load_args(&["--fexc", "f"])).contains("no boot method"));
    assert!(err(load_args(&["--kernel", "k"])).contains("no fexc"));
    assert!(err(load_args(&["--fexc", "f", "--initrd", "i"])).contains("no boot method"));
}

// ---- old spellings ------------------------------------------------------------------------------------------------------

#[test]
fn boolean_words_in_flags_env_and_set() {
    let _env = lock();
    for (word, want) in [
        ("1", true),
        ("yes", true),
        ("on", true),
        ("true", true),
        ("0", false),
        ("no", false),
        ("off", false),
        ("false", false),
    ] {
        unsafe { std::env::set_var("NATEMU_SERIAL_STDIN", word) };
        assert_eq!(load_old(&[]).unwrap().serial_stdin, want, "env {word}");
        unsafe { std::env::remove_var("NATEMU_SERIAL_STDIN") };
        let set = format!("serial.stdin={word}");
        assert_eq!(
            load_old(&["--set", &set]).unwrap().serial_stdin,
            want,
            "--set {word}"
        );
        let set = format!("debug.pool={word}");
        assert_eq!(
            load_old(&["--set", &set]).unwrap().pool,
            want,
            "--set pool {word}"
        );
    }
    assert!(
        err(load_old(&["--set", "serial.stdin=maybe"])).contains("--set serial.stdin: bad boolean")
    );
}

#[test]
fn numeric_cmdline_stays_a_string() {
    let _env = lock();
    assert_eq!(load_old(&["--cmdline", "3"]).unwrap().cmdline, "3");
    assert_eq!(
        load_old(&["--set", "boot.kernel.cmdline=3"])
            .unwrap()
            .cmdline,
        "3"
    );
    let (_, c) = load_file(&format!("{MIN}cmdline = \"3\"\n"), &[]);
    assert_eq!(c.unwrap().cmdline, "3");
}

#[test]
fn paths_resolve_against_the_file_or_the_working_directory() {
    let _env = lock();
    let (d, c) = load_file(
        "[engine]\nfexc = \"../build/fexc\"\ncompile_cache = \"off\"\n[boot.kernel]\nimage = \"vmlinuz\"\ninitrd = \"sub/initrd\"\n[serial]\noutput = \"s.log\"\n[debug]\nperf_log = \"/abs/p.log\"\n",
        &[],
    );
    let c = c.unwrap();
    assert_eq!(c.kernel, Some(d.join("vmlinuz")));
    assert_eq!(c.initrd, Some(d.join("sub/initrd")));
    assert_eq!(c.fexc, Some(d.join("../build/fexc")));
    assert_eq!(c.serial_output, Some(d.join("s.log")));
    assert_eq!(c.perf_log, Some(PathBuf::from("/abs/p.log")));
    assert_eq!(c.compile_cache, Some(PathBuf::from("off")));
    let (d, c) = load_file(
        &format!(
            "{MIN}[[disk]]\nid = \"root\"\npath = \"runs/top.qcow2\"\nbacking = \"images/base.raw\"\n"
        ),
        &[],
    );
    let disk = &c.unwrap().disks[0];
    assert_eq!(disk.path, d.join("runs/top.qcow2"));
    assert_eq!(disk.backing, Some(d.join("images/base.raw")));
    // Flag and --set values are kept as given (the working directory's).
    let (_, c) = load_file(
        MIN,
        &[
            "--kernel",
            "rel/k",
            "--set",
            "boot.kernel.initrd=rel/i",
            "--set",
            "engine.fexc=./f",
        ],
    );
    let c = c.unwrap();
    assert_eq!(c.kernel, Some(PathBuf::from("rel/k")));
    assert_eq!(c.initrd, Some(PathBuf::from("rel/i")));
    assert_eq!(c.fexc, Some(PathBuf::from("./f")));
}

#[test]
fn transports_default_and_partial_specs() {
    let _env = lock();
    assert_eq!(
        load_old(&[]).unwrap().transports,
        Transports::all(Mmio),
        "no file: mmio"
    );
    let (_, c) = load_file(MIN, &[]);
    assert_eq!(
        c.unwrap().transports,
        Transports::all(Pci),
        "with a file: pci"
    );
    // A partial old spec: unnamed kinds are mmio, even with a file; net= is the nic.
    assert_eq!(
        load_old(&["--virtio-transport", "disk=pci"])
            .unwrap()
            .transports,
        Transports {
            disk: Pci,
            net: Mmio,
            rng: Mmio,
            console: Mmio
        }
    );
    let (_, c) = load_file(MIN, &["--virtio-transport", "net=pci"]);
    assert_eq!(
        c.unwrap().transports,
        Transports {
            disk: Mmio,
            net: Pci,
            rng: Mmio,
            console: Mmio
        }
    );
    // The file's table: nic is the network device; unnamed keys keep the file default (pci).
    let (_, c) = load_file(
        &format!("[machine]\ntransport = {{ nic = \"mmio\" }}\n{MIN}"),
        &[],
    );
    assert_eq!(
        c.unwrap().transports,
        Transports {
            disk: Pci,
            net: Mmio,
            rng: Pci,
            console: Pci
        }
    );
    let (_, c) = load_file(MIN, &["--set", "machine.transport.rng=mmio"]);
    assert_eq!(c.unwrap().transports.rng, Mmio);
    assert!(
        err(load_file(
            &format!("[machine]\ntransport = {{ net = \"pci\" }}\n{MIN}"),
            &[]
        )
        .1)
        .contains("net")
    );
}

/// `[[console]]` takes exactly one of `output` and `socket` (a path from the file's folder); its id joins the others.
#[test]
fn console_needs_exactly_one_of_output_socket() {
    let _env = lock();
    let (d, c) = load_file(
        &format!("{MIN}[[console]]\nid = \"hvc\"\noutput = \"out/c.log\"\n"),
        &[],
    );
    let c = c.unwrap();
    assert_eq!(
        c.consoles,
        [Console {
            id: "hvc".into(),
            backend: ConsoleBackend::Output(d.join("out/c.log"))
        }]
    );
    let (d, c) = load_file(
        &format!("{MIN}[[console]]\nid = \"tty\"\nsocket = \"c.sock\"\n"),
        &[],
    );
    assert_eq!(
        c.unwrap().consoles[0].backend,
        ConsoleBackend::Socket(d.join("c.sock"))
    );
    for (keys, found) in [("", "none"), ("output = \"a\"\nsocket = \"b\"\n", "both")] {
        let e = err(load_file(&format!("{MIN}[[console]]\nid = \"hvc\"\n{keys}"), &[]).1);
        assert!(
            e.ends_with(&format!(
                "vm.toml: console[0]: needs exactly one of output, socket; found {found}"
            )),
            "{e}"
        );
    }
    let e = err(load_file(
        &format!("{MIN}[[console]]\nid = \"hvc\"\nfile = \"a\"\n"),
        &[],
    )
    .1);
    assert!(e.contains("console[0].file: unknown field"), "{e}");
    let two = format!("{MIN}[[rng]]\nid = \"x\"\n[[console]]\nid = \"x\"\noutput = \"a\"\n");
    assert!(
        err(load_file(&two, &[]).1).contains("device id \"x\" is used twice (rng 1 and console 1)")
    );
    let bad = format!("{MIN}[[console]]\nid = \"A\"\noutput = \"a\"\n");
    assert!(err(load_file(&bad, &[]).1).contains("(console 1)"));
}

/// `machine.transport.console`: in the file's table, by `--set`, counted with the other transport keys, and given to
/// each console the library sees.
#[test]
fn console_transport_key() {
    let _env = lock();
    let console = format!("{MIN}[[console]]\nid = \"hvc\"\noutput = \"c.log\"\n");
    let (_, c) = load_file(&console, &[]);
    assert_eq!(c.unwrap().transports.console, Pci, "with a file: pci");
    assert_eq!(
        load_old(&[]).unwrap().transports.console,
        Mmio,
        "no file: mmio"
    );
    let (_, c) = load_file(
        &format!("[machine]\ntransport = {{ console = \"mmio\" }}\n{console}"),
        &[],
    );
    let c = c.unwrap();
    assert_eq!(
        c.transports,
        Transports {
            console: Mmio,
            ..Transports::all(Pci)
        },
        "the others keep the file default"
    );
    let boot = to_boot(&c, "rt".into(), vec![], None).unwrap();
    assert_eq!(
        boot.consoles,
        [natemu::linux::ConsoleSpec {
            id: "hvc".into(),
            backend: c.consoles[0].backend.clone(),
            transport: Mmio
        }]
    );
    let (_, c) = load_file(&console, &["--set", "machine.transport.console=mmio"]);
    assert_eq!(c.unwrap().transports.console, Mmio);
    let (_, c) = load_file(&console, &["--set", "machine.transport=mmio"]);
    assert_eq!(c.unwrap().transports.console, Mmio, "the whole transport");
    let e = err(load_file(
        &console,
        &[
            "--set",
            "machine.transport=pci",
            "--set",
            "machine.transport.console=mmio",
        ],
    )
    .1);
    assert!(e.contains("machine.transport.console: set twice"), "{e}");
    let e = err(load_file(
        &format!("[machine]\ntransport = {{ console = \"usb\" }}\n{MIN}"),
        &[],
    )
    .1);
    assert!(
        e.contains("machine.transport.console: bad transport \"usb\": mmio or pci"),
        "{e}"
    );
    let e = err(load_old(&["--set", "machine.transport.console=usb"]));
    assert!(
        e.contains("--set machine.transport.console: bad transport"),
        "{e}"
    );
}

/// `--set machine.transport=...` names the console as the file's table and `machine.transport.console` do.
#[test]
fn console_in_the_transport_spec() {
    let _env = lock();
    let console = format!("{MIN}[[console]]\nid = \"hvc\"\noutput = \"c.log\"\n");
    let (_, c) = load_file(&console, &["--set", "machine.transport=nic=pci,console=mmio"]);
    assert_eq!(
        c.unwrap().transports,
        Transports {
            net: Pci,
            ..Transports::all(Mmio)
        },
        "unnamed devices take mmio"
    );
    let c = load_old(&["--set", "machine.transport=console=pci"]).unwrap();
    assert_eq!(c.transports.console, Pci);
    let e = err(load_old(&["--set", "machine.transport=gpu=pci"]));
    assert!(e.contains("disk, net, rng or console"), "{e}");
}

#[test]
fn ram_forms_in_the_file() {
    let _env = lock();
    let (_, c) = load_file(&format!("[machine]\nram = \"4G\"\nvcpus = 3\n{MIN}"), &[]);
    let c = c.unwrap();
    assert_eq!((c.ram, c.vcpus), (Some(4 << 30), Some(3)));
    let (_, c) = load_file(
        &format!("[machine]\nram = 4096\n{MIN}"),
        &["--ram", "0x2000"],
    );
    assert_eq!(c.unwrap().ram, Some(0x2000), "a flag beats the file");
    assert!(
        err(load_file(&format!("[machine]\nram = \"2T\"\n{MIN}"), &[]).1)
            .contains("machine.ram: bad size")
    );
}

// ---- the layers ---------------------------------------------------------------------------------------------------------

#[test]
fn layers_file_then_environment_then_flag_then_set() {
    let _env = lock();
    let file = "[engine]\nfexc = \"fexc\"\nfp = \"accurate\"\n[boot.kernel]\nimage = \"vmlinuz\"\n";
    assert!(load_file(file, &[]).1.unwrap().fp, "file");
    unsafe { std::env::set_var("NATEMU_FP", "fast") };
    let cli = |args: &[&str]| {
        let d = dir();
        std::fs::write(d.join("vm.toml"), file).unwrap();
        let mut a = vec![
            "--config".to_string(),
            d.join("vm.toml").display().to_string(),
        ];
        a.extend(args.iter().map(|s| s.to_string()));
        parse_cli(std::iter::once("natemu".to_string()).chain(a)).unwrap()
    };
    assert!(
        !load(&cli(&[])).unwrap().fp,
        "the environment beats the file"
    );
    assert!(
        load(&cli(&["--fp", "accurate"])).unwrap().fp,
        "the flag beats the environment"
    );
    assert!(
        !load(&cli(&["--set", "engine.fp=fast"])).unwrap().fp,
        "--set beats the file"
    );
    assert_eq!(
        deprecations(&cli(&[])),
        ["natemu: NATEMU_FP is deprecated; use [engine] fp or --set engine.fp=..."],
        "one warning, only for a value from the environment"
    );
    assert!(
        deprecations(&cli(&["--fp", "fast"])).is_empty(),
        "the flag was given, so the value is not from the environment"
    );
    unsafe { std::env::remove_var("NATEMU_FP") };
    assert!(deprecations(&cli(&[])).is_empty());
}

#[test]
fn every_env_variable_warns_once() {
    let _env = lock();
    let vars = [
        "NATEMU_SERIAL_STDIN",
        "NATEMU_SPECULATION_BARRIERS",
        "NATEMU_FP",
        "NATEMU_PAGE_CURRENCY",
        "NATEMU_WRITABLE_ON_READ",
        "NATEMU_POOL",
        "NATEMU_CHECK_REUSE",
        "NATEMU_COMPILE_CACHE",
        "NATEMU_COMPILE_CACHE_MB",
        "NATEMU_PERF_LOG",
    ];
    let value = |v: &str| match v {
        "NATEMU_FP" => "fast",
        "NATEMU_PAGE_CURRENCY" | "NATEMU_WRITABLE_ON_READ" => "on",
        "NATEMU_COMPILE_CACHE_MB" => "5",
        "NATEMU_COMPILE_CACHE" | "NATEMU_PERF_LOG" => "x",
        _ => "1",
    };
    for v in vars {
        unsafe { std::env::set_var(v, value(v)) };
    }
    let cli = parse_cli(["natemu", "--fexc", "f", "--kernel", "k"]).unwrap();
    let w = deprecations(&cli);
    for v in vars {
        unsafe { std::env::remove_var(v) };
    }
    assert_eq!(w.len(), 10);
    for v in vars {
        assert_eq!(
            w.iter()
                .filter(|l| l.contains(&format!("{v} is deprecated")))
                .count(),
            1,
            "{v}"
        );
    }
}

#[test]
fn set_errors() {
    let _env = lock();
    assert!(
        err(load_old(&["--set", "machine.ramm=1"]))
            .contains("--set machine.ramm: unknown key; expected one of machine.ram")
    );
    assert!(err(load_old(&["--set", "ram"])).contains("expected key=value"));
    assert!(
        err(load_old(&[
            "--set",
            "engine.fp=fast",
            "--set",
            "engine.fp=accurate"
        ]))
        .contains("set twice")
    );
    assert!(
        err(load_old(&["--fp", "fast", "--set", "engine.fp=accurate"]))
            .contains("--set engine.fp: set twice")
    );
    assert!(
        err(load_old(&[
            "--virtio-transport",
            "pci",
            "--set",
            "machine.transport.disk=mmio"
        ]))
        .contains("set twice")
    );
    assert!(err(load_old(&["--set", "machine.ram=2T"])).contains("--set machine.ram: bad size"));
    assert!(err(load_old(&["--set", "engine.fp=medium"])).contains("fast or accurate"));
    assert!(
        err(load_old(&["--set", "debug.page_currency=check-halt"])).contains("on, off or check")
    );
}

#[test]
fn set_edits_devices_but_cannot_create_them() {
    let _env = lock();
    let file =
        format!("{MIN}[[disk]]\nid = \"root\"\npath = \"root.raw\"\n[[nic]]\nid = \"net0\"\n");
    let (d, c) = load_file(
        &file,
        &[
            "--set",
            "disk.root.readonly=yes",
            "--set",
            "disk.root.path=other.raw",
            "--set",
            "disk.root.backing=rel/base.raw",
            "--set",
            "nic.net0.forward=tcp:2222:22,udp:5:6",
        ],
    );
    let c = c.unwrap();
    assert!(c.disks[0].readonly);
    assert_eq!(c.disks[0].path, PathBuf::from("other.raw"));
    assert_eq!(c.disks[0].backing, Some(PathBuf::from("rel/base.raw")));
    assert_eq!(c.nics[0].forward.len(), 2);
    let _ = d;
    let e = err(load_file(&file, &["--set", "disk.boot.path=x"]).1);
    assert!(
        e.contains("--set disk.boot.path: no disk with id \"boot\""),
        "{e}"
    );
    assert!(err(load_old(&["--set", "disk.disk0.path=x"])).contains("no disk with id"));
    assert!(err(load_file(&file, &["--set", "disk.root.fromat=x"]).1).contains("unknown key"));
}

#[test]
fn device_flags_are_refused_when_the_file_lists_that_kind() {
    let _env = lock();
    let file = format!(
        "{MIN}[[disk]]\nid = \"root\"\npath = \"root.raw\"\n[[nic]]\nid = \"n\"\n[[rng]]\nid = \"r\"\n"
    );
    for flag in [["--disk", "x"], ["--net", ""], ["--rng", ""]] {
        let args: Vec<&str> = flag.iter().copied().filter(|a| !a.is_empty()).collect();
        let e = err(load_file(&file, &args).1);
        assert!(
            e.contains(&format!("{}: the config file lists", flag[0])),
            "{e}"
        );
    }
    // Kinds the file does not list may still come from flags.
    let (_, c) = load_file(
        &format!("{MIN}[[disk]]\nid = \"root\"\npath = \"root.raw\"\n"),
        &["--net", "--net-forward", "1:2", "--rng"],
    );
    let c = c.unwrap();
    assert_eq!(
        (
            c.nics.len(),
            c.rngs.len(),
            c.nics[0].id.as_str(),
            c.rngs[0].id.as_str()
        ),
        (1, 1, "net0", "rng0")
    );
    let c = load_old(&["--disk", "d", "--net", "--rng"]).unwrap();
    assert_eq!(c.disks[0].id, "disk0");
}

// ---- the file's validation ----------------------------------------------------------------------------------------------

#[test]
fn unknown_keys_name_the_file_and_the_key_path() {
    let _env = lock();
    let file = format!(
        "{MIN}[[disk]]\nid = \"a\"\npath = \"a\"\n[[disk]]\nid = \"b\"\npath = \"b\"\nfromat = \"raw\"\n"
    );
    let e = err(load_file(&file, &[]).1);
    assert!(
        e.contains("vm.toml: ")
            && e.contains("fromat")
            && e.contains("expected one of")
            && !e.contains('\n'),
        "{e}"
    );
    assert!(e.contains("disk[1]"), "{e}");
    for (toml, key) in [
        ("[machine]\nramm = 1\n", "ramm"),
        ("[bogus]\n", "bogus"),
        ("[serial]\nout = 1\n", "out"),
        ("[debug]\nx = 1\n", "x"),
    ] {
        let e = err(load_file(&format!("{MIN}{toml}"), &[]).1);
        assert!(
            e.contains("vm.toml") && e.contains(key) && !e.contains('\n'),
            "{e}"
        );
    }
    // A field of the other boot method is an unknown key of this one.
    let e = err(load_file(
        "[engine]\nfexc = \"f\"\n[boot.kernel]\nimage = \"k\"\nvariables = \"x\"\n",
        &[],
    )
    .1);
    assert!(e.contains("variables"), "{e}");
    let e = err(load_file(
        "[engine]\nfexc = \"f\"\n[boot.kernel]\ninitrd = \"x\"\n",
        &[],
    )
    .1);
    assert!(e.contains("image"), "{e}");
}

#[test]
fn boot_method_rules() {
    let _env = lock();
    let eng = "[engine]\nfexc = \"f\"\n";
    let e = err(load_file(&format!("{eng}[boot]\n"), &[]).1);
    assert!(
        e.contains(
            "[boot] needs exactly one boot method, [boot.kernel], [boot.firmware] or [boot.uefi]; found none"
        ),
        "{e}"
    );
    let e = err(load_file(
        &format!("{eng}[boot.kernel]\nimage = \"k\"\n[boot.uefi]\n"),
        &[],
    )
    .1);
    assert!(e.contains("found more"), "{e}");
    let e = err(load_file(
        &format!("{eng}[boot.kernel]\nimage = \"k\"\n[boot.firmware]\nimage = \"f\"\n"),
        &[],
    )
    .1);
    assert!(e.contains("found more"), "{e}");
    let e = err(load_file(&format!("{eng}[boot.uefi]\nfirmware = \"x\"\n"), &[]).1);
    assert!(
        e.contains("UEFI boot is planned (M3) and not built yet"),
        "{e}"
    );
    // No [boot] at all and no --kernel: no boot method; --kernel supplies one.
    assert!(err(load_file(eng, &[]).1).contains("no boot method"));
    assert_eq!(
        load_file(eng, &["--kernel", "k"]).1.unwrap().kernel,
        Some(PathBuf::from("k"))
    );
}

/// M2 Part 2, the firmware boot mode: `--firmware` or `[boot.firmware]` (paths from the file's folder), its start, and the
/// refusals (two boot methods, an initrd or command line, devices, an unknown start, a table without its image).
#[test]
fn firmware_boot() {
    use natemu::boot::firmware::Start;
    let _env = lock();
    let eng = "[engine]\nfexc = \"f\"\n";
    let c = load_args(&["--fexc", "f", "--firmware", "fw.bin"]).unwrap();
    assert_eq!((c.firmware, c.kernel, c.firmware_start), (Some(PathBuf::from("fw.bin")), None, Start::Reset));
    let (d, c) = load_file(&format!("{eng}[boot.firmware]\nimage = \"fw.bin\"\nstart = \"flat32\"\n"), &[]);
    let c = c.unwrap();
    assert_eq!((c.firmware, c.firmware_start), (Some(d.join("fw.bin")), Start::Flat32));
    let c = load_args(&["--fexc", "f", "--firmware", "fw.bin", "--set", "boot.firmware.start=flat32"]).unwrap();
    assert_eq!(c.firmware_start, Start::Flat32);
    // The file's table takes its image from --firmware; without either it is refused.
    let c = load_file(&format!("{eng}[boot.firmware]\n"), &["--firmware", "x"]).1.unwrap();
    assert_eq!(c.firmware, Some(PathBuf::from("x")));
    assert!(err(load_file(&format!("{eng}[boot.firmware]\n"), &[]).1).contains("[boot.firmware] needs image"));
    // Refused: clap's conflicts, two methods through the file, extras, devices, a bad start.
    assert!(err(load_args(&["--fexc", "f", "--firmware", "a", "--kernel", "k"])).contains("cannot be used with"));
    assert!(err(load_args(&["--fexc", "f", "--firmware", "a", "--cmdline", "x"])).contains("cannot be used with"));
    let e = err(load_file(MIN, &["--firmware", "a"]).1);
    assert!(e.contains("two boot methods"), "{e}");
    let e = err(load_args(&["--fexc", "f", "--firmware", "a", "--set", "boot.kernel.cmdline=x"]));
    assert!(e.contains("no initrd or kernel command line"), "{e}");
    let e = err(load_file(&format!("{eng}[boot.firmware]\nimage = \"a\"\n[[rng]]\nid = \"r\"\n"), &[]).1);
    assert!(e.contains("no virtio devices"), "{e}");
    let e = err(load_file(&format!("{eng}[boot.firmware]\nimage = \"a\"\nstart = \"main32\"\n"), &[]).1);
    assert!(e.contains("reset or flat32"), "{e}");
    assert!(err(load_file(&format!("{eng}[boot.firmware]\nimage = \"a\"\nentry = 1\n"), &[]).1).contains("unknown field"));
    let e = err(load_args(&["--fexc", "f", "--kernel", "k", "--set", "boot.firmware.start=flat32"]));
    assert!(e.contains("two boot methods") || e.contains("without a firmware image"), "{e}");
    assert!(err(load_old(&["--set", "boot.firmware.start=flat32"])).contains("without a firmware image"));
}

#[test]
fn device_id_rules() {
    let _env = lock();
    let disk = |id: &str, path: &str| format!("[[disk]]\nid = \"{id}\"\npath = \"{path}\"\n");
    let e = err(load_file(
        &format!(
            "{MIN}{}{}{}",
            disk("root", "a"),
            disk("b", "b"),
            disk("root", "c")
        ),
        &[],
    )
    .1);
    assert!(
        e.contains("device id \"root\" is used twice (disk 1 and disk 3)"),
        "{e}"
    );
    let e = err(load_file(
        &format!("{MIN}{}[[nic]]\nid = \"root\"\n", disk("root", "a")),
        &[],
    )
    .1);
    assert!(e.contains("used twice (disk 1 and nic 1)"), "{e}");
    for bad in ["Root", "a b", "a.b", ""] {
        let e = err(load_file(&format!("{MIN}{}", disk(bad, "a")), &[]).1);
        assert!(e.contains("must match [a-z0-9_-]+"), "{bad}: {e}");
    }
    assert!(
        load_file(&format!("{MIN}{}", disk("a_b-9", "a")), &[])
            .1
            .is_ok()
    );
    assert!(
        err(load_file(
            &format!("{MIN}[[nic]]\nid = \"a\"\n[[nic]]\nid = \"b\"\n"),
            &[]
        )
        .1)
        .contains("at most one [[nic]]")
    );
    assert!(
        err(load_file(
            &format!("{MIN}[[rng]]\nid = \"a\"\n[[rng]]\nid = \"b\"\n"),
            &[]
        )
        .1)
        .contains("at most one [[rng]]")
    );
    // Required fields.
    assert!(err(load_file(&format!("{MIN}[[disk]]\nid = \"a\"\n"), &[]).1).contains("path"));
}

#[test]
fn disks_that_are_the_same_file_are_refused() {
    let _env = lock();
    let d = dir();
    let (a, b, c) = (d.join("a.raw"), d.join("b.raw"), d.join("c.raw"));
    std::fs::write(&a, b"x").unwrap();
    std::fs::hard_link(&a, &b).unwrap();
    std::fs::write(&c, b"y").unwrap();
    let disks = |x: &Path, y: &Path| {
        format!(
            "{MIN}[[disk]]\nid = \"root\"\npath = \"{}\"\n[[disk]]\nid = \"copy\"\npath = \"{}\"\n",
            x.display(),
            y.display()
        )
    };
    let e = err(load_file(&disks(&a, &b), &[]).1);
    assert!(
        e.contains("disks \"root\" and \"copy\" are the same file"),
        "{e}"
    );
    assert!(err(load_file(&disks(&a, &a), &[]).1).contains("same file"));
    assert!(load_file(&disks(&a, &c), &[]).1.is_ok());
}

#[test]
fn one_file_twice_is_allowed_only_when_both_uses_are_read_only() {
    let _env = lock();
    let d = dir();
    let a = d.join("a.iso");
    std::fs::write(&a, b"x").unwrap();
    let disk = |id: &str, ro: bool| {
        format!(
            "[[disk]]\nid = \"{id}\"\npath = \"{}\"\nreadonly = {ro}\n",
            a.display()
        )
    };
    let both = |x: bool, y: bool| format!("{MIN}{}{}", disk("one", x), disk("two", y));
    assert!(load_file(&both(true, true), &[]).1.is_ok());
    for (x, y) in [(true, false), (false, true), (false, false)] {
        let e = err(load_file(&both(x, y), &[]).1);
        assert!(
            e.contains("same file") && e.contains("both are readonly"),
            "{e}"
        );
    }
}

#[test]
fn file_forwards_and_debug_values_use_the_old_parsers() {
    let _env = lock();
    let (_, c) = load_file(
        &format!(
            "{MIN}[[nic]]\nid = \"n\"\nforward = [\"2222:22\", \"udp:9:8\"]\nhost_loopback = true\n[debug]\npage_currency = \"check\"\nwritable_on_read = false\npool = false\n"
        ),
        &[],
    );
    let c = c.unwrap();
    assert_eq!(c.nics[0].forward.len(), 2);
    assert!(c.nics[0].host_loopback && !c.pool && !c.writable_on_read);
    assert_eq!(
        c.page_currency,
        crate::parse_page_currency("check").unwrap()
    );
    assert!(
        err(load_file(
            &format!("{MIN}[[nic]]\nid = \"n\"\nforward = [\"x\"]\n"),
            &[]
        )
        .1)
        .contains("nic[0].forward: bad forward")
    );
    assert!(
        err(load_file(
            &format!("{MIN}[debug]\npage_currency = \"check-halt\"\n"),
            &[]
        )
        .1)
        .contains("debug.page_currency")
    );
    assert!(err(load_args(&["--config", "/nonexistent/vm.toml"])).contains("/nonexistent/vm.toml"));
}

/// `[debug] pci_testdev` (test only): off by default, on from the file or `--set`, carried into the launch.
#[test]
fn debug_pci_testdev_is_off_by_default_and_reaches_the_boot() {
    let _env = lock();
    let boot = |c: &Config| to_boot(c, "RT".into(), vec![7], None).unwrap();
    let (_, c) = load_file(MIN, &[]);
    let c = c.unwrap();
    assert!(!c.pci_testdev && !boot(&c).pci_testdev);
    assert!(!load_old(&[]).unwrap().pci_testdev);
    let (_, c) = load_file(&format!("{MIN}[debug]\npool = false\npci_testdev = true\n"), &[]);
    let c = c.unwrap();
    assert!(c.pci_testdev && !c.pool && boot(&c).pci_testdev);
    let (_, c) = load_file(&format!("{MIN}[debug]\npci_testdev = true\n"), &["--set", "debug.pci_testdev=off"]);
    assert!(!c.unwrap().pci_testdev, "--set overrides the file");
    assert!(load_old(&["--set", "debug.pci_testdev=on"]).unwrap().pci_testdev);
    let (_, c) = load_file(&format!("{MIN}[debug]\npci_testdev = \"yes\"\n"), &[]);
    assert!(err(c).contains("pci_testdev"));
    assert!(err(load_old(&["--set", "debug.pci_testdev=maybe"])).contains("bad boolean"));
}

/// `[debug] mapped_testdev` (test only): off by default, on from the file or `--set`, carried into the launch.
#[test]
fn debug_mapped_testdev_is_off_by_default_and_reaches_the_boot() {
    let _env = lock();
    let boot = |c: &Config| to_boot(c, "RT".into(), vec![7], None).unwrap();
    let (_, c) = load_file(MIN, &[]);
    let c = c.unwrap();
    assert!(!c.mapped_testdev && !boot(&c).mapped_testdev);
    let (_, c) = load_file(&format!("{MIN}[debug]\nmapped_testdev = true\n"), &[]);
    let c = c.unwrap();
    assert!(c.mapped_testdev && !c.pci_testdev && boot(&c).mapped_testdev);
    let (_, c) = load_file(&format!("{MIN}[debug]\nmapped_testdev = true\n"), &["--set", "debug.mapped_testdev=off"]);
    assert!(!c.unwrap().mapped_testdev, "--set overrides the file");
    assert!(load_old(&["--set", "debug.mapped_testdev=on"]).unwrap().mapped_testdev);
    assert!(err(load_old(&["--set", "debug.mapped_testdev=maybe"])).contains("bad boolean"));
}

#[test]
fn a_file_boot_is_the_same_boot_as_the_flags() {
    let _env = lock();
    let (d, c) = load_file(
        "[machine]\nram = \"1G\"\nvcpus = 2\ntransport = { disk = \"mmio\", nic = \"mmio\", rng = \"mmio\" }\n[engine]\nfexc = \"fexc\"\nfp = \"accurate\"\ncompile_cache = \"off\"\n[boot.kernel]\nimage = \"k\"\ncmdline = \"quiet\"\n[[disk]]\nid = \"root\"\npath = \"r.raw\"\n[[rng]]\nid = \"r\"\n",
        &[],
    );
    let cfg = c.unwrap();
    let b = to_boot(&cfg, "RT".into(), vec![1], None).unwrap();
    assert_eq!(
        (b.ram, b.vcpus, b.accurate_fp, b.rng, b.net),
        (1 << 30, 2, true, true, false)
    );
    assert_eq!(
        b.disks.first().map(|d| d.path.clone()),
        Some(d.join("r.raw"))
    );
    assert_eq!(b.cmdline, "quiet");
    // The table names no console: it keeps the file default.
    assert_eq!(
        b.transports,
        Transports {
            console: Pci,
            ..Transports::all(Mmio)
        }
    );
    // Several plain disks keep their order.
    let mut three = cfg.clone();
    for id in ["b", "c"] {
        let mut d = three.disks[0].clone();
        (d.id, d.path) = (id.into(), d.path.with_file_name(format!("{id}.raw")));
        three.disks.push(d);
    }
    let boot = to_boot(&three, "RT".into(), vec![1], None).unwrap();
    let ids: Vec<_> = boot.disks.iter().map(|d| d.id.as_str()).collect();
    assert_eq!(ids, ["root", "b", "c"]);
    // Read-only reaches the launch description per disk, in any position.
    let mut ro = three.clone();
    ro.disks[1].readonly = true;
    let flags: Vec<_> = to_boot(&ro, "RT".into(), vec![1], None)
        .unwrap()
        .disks
        .iter()
        .map(|d| d.readonly)
        .collect();
    assert_eq!(flags, [false, true, false]);
    // A backing file reaches the launch description.
    let mut two = cfg.clone();
    two.disks[0].backing = Some("base.raw".into());
    let boot = to_boot(&two, "RT".into(), vec![1], None).unwrap();
    assert_eq!(boot.disks[0].backing, Some(PathBuf::from("base.raw")));
}

#[test]
fn a_snapshot_disk_is_a_read_only_use_of_its_file() {
    let _env = lock();
    let d = dir();
    let (a, top) = (d.join("a.raw"), d.join("top.qcow2"));
    std::fs::write(&a, b"x").unwrap();
    let disk = |id: &str, path: &Path, extra: &str| {
        format!(
            "[[disk]]\nid = \"{id}\"\npath = \"{}\"\n{extra}",
            path.display()
        )
    };
    let run = |disks: &[String]| load_file(&format!("{MIN}{}", disks.concat()), &[]).1;
    let c = run(&[disk("one", &a, "snapshot = true\n")]).unwrap();
    assert!(c.disks[0].snapshot && !c.disks[0].readonly);
    assert_eq!(
        load_file(
            &format!("{MIN}{}", disk("one", &a, "")),
            &["--set", "disk.one.snapshot=yes"]
        )
        .1
        .unwrap()
        .disks[0]
            .snapshot,
        true
    );
    // With a read-only use, another snapshot or an overlay over it; never with a writable use.
    assert!(
        run(&[
            disk("one", &a, "snapshot = true\n"),
            disk("two", &a, "readonly = true\n")
        ])
        .is_ok()
    );
    assert!(
        run(&[
            disk("one", &a, "snapshot = true\n"),
            disk("two", &a, "snapshot = true\n")
        ])
        .is_ok()
    );
    assert!(
        run(&[
            disk("one", &a, "snapshot = true\n"),
            disk("two", &top, &format!("backing = \"{}\"\n", a.display()))
        ])
        .is_ok()
    );
    let e = err(run(&[
        disk("one", &a, "snapshot = true\n"),
        disk("two", &a, ""),
    ]));
    assert!(
        e.contains("same file") && e.contains("readonly or snapshot"),
        "{e}"
    );
}

#[test]
fn backing_files_follow_the_same_file_rule() {
    let _env = lock();
    let d = dir();
    let (base, top, other) = (d.join("base.raw"), d.join("top.qcow2"), d.join("o.qcow2"));
    std::fs::write(&base, b"x").unwrap();
    std::fs::write(&top, b"y").unwrap();
    let disk = |id: &str, path: &Path, ro: bool, backing: Option<&Path>| {
        let b = backing.map_or(String::new(), |b| {
            format!("backing = \"{}\"\n", b.display())
        });
        format!(
            "[[disk]]\nid = \"{id}\"\npath = \"{}\"\nreadonly = {ro}\n{b}",
            path.display()
        )
    };
    let run = |disks: &[String]| load_file(&format!("{MIN}{}", disks.concat()), &[]).1;
    // Its own top.
    let e = err(run(&[disk("a", &top, false, Some(&top))]));
    assert!(e.contains("disk \"a\" is its own backing file"), "{e}");
    // Another disk's writable image, before or after it in the list.
    for order in [false, true] {
        let mut disks = vec![
            disk("a", &top, false, Some(&base)),
            disk("b", &base, false, None),
        ];
        if order {
            disks.reverse();
        }
        let e = err(run(&disks));
        assert!(
            e.contains("the backing file of disk \"a\" is disk \"b\", which is writable"),
            "{e}"
        );
    }
    // Paths that do not exist yet are compared by folder and name: one new overlay named twice (through another
    // spelling of its folder), or one disk's new image named as another's backing.
    let new = d.join("new.qcow2");
    let e = err(run(&[
        disk("a", &new, false, Some(&base)),
        disk("b", &d.join("./new.qcow2"), false, Some(&base)),
    ]));
    assert!(
        e.contains("disks \"a\" and \"b\" both name") && e.contains("does not exist yet"),
        "{e}"
    );
    let e = err(run(&[
        disk("a", &top, false, Some(&new)),
        disk("b", &new, false, Some(&base)),
    ]));
    assert!(
        e.contains("the backing file of disk \"a\" is disk \"b\""),
        "{e}"
    );
    let e = err(run(&[disk("a", &new, false, Some(&new))]));
    assert!(e.contains("is its own backing file"), "{e}");
    // Shared read-only: two overlays on one base (one not created yet), and the base also a read-only disk.
    assert!(
        run(&[
            disk("a", &top, false, Some(&base)),
            disk("b", &other, false, Some(&base)),
            disk("c", &base, true, None),
        ])
        .is_ok()
    );
}

/// `machine.pvpanic`: on by default, off from the file or with `--set machine.pvpanic=false`, and the launch follows.
#[test]
fn pvpanic_key_parses_and_set_turns_it_off() {
    let _env = lock();
    let boot = |c: &Config| to_boot(c, "r".into(), Vec::new(), None).unwrap().pvpanic;
    let c = load_old(&[]).unwrap();
    assert!(c.pvpanic && boot(&c));
    let c = load_old(&["--set", "machine.pvpanic=false"]).unwrap();
    assert!(!c.pvpanic && !boot(&c));
    let (_, c) = load_file(&format!("[machine]\npvpanic = false\n{MIN}"), &[]);
    let c = c.unwrap();
    assert!(!c.pvpanic && !boot(&c));
    let (_, c) = load_file(
        &format!("[machine]\npvpanic = false\n{MIN}"),
        &["--set", "machine.pvpanic=on"],
    );
    assert!(c.unwrap().pvpanic, "--set wins over the file");
    assert!(err(load_file(&format!("[machine]\npvpanic = \"no\"\n{MIN}"), &[]).1).contains("machine.pvpanic"));
    assert!(err(load_old(&["--set", "machine.pvpanic=maybe"])).contains("--set machine.pvpanic: bad boolean"));
}

/// `machine.smbios`: on by default, off from the file or with `--set machine.smbios=false`, and the launch follows.
#[test]
fn smbios_key_parses_and_set_turns_it_off() {
    let _env = lock();
    let boot = |c: &Config| to_boot(c, "r".into(), Vec::new(), None).unwrap().smbios;
    let c = load_old(&[]).unwrap();
    assert!(c.smbios && boot(&c));
    let c = load_old(&["--set", "machine.smbios=false"]).unwrap();
    assert!(!c.smbios && !boot(&c));
    let (_, c) = load_file(&format!("[machine]\nsmbios = false\n{MIN}"), &[]);
    let c = c.unwrap();
    assert!(!c.smbios && !boot(&c));
    let (_, c) = load_file(
        &format!("[machine]\nsmbios = false\n{MIN}"),
        &["--set", "machine.smbios=on"],
    );
    assert!(c.unwrap().smbios, "--set wins over the file");
    assert!(err(load_file(&format!("[machine]\nsmbios = \"no\"\n{MIN}"), &[]).1).contains("machine.smbios"));
    assert!(err(load_old(&["--set", "machine.smbios=maybe"])).contains("--set machine.smbios: bad boolean"));
}

/// `machine.ps2`: on by default, off from the file or with `--set machine.ps2=false`, and the launch follows.
#[test]
fn ps2_key_parses_and_set_turns_it_off() {
    let _env = lock();
    let boot = |c: &Config| to_boot(c, "r".into(), Vec::new(), None).unwrap().ps2;
    let c = load_old(&[]).unwrap();
    assert!(c.ps2 && boot(&c));
    let c = load_old(&["--set", "machine.ps2=false"]).unwrap();
    assert!(!c.ps2 && !boot(&c));
    let (_, c) = load_file(&format!("[machine]\nps2 = false\n{MIN}"), &[]);
    let c = c.unwrap();
    assert!(!c.ps2 && !boot(&c));
    let (_, c) = load_file(
        &format!("[machine]\nps2 = false\n{MIN}"),
        &["--set", "machine.ps2=on"],
    );
    assert!(c.unwrap().ps2, "--set wins over the file");
    assert!(err(load_file(&format!("[machine]\nps2 = \"no\"\n{MIN}"), &[]).1).contains("machine.ps2"));
    assert!(err(load_old(&["--set", "machine.ps2=maybe"])).contains("--set machine.ps2: bad boolean"));
}

#[test]
fn every_set_key_has_a_handler_and_old_flags_map_to_known_keys() {
    let _env = lock();
    for key in KEYS {
        for value in ["on", "1", "x", "mmio", "fast", "2G"] {
            let mut cfg = Config::new(None);
            let r = set(&mut cfg, &format!("{key}={value}"), &mut BTreeSet::new());
            let e = r.err().unwrap_or_default();
            assert!(
                !e.contains("unknown key") && !e.contains("no handler"),
                "{key}={value}: {e}"
            );
        }
    }
    // apply_flags asserts (debug builds) that every key it claims is in KEYS; this line gives every flag.
    let _ = load_old(&[
        "--runtime",
        "r",
        "--initrd",
        "i",
        "--cmdline",
        "c",
        "--ram",
        "1G",
        "--vcpus",
        "1",
        "--serial",
        "s",
        "--serial-stdin",
        "--speculation-barriers",
        "--fp",
        "fast",
        "--page-currency",
        "on",
        "--writable-on-read",
        "on",
        "--rng",
        "--disk",
        "d",
        "--net",
        "--net-forward",
        "1:2",
        "--net-host-loopback",
        "--pool",
        "--check-reuse",
        "--virtio-transport",
        "pci",
        "--compile-cache",
        "off",
        "--compile-cache-mb",
        "1",
        "--perf-log",
        "p",
    ])
    .unwrap();
}

#[test]
fn review_fixes() {
    let _env = lock();
    // (1) nic= and net= both in --set machine.transport.
    for spec in ["nic=pci", "net=pci"] {
        let c = load_old(&["--set", &format!("machine.transport={spec}")]).unwrap();
        assert_eq!(
            c.transports,
            Transports {
                disk: Mmio,
                net: Pci,
                rng: Mmio,
                console: Mmio
            },
            "{spec}"
        );
    }
    // (3) `off` is special only for compile_cache.
    let (d, c) = load_file(
        "[engine]\nfexc = \"off\"\ncompile_cache = \"off\"\n[boot.kernel]\nimage = \"off\"\n",
        &[],
    );
    let c = c.unwrap();
    assert_eq!(
        (c.fexc, c.kernel),
        (Some(d.join("off")), Some(d.join("off")))
    );
    assert_eq!(c.compile_cache, Some(PathBuf::from("off")));
    // (4) readable ram errors.
    for bad in ["-1", "true", "[1]"] {
        let e = err(load_file(&format!("[machine]\nram = {bad}\n{MIN}"), &[]).1);
        assert!(
            e.contains("machine.ram: expected a size string") && !e.contains("untagged"),
            "{e}"
        );
    }
    // (5) --kernel supplies a missing image; without it the error names the table.
    let file = "[engine]\nfexc = \"f\"\n[boot.kernel]\ncmdline = \"q\"\n";
    let c = load_file(file, &["--kernel", "k"]).1.unwrap();
    assert_eq!(
        (c.kernel, c.cmdline.as_str()),
        (Some(PathBuf::from("k")), "q")
    );
    assert!(err(load_file(file, &[]).1).contains("[boot.kernel] needs image"));
    // (7) --net alone does not claim nic.net0.host_loopback or forward.
    let c = load_old(&[
        "--net",
        "--set",
        "nic.net0.host_loopback=on",
        "--set",
        "nic.net0.forward=1:2",
    ])
    .unwrap();
    assert!(c.nics[0].host_loopback);
    assert!(
        load_old(&[
            "--net",
            "--net-host-loopback",
            "--set",
            "nic.net0.host_loopback=on"
        ])
        .is_err()
    );
}

/// The annotated example in docs/configuration.md is the file crates/cli/tests/example.toml, and it loads.
#[test]
fn the_documented_example_is_in_the_doc_and_loads() {
    let _env = lock();
    let example = include_str!("../../tests/example.toml");
    assert!(include_str!("../../../../docs/configuration.md").contains(example));
    let (d, c) = load_file(example, &[]);
    let cfg = c.unwrap();
    assert_eq!(cfg.disks.len(), 4);
    assert!(cfg.disks[2].readonly && !cfg.disks[0].readonly);
    assert!(cfg.disks[3].snapshot && !cfg.disks[3].readonly && !cfg.disks[0].snapshot);
    assert_eq!(cfg.disks[0].backing, Some(d.join("debian13.raw")));
    assert_eq!((cfg.nics.len(), cfg.rngs.len()), (1, 1));
    assert_eq!(cfg.nics[0].forward.len(), 2);
    assert_eq!(cfg.ram, Some(4 << 30));
    assert_eq!(cfg.fexc, Some(d.join("../build/fexc")));
}

/// `[control] socket`: none by default (no control socket); from the file relative to its folder, by `--set` as given
/// (the working directory's), `--set` winning; an empty path, a wrong type and an unknown key are refused.
#[test]
fn control_socket_key() {
    let _env = lock();
    assert_eq!(load_old(&[]).unwrap().control_socket, None, "no socket by default");
    let (_, c) = load_file(MIN, &[]);
    assert_eq!(c.unwrap().control_socket, None);
    let (d, c) = load_file(&format!("{MIN}[control]\nsocket = \"run/c.sock\"\n"), &[]);
    assert_eq!(c.unwrap().control_socket, Some(d.join("run/c.sock")));
    let (_, c) = load_file(&format!("{MIN}[control]\nsocket = \"/abs/c.sock\"\n"), &[]);
    assert_eq!(c.unwrap().control_socket, Some(PathBuf::from("/abs/c.sock")));
    let (_, c) = load_file(&format!("{MIN}[control]\n"), &[]);
    assert_eq!(c.unwrap().control_socket, None, "an empty table names no socket");
    let c = load_old(&["--set", "control.socket=rel/c.sock"]).unwrap();
    assert_eq!(c.control_socket, Some(PathBuf::from("rel/c.sock")));
    let (_, c) = load_file(&format!("{MIN}[control]\nsocket = \"a.sock\"\n"), &["--set", "control.socket=b.sock"]);
    assert_eq!(c.unwrap().control_socket, Some(PathBuf::from("b.sock")), "--set wins");
    let e = err(load_file(&format!("{MIN}[control]\nsocket = \"\"\n"), &[]).1);
    assert!(e.contains("vm.toml: control.socket: an empty path"), "{e}");
    let e = err(load_old(&["--set", "control.socket="]));
    assert!(e.contains("--set control.socket: an empty path"), "{e}");
    let e = err(load_file(&format!("{MIN}[control]\nsocket = 3\n"), &[]).1);
    assert!(e.contains("vm.toml: control.socket"), "{e}");
    let e = err(load_file(&format!("{MIN}[control]\nport = 3\n"), &[]).1);
    assert!(e.contains("vm.toml: control") && e.contains("port"), "{e}");
    let e = err(load_old(&["--set", "control.socket=a", "--set", "control.socket=b"]));
    assert!(e.contains("--set control.socket: set twice"), "{e}");
}

/// `[display]`: off by default (`device = "none"`), 16 MiB at 1024 x 768; `device = "bochs"` from the file or `--set`
/// carries the spec into the launch; `--set` wins over the file; an unknown device, a framebuffer size that is not a
/// power of two from 4 to 64 MiB, a mode the EDID cannot describe and a framebuffer under two frames are refused, each
/// naming its key; an unknown key is refused; a firmware boot has no PCI bus for it.
#[test]
fn display_keys() {
    let _env = lock();
    let boot = |c: &Config| to_boot(c, "RT".into(), vec![7], None).unwrap();
    let default = natemu::display::Spec { vram_mib: 32, width: 1024, height: 768 };
    let c = load_old(&[]).unwrap();
    assert!(!c.display_on && c.display == default && boot(&c).display.is_none(), "no display by default");
    let (_, c) = load_file(MIN, &[]);
    assert!(boot(&c.unwrap()).display.is_none());
    let (_, c) = load_file(&format!("{MIN}[display]\ndevice = \"bochs\"\n"), &[]);
    assert_eq!(boot(&c.unwrap()).display, Some(default), "the defaults");
    let (_, c) = load_file(&format!("{MIN}[display]\ndevice = \"bochs\"\nvram_mib = 32\nwidth = 1920\nheight = 1080\n"), &[]);
    let want = natemu::display::Spec { vram_mib: 32, width: 1920, height: 1080 };
    assert_eq!(boot(&c.unwrap()).display, Some(want));
    let c = load_old(&["--set", "display.device=bochs", "--set", "display.vram_mib=64", "--set", "display.width=2560",
                       "--set", "display.height=1600"]).unwrap();
    assert_eq!(boot(&c).display, Some(natemu::display::Spec { vram_mib: 64, width: 2560, height: 1600 }));
    let (_, c) = load_file(&format!("{MIN}[display]\ndevice = \"bochs\"\nwidth = 800\n"), &["--set", "display.device=none",
                                                                                             "--set", "display.width=640"]);
    let c = c.unwrap();
    assert!(!c.display_on && c.display.width == 640 && boot(&c).display.is_none(), "--set wins over the file");
    // refusals, each naming the key
    for (toml, want) in [
        ("device = \"vga\"\n", "vm.toml: display.device: unknown display device \"vga\": bochs or none"),
        ("device = \"bochs\"\nvram_mib = 3\n", "vm.toml: display.vram_mib: 3: a power of two from 4 to 64 (MiB)"),
        ("device = \"bochs\"\nvram_mib = 2\n", "vm.toml: display.vram_mib: 2: a power of two"),
        ("device = \"bochs\"\nvram_mib = 128\n", "vm.toml: display.vram_mib: 128: a power of two from 4 to 64"),
        ("device = \"bochs\"\nvram_mib = 4\n", "vm.toml: display.vram_mib: 4 MiB holds fewer than two 1024 x 768 frames"),
        ("device = \"bochs\"\nwidth = 63\n", "vm.toml: display.width, display.height: 63 x 768: not a mode the EDID"),
        ("device = \"bochs\"\nvram_mib = 64\nwidth = 4095\nheight = 2497\n", "vm.toml: display.width, display.height: 4095 x 2497"),
        ("vram_mib = 5\n", "vm.toml: display.vram_mib: 5:"),
        ("width = -1\n", "vm.toml: display.width"),
        ("device = 1\n", "vm.toml: display.device"),
        ("colour = 1\n", "vm.toml: display"),
    ] {
        let e = err(load_file(&format!("{MIN}[display]\n{toml}"), &[]).1);
        // The file is named by its whole path (a temporary folder's), so the message ends with what is checked.
        assert!(e.contains(want) && e.split(want).next().unwrap().ends_with("/"), "{toml}: {e}");
    }
    let e = err(load_old(&["--set", "display.device=vga"]));
    assert!(e.contains("--set display.device: unknown display device \"vga\""), "{e}");
    let e = err(load_old(&["--set", "display.vram_mib=lots"]));
    assert!(e.contains("--set display.vram_mib: bad count \"lots\""), "{e}");
    let e = err(load_old(&["--set", "display.device=bochs", "--set", "display.vram_mib=256"]));
    assert!(e.contains("config: display.vram_mib: 256"), "{e}");
    let e = err(load_old(&["--set", "display.depth=32"]));
    assert!(e.contains("--set display.depth: unknown key"), "{e}");
    let e = err(load_args(&["--fexc", "f", "--firmware", "fw", "--set", "display.device=bochs"]));
    assert!(e.contains("a firmware boot has no PCI bus for the display"), "{e}");
}
