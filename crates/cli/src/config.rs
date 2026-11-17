//! The launch configuration: one typed struct, filled in layers (docs/m1-config-and-qcow2-design.md §1-2).
//!
//! Lowest first: built-in defaults, the `--config FILE`, the ten deprecated environment variables, the old command-line flags,
//! then `--set key=value`. Each layer is a series of typed assignments with today's clap parsers (`size`, `parse_fp`, ...),
//! never a text tree. Paths of file values are resolved against the file's folder; flag and `--set` values are kept as given
//! (so they mean the working directory, as before). [`to_boot`] turns the result into the library's [`Boot`].
use crate::Cli;
use natemu::linux::{Boot, Transport, Transports};
use natemu::virtio::{PortForward, SlirpOptions};
use serde::Deserialize;
use std::collections::BTreeSet;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

type Res<T> = Result<T, String>;

#[derive(Clone, Debug, PartialEq)]
pub struct Disk {
    pub id: String,
    pub path: PathBuf,
    pub readonly: bool,
    pub backing: Option<PathBuf>,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Nic {
    pub id: String,
    pub forward: Vec<PortForward>,
    pub host_loopback: bool,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Rng {
    pub id: String,
}

/// The final configuration. Fields are final values (defaults filled in); `None` is "not given".
#[derive(Clone, Debug, PartialEq)]
pub struct Config {
    /// The `--config` file as named on the command line, for messages.
    pub file: Option<String>,
    pub ram: Option<u64>,
    pub vcpus: Option<usize>,
    pub transports: Transports,
    pub serial_output: Option<PathBuf>, // None = standard output
    pub serial_stdin: bool,
    pub fexc: Option<PathBuf>,
    pub runtime: Option<PathBuf>,
    pub fp: bool,
    pub compile_cache: Option<PathBuf>, // a folder or the word `off`; None = the user cache folder
    pub compile_cache_mb: Option<u64>,
    pub speculation_barriers: bool,
    pub kernel: Option<PathBuf>,
    /// The file has a `[boot.kernel]` table (its `image` may still come from `--kernel`).
    pub kernel_table: bool,
    pub initrd: Option<PathBuf>,
    pub cmdline: String,
    pub disks: Vec<Disk>,
    pub nics: Vec<Nic>,
    pub rngs: Vec<Rng>,
    pub check_reuse: bool,
    pub pool: bool,
    pub page_currency: u64,
    pub writable_on_read: bool,
    pub perf_log: Option<PathBuf>,
}

impl Config {
    /// The defaults; the transport default is PCI with a config file and MMIO without one (the old command lines).
    fn new(file: Option<&Path>) -> Self {
        Config {
            file: file.map(|f| f.display().to_string()),
            ram: None,
            vcpus: None,
            transports: Transports::all(if file.is_some() {
                Transport::Pci
            } else {
                Transport::Mmio
            }),
            serial_output: None,
            serial_stdin: false,
            fexc: None,
            runtime: None,
            fp: false,
            compile_cache: None,
            compile_cache_mb: None,
            speculation_barriers: false,
            kernel: None,
            kernel_table: false,
            initrd: None,
            cmdline: String::new(),
            disks: vec![],
            nics: vec![],
            rngs: vec![],
            check_reuse: false,
            pool: true,
            page_currency: natemu::execution::parse_page_currency("on").unwrap(),
            writable_on_read: true,
            perf_log: None,
        }
    }
    /// The prefix of this configuration's error messages: the config file's name as given, or `config` when there is none.
    fn who(&self) -> &str {
        self.file.as_deref().unwrap_or("config")
    }
    pub fn net_options(&self) -> SlirpOptions {
        let nic = self.nics.first();
        SlirpOptions {
            forwards: nic.map(|n| n.forward.clone()).unwrap_or_default(),
            host_loopback: nic.is_some_and(|n| n.host_loopback),
        }
    }
}

// ---- the file ------------------------------------------------------------------------------------------------------

#[derive(Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct FileConfig {
    machine: Option<FMachine>,
    serial: Option<FSerial>,
    engine: Option<FEngine>,
    boot: Option<FBoot>,
    #[serde(default)]
    disk: Vec<FDisk>,
    #[serde(default)]
    nic: Vec<FNic>,
    #[serde(default)]
    rng: Vec<FRng>,
    debug: Option<FDebug>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FMachine {
    ram: Option<toml::Value>,
    vcpus: Option<usize>,
    transport: Option<FTransport>,
}
#[derive(Deserialize)]
#[serde(untagged)]
enum FSize {
    Bytes(u64),
    Text(String),
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FTransport {
    disk: Option<String>,
    nic: Option<String>,
    rng: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FSerial {
    output: Option<String>,
    stdin: Option<bool>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FEngine {
    fexc: Option<PathBuf>,
    runtime: Option<PathBuf>,
    fp: Option<String>,
    compile_cache: Option<String>,
    compile_cache_mb: Option<u64>,
    speculation_barriers: Option<bool>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FBoot {
    kernel: Option<FKernel>,
    uefi: Option<toml::Value>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FKernel {
    image: Option<PathBuf>, // required after merging with --kernel (see `validate`)
    initrd: Option<PathBuf>,
    cmdline: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FDisk {
    id: String,
    path: PathBuf,
    readonly: Option<bool>,
    backing: Option<PathBuf>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FNic {
    id: String,
    forward: Option<Vec<String>>,
    host_loopback: Option<bool>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FRng {
    id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FDebug {
    check_reuse: Option<bool>,
    pool: Option<bool>,
    page_currency: Option<String>,
    writable_on_read: Option<bool>,
    perf_log: Option<PathBuf>,
}

/// A file value's path: relative ones are taken from the file's folder.
fn rel(base: &Path, p: &Path) -> PathBuf {
    if p.is_absolute() {
        p.to_path_buf()
    } else {
        base.join(p)
    }
}

fn parse_file(file: &Path, text: &str) -> Res<FileConfig> {
    let name = file.display();
    let de = toml::Deserializer::parse(text).map_err(|e| format!("{name}: {}", e.message()))?;
    serde_path_to_error::deserialize(de).map_err(|e| {
        let path = e.path().to_string();
        let msg = e.inner().message().to_string();
        let msg = msg.lines().next().unwrap_or("").to_string();
        if path == "." {
            format!("{name}: {msg}")
        } else {
            format!("{name}: {path}: {msg}")
        }
    })
}

fn apply_file(cfg: &mut Config, f: FileConfig, base: &Path) -> Res<()> {
    let who = cfg.who().to_string();
    if let Some(m) = f.machine {
        if let Some(r) = m.ram {
            // `"4G"` (any `--ram` spelling as a string) or a plain integer of bytes.
            let bad = || {
                format!(
                    "{who}: machine.ram: expected a size string like \"4G\" or a non-negative integer of bytes"
                )
            };
            cfg.ram = Some(match r {
                toml::Value::Integer(n) => u64::try_from(n).map_err(|_| bad())?,
                toml::Value::String(t) => {
                    crate::size(&t).map_err(|e| format!("{who}: machine.ram: {e}"))?
                }
                _ => return Err(bad()),
            });
        }
        cfg.vcpus = m.vcpus.or(cfg.vcpus);
        if let Some(t) = m.transport {
            let one = |v: Option<String>, key: &str, slot: &mut Transport| -> Res<()> {
                if let Some(v) = v {
                    *slot = transport_word(&v)
                        .map_err(|e| format!("{who}: machine.transport.{key}: {e}"))?;
                }
                Ok(())
            };
            one(t.disk, "disk", &mut cfg.transports.disk)?;
            one(t.nic, "nic", &mut cfg.transports.net)?;
            one(t.rng, "rng", &mut cfg.transports.rng)?;
        }
    }
    if let Some(s) = f.serial {
        if let Some(o) = s.output {
            cfg.serial_output = (o != "stdout").then(|| rel(base, Path::new(&o)));
        }
        cfg.serial_stdin = s.stdin.unwrap_or(cfg.serial_stdin);
    }
    if let Some(e) = f.engine {
        cfg.fexc = e.fexc.map(|p| rel(base, &p)).or(cfg.fexc.take());
        cfg.runtime = e.runtime.map(|p| rel(base, &p)).or(cfg.runtime.take());
        if let Some(v) = e.fp {
            cfg.fp =
                natemu::execution::parse_fp(&v).map_err(|e| format!("{who}: engine.fp: {e}"))?;
        }
        cfg.compile_cache = e
            .compile_cache
            .map(|p| {
                if p == "off" {
                    PathBuf::from(p)
                } else {
                    rel(base, Path::new(&p))
                }
            }) // `off` is a word, not a path
            .or(cfg.compile_cache.take());
        cfg.compile_cache_mb = e.compile_cache_mb.or(cfg.compile_cache_mb);
        cfg.speculation_barriers = e.speculation_barriers.unwrap_or(cfg.speculation_barriers);
    }
    if let Some(b) = f.boot {
        match (b.kernel, b.uefi) {
            (None, None) => {
                return Err(format!(
                    "{who}: [boot] needs exactly one boot method, [boot.kernel] or [boot.uefi]; found none"
                ));
            }
            (Some(_), Some(_)) => {
                return Err(format!(
                    "{who}: [boot] needs exactly one boot method, [boot.kernel] or [boot.uefi]; found both"
                ));
            }
            (None, Some(_)) => {
                return Err(format!(
                    "{who}: [boot.uefi]: UEFI boot is planned (M3) and not built yet"
                ));
            }
            (Some(k), None) => {
                cfg.kernel_table = true;
                cfg.kernel = k.image.map(|p| rel(base, &p)).or(cfg.kernel.take());
                cfg.initrd = k.initrd.map(|p| rel(base, &p));
                cfg.cmdline = k.cmdline.unwrap_or_default();
            }
        }
    }
    for d in f.disk {
        cfg.disks.push(Disk {
            id: d.id,
            path: rel(base, &d.path),
            readonly: d.readonly.unwrap_or(false),
            backing: d.backing.map(|p| rel(base, &p)),
        });
    }
    for (i, n) in f.nic.into_iter().enumerate() {
        let forward = n
            .forward
            .unwrap_or_default()
            .iter()
            .map(|t| crate::port_forward(t))
            .collect::<Res<Vec<_>>>()
            .map_err(|e| format!("{who}: nic[{i}].forward: {e}"))?;
        cfg.nics.push(Nic {
            id: n.id,
            forward,
            host_loopback: n.host_loopback.unwrap_or(false),
        });
    }
    cfg.rngs.extend(f.rng.into_iter().map(|r| Rng { id: r.id }));
    if let Some(d) = f.debug {
        cfg.check_reuse = d.check_reuse.unwrap_or(cfg.check_reuse);
        cfg.pool = d.pool.unwrap_or(cfg.pool);
        if let Some(v) = d.page_currency {
            cfg.page_currency = crate::parse_page_currency(&v)
                .map_err(|e| format!("{who}: debug.page_currency: {e}"))?;
        }
        cfg.writable_on_read = d.writable_on_read.unwrap_or(cfg.writable_on_read);
        cfg.perf_log = d.perf_log.map(|p| rel(base, &p)).or(cfg.perf_log.take());
    }
    Ok(())
}

fn transport_word(v: &str) -> Res<Transport> {
    match v {
        "mmio" => Ok(Transport::Mmio),
        "pci" => Ok(Transport::Pci),
        _ => Err(format!("bad transport {v:?}: mmio or pci")),
    }
}

// ---- the old flags and the environment variables ---------------------------------------------------------------------

/// (clap id, environment variable, key to name in the deprecation warning) of the ten variables the CLI reads.
const ENV: [(&str, &str, &str); 10] = [
    ("serial_stdin", "NATEMU_SERIAL_STDIN", "serial.stdin"),
    (
        "speculation_barriers",
        "NATEMU_SPECULATION_BARRIERS",
        "engine.speculation_barriers",
    ),
    ("fp", "NATEMU_FP", "engine.fp"),
    (
        "page_currency",
        "NATEMU_PAGE_CURRENCY",
        "debug.page_currency",
    ),
    (
        "writable_on_read",
        "NATEMU_WRITABLE_ON_READ",
        "debug.writable_on_read",
    ),
    ("pool", "NATEMU_POOL", "debug.pool"),
    ("check_reuse", "NATEMU_CHECK_REUSE", "debug.check_reuse"),
    (
        "compile_cache",
        "NATEMU_COMPILE_CACHE",
        "engine.compile_cache",
    ),
    (
        "compile_cache_mb",
        "NATEMU_COMPILE_CACHE_MB",
        "engine.compile_cache_mb",
    ),
    ("perf_log", "NATEMU_PERF_LOG", "debug.perf_log"),
];

/// One warning per variable whose value clap took from the environment.
pub fn deprecations(cli: &Cli) -> Vec<String> {
    ENV.iter()
        .filter(|(id, ..)| cli.given.env.contains(*id))
        .map(|(_, var, key)| {
            let section = key.split('.').next().unwrap();
            let field = key.rsplit('.').next().unwrap();
            format!("natemu: {var} is deprecated; use [{section}] {field} or --set {key}=...")
        })
        .collect()
}

/// The ids clap reports as coming from the command line / from the environment.
#[derive(Default, Clone)]
pub struct Given {
    pub cmd: BTreeSet<String>,
    pub env: BTreeSet<String>,
}

/// Applies the flags clap took from `given` (the environment, or the command line) as typed assignments.
/// `seen` collects the keys set, for the command line's duplicate check.
fn apply_flags(
    cfg: &mut Config,
    cli: &Cli,
    given: &BTreeSet<String>,
    cmdline: bool,
    seen: &mut BTreeSet<String>,
) -> Res<()> {
    let mut on = |id: &str, keys: &[&str]| {
        debug_assert!(
            keys.iter()
                .all(|k| KEYS.contains(k) || k.starts_with("disk.") || k.starts_with("nic.")),
            "{keys:?}"
        );
        let hit = given.contains(id);
        if hit {
            seen.extend(keys.iter().map(|k| k.to_string()));
        }
        hit
    };
    if on("runtime", &["engine.runtime"]) {
        cfg.runtime = cli.runtime.clone();
    }
    if on("fexc", &["engine.fexc"]) {
        cfg.fexc = cli.fexc.clone();
    }
    if on("kernel", &["boot.kernel.image"]) {
        cfg.kernel = cli.kernel.clone();
    }
    if on("initrd", &["boot.kernel.initrd"]) {
        cfg.initrd = cli.initrd.clone();
    }
    if on("cmdline", &["boot.kernel.cmdline"]) {
        cfg.cmdline = cli.cmdline.clone().unwrap_or_default();
    }
    if on("ram", &["machine.ram"]) {
        cfg.ram = cli.ram;
    }
    if on("vcpus", &["machine.vcpus"]) {
        cfg.vcpus = cli.vcpus;
    }
    if on("serial", &["serial.output"]) {
        cfg.serial_output = cli.serial.clone();
    }
    if on("serial_stdin", &["serial.stdin"]) {
        cfg.serial_stdin = cli.serial_stdin;
    }
    if on("speculation_barriers", &["engine.speculation_barriers"]) {
        cfg.speculation_barriers = cli.speculation_barriers;
    }
    if on("fp", &["engine.fp"]) {
        cfg.fp = cli.fp;
    }
    if on("page_currency", &["debug.page_currency"]) {
        cfg.page_currency = cli.page_currency;
    }
    if on("writable_on_read", &["debug.writable_on_read"]) {
        cfg.writable_on_read = cli.writable_on_read;
    }
    if on("pool", &["debug.pool"]) {
        cfg.pool = cli.pool;
    }
    if on("check_reuse", &["debug.check_reuse"]) {
        cfg.check_reuse = cli.check_reuse;
    }
    if on("compile_cache", &["engine.compile_cache"]) {
        cfg.compile_cache = cli.compile_cache.clone();
    }
    if on("compile_cache_mb", &["engine.compile_cache_mb"]) {
        cfg.compile_cache_mb = cli.compile_cache_mb;
    }
    if on("perf_log", &["debug.perf_log"]) {
        cfg.perf_log = cli.perf_log.clone();
    }
    if on(
        "virtio_transport",
        &[
            "machine.transport",
            "machine.transport.disk",
            "machine.transport.nic",
            "machine.transport.rng",
        ],
    ) {
        cfg.transports = cli.virtio_transport;
    }
    if !cmdline {
        return Ok(());
    }
    // The device flags: each makes one device with the old fixed id, and is refused when the file lists that kind.
    let refuse = |flag: &str, kind: &str, key: &str| {
        format!(
            "{flag}: the config file lists [[{kind}]] devices; use --set {kind}.ID.{key}=... or edit the file"
        )
    };
    if on("disk", &["disk.disk0.path"]) {
        if !cfg.disks.is_empty() {
            return Err(refuse("--disk", "disk", "path"));
        }
        let path = cli.disk.clone().unwrap();
        cfg.disks.push(Disk {
            id: "disk0".into(),
            path,
            readonly: false,
            backing: None,
        });
    }
    // --net claims no key of its own; its options claim theirs.
    on("net_forward", &["nic.net0.forward"]);
    on("net_host_loopback", &["nic.net0.host_loopback"]);
    if on("net", &[]) && cli.net {
        if !cfg.nics.is_empty() {
            return Err(refuse("--net", "nic", "forward"));
        }
        cfg.nics.push(Nic {
            id: "net0".into(),
            forward: cli.net_forward.clone(),
            host_loopback: cli.net_host_loopback,
        });
    }
    if on("rng", &[]) && cli.rng {
        if !cfg.rngs.is_empty() {
            return Err("--rng: the config file lists [[rng]] devices; edit the file".into());
        }
        cfg.rngs.push(Rng { id: "rng0".into() });
    }
    Ok(())
}

// ---- --set -------------------------------------------------------------------------------------------------------------

/// Every setting name `--set` accepts besides the per-device `disk.ID.*` and `nic.ID.*` (`set` has a handler for each; a test
/// walks this table, and `apply_flags` asserts each name it claims is here or is a device key).
// TODO(step 7): `--virtio-transport`'s help says [default: mmio], which is wrong with a --config file (the default is pci there);
// and the serial keyword `stdout` of the file and --set differs from the flag, where `--serial stdout` is a file name.
const KEYS: [&str; 22] = [
    "machine.ram",
    "machine.vcpus",
    "machine.transport",
    "machine.transport.disk",
    "machine.transport.nic",
    "machine.transport.rng",
    "serial.output",
    "serial.stdin",
    "engine.fexc",
    "engine.runtime",
    "engine.fp",
    "engine.compile_cache",
    "engine.compile_cache_mb",
    "engine.speculation_barriers",
    "boot.kernel.image",
    "boot.kernel.initrd",
    "boot.kernel.cmdline",
    "debug.check_reuse",
    "debug.pool",
    "debug.page_currency",
    "debug.writable_on_read",
    "debug.perf_log",
];
const DEVICE_KEYS: &str = "disk.ID.{path,readonly,backing}, nic.ID.{forward,host_loopback}";

fn boolish(text: &str) -> Res<bool> {
    use clap::builder::TypedValueParser;
    clap::builder::BoolishValueParser::new()
        .parse_ref(
            &clap::Command::new("natemu"),
            None,
            std::ffi::OsStr::new(text),
        )
        .map_err(|_| format!("bad boolean {text:?}: 1/0, yes/no, on/off or true/false"))
}

/// One `--set key=value`: the key table gives the same parser as the matching old flag; string keys stay strings.
fn set(cfg: &mut Config, arg: &str, seen: &mut BTreeSet<String>) -> Res<()> {
    let (key, text) = arg
        .split_once('=')
        .ok_or_else(|| format!("--set {arg}: expected key=value"))?;
    let err = |e: String| format!("--set {key}: {e}");
    let parts: Vec<&str> = key.split('.').collect();
    let path = || PathBuf::from(text);
    match parts.as_slice() {
        ["disk" | "nic", id, field] => {
            if !seen.insert(key.to_string()) {
                return Err(err("set twice on the command line".into()));
            }
            let kind = parts[0];
            let missing = || {
                err(format!(
                    "no {kind} with id {id:?} (an override cannot create a device)"
                ))
            };
            if kind == "disk" {
                let d = cfg
                    .disks
                    .iter_mut()
                    .find(|d| d.id == *id)
                    .ok_or_else(missing)?;
                match *field {
                    "path" => d.path = path(),
                    "readonly" => d.readonly = boolish(text).map_err(err)?,
                    "backing" => d.backing = Some(path()),
                    _ => {
                        return Err(err(
                            "unknown key; expected one of path, readonly, backing".into()
                        ));
                    }
                }
            } else {
                let n = cfg
                    .nics
                    .iter_mut()
                    .find(|n| n.id == *id)
                    .ok_or_else(missing)?;
                match *field {
                    "forward" => {
                        n.forward = text
                            .split(',')
                            .filter(|t| !t.is_empty())
                            .map(crate::port_forward)
                            .collect::<Res<_>>()
                            .map_err(err)?
                    }
                    "host_loopback" => n.host_loopback = boolish(text).map_err(err)?,
                    _ => {
                        return Err(err(
                            "unknown key; expected one of forward, host_loopback".into()
                        ));
                    }
                }
            }
            return Ok(());
        }
        _ => {}
    }
    // Setting a key twice is an error, whether by flag or --set (the transport's parts count as the whole).
    let group: Vec<&str> = if key.starts_with("machine.transport") {
        vec![
            "machine.transport",
            "machine.transport.disk",
            "machine.transport.nic",
            "machine.transport.rng",
        ]
    } else {
        vec![key]
    };
    if !KEYS.contains(&key) {
        return Err(err(format!(
            "unknown key; expected one of {}, {DEVICE_KEYS}",
            KEYS.join(", ")
        )));
    }
    if group.iter().any(|k| seen.contains(*k)) {
        return Err(err("set twice on the command line".into()));
    }
    seen.insert(key.to_string());
    match key {
        "machine.ram" => cfg.ram = Some(crate::size(text).map_err(err)?),
        "machine.vcpus" => {
            cfg.vcpus = Some(
                text.parse()
                    .map_err(|_| err(format!("bad count {text:?}")))?,
            )
        }
        "machine.transport" => {
            // `nic=` is the new name of the old flag's `net=`; both are accepted.
            let spec: Vec<String> = text
                .split(',')
                .map(|p| {
                    p.strip_prefix("nic=")
                        .map_or(p.to_string(), |v| format!("net={v}"))
                })
                .collect();
            cfg.transports = crate::transports(&spec.join(",")).map_err(err)?
        }
        "machine.transport.disk" => cfg.transports.disk = transport_word(text).map_err(err)?,
        "machine.transport.nic" => cfg.transports.net = transport_word(text).map_err(err)?,
        "machine.transport.rng" => cfg.transports.rng = transport_word(text).map_err(err)?,
        "serial.output" => cfg.serial_output = (text != "stdout").then(path),
        "serial.stdin" => cfg.serial_stdin = boolish(text).map_err(err)?,
        "engine.fexc" => cfg.fexc = Some(path()),
        "engine.runtime" => cfg.runtime = Some(path()),
        "engine.fp" => cfg.fp = natemu::execution::parse_fp(text).map_err(err)?,
        "engine.compile_cache" => cfg.compile_cache = Some(path()),
        "engine.compile_cache_mb" => {
            cfg.compile_cache_mb = Some(
                text.parse()
                    .map_err(|_| err(format!("bad count {text:?}")))?,
            )
        }
        "engine.speculation_barriers" => cfg.speculation_barriers = boolish(text).map_err(err)?,
        "boot.kernel.image" => cfg.kernel = Some(path()),
        "boot.kernel.initrd" => cfg.initrd = Some(path()),
        "boot.kernel.cmdline" => cfg.cmdline = text.to_string(), // a string stays a string: --set boot.kernel.cmdline=3
        "debug.check_reuse" => cfg.check_reuse = boolish(text).map_err(err)?,
        "debug.pool" => cfg.pool = boolish(text).map_err(err)?,
        "debug.page_currency" => {
            cfg.page_currency = crate::parse_page_currency(text).map_err(err)?
        }
        "debug.writable_on_read" => {
            cfg.writable_on_read = natemu::execution::parse_writable_on_read(text).map_err(err)?
        }
        "debug.perf_log" => cfg.perf_log = Some(path()),
        _ => return Err(err("internal error: no handler for this key".into())),
    }
    Ok(())
}

// ---- validation and the build ---------------------------------------------------------------------------------------------

fn id_ok(id: &str) -> bool {
    !id.is_empty()
        && id
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-')
}

/// The checks that need no VM (design §1): all before anything is opened except the same-file test, which stats the disks.
pub fn validate(cfg: &Config) -> Res<()> {
    let who = cfg.who();
    if cfg.kernel.is_none() && cfg.kernel_table {
        return Err(format!(
            "{who}: [boot.kernel] needs image (or pass --kernel FILE)"
        ));
    }
    if cfg.kernel.is_none() {
        return Err(format!(
            "{who}: no boot method: --kernel FILE, or [boot.kernel] in a --config file"
        ));
    }
    if cfg.fexc.is_none() {
        return Err(format!(
            "{who}: no fexc: --fexc FILE, or [engine] fexc in a --config file"
        ));
    }
    let ids = cfg
        .disks
        .iter()
        .enumerate()
        .map(|(i, d)| (&d.id, "disk", i + 1))
        .chain(
            cfg.nics
                .iter()
                .enumerate()
                .map(|(i, d)| (&d.id, "nic", i + 1)),
        )
        .chain(
            cfg.rngs
                .iter()
                .enumerate()
                .map(|(i, d)| (&d.id, "rng", i + 1)),
        );
    let mut first: Vec<(&String, &str, usize)> = vec![];
    for (id, kind, n) in ids {
        if !id_ok(id) {
            return Err(format!(
                "{who}: device id {id:?} must match [a-z0-9_-]+ ({kind} {n})"
            ));
        }
        if let Some((_, k0, n0)) = first.iter().find(|(i, ..)| *i == id) {
            return Err(format!(
                "{who}: device id {id:?} is used twice ({k0} {n0} and {kind} {n})"
            ));
        }
        first.push((id, kind, n));
    }
    if cfg.nics.len() > 1 {
        return Err(format!(
            "{who}: at most one [[nic]] is supported in M1; found {}",
            cfg.nics.len()
        ));
    }
    if cfg.rngs.len() > 1 {
        return Err(format!(
            "{who}: at most one [[rng]] is supported in M1; found {}",
            cfg.rngs.len()
        ));
    }
    let key = |d: &Disk| std::fs::metadata(&d.path).ok().map(|m| (m.dev(), m.ino()));
    for (i, a) in cfg.disks.iter().enumerate() {
        for b in &cfg.disks[i + 1..] {
            if key(a).is_some() && key(a) == key(b) {
                return Err(format!(
                    "{who}: disks {:?} and {:?} are the same file",
                    a.id, b.id
                ));
            }
        }
    }
    Ok(())
}

/// Builds the configuration: defaults, file, environment, flags, `--set`; then validates it.
pub fn load(cli: &Cli) -> Res<Config> {
    let file = cli.config.as_deref();
    let mut cfg = Config::new(file);
    if let Some(f) = file {
        let text = std::fs::read_to_string(f).map_err(|e| format!("{}: {e}", f.display()))?;
        let parsed = parse_file(f, &text)?;
        let base = f
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new("."));
        apply_file(&mut cfg, parsed, base)?;
    }
    apply_flags(&mut cfg, cli, &cli.given.env, false, &mut BTreeSet::new())?;
    let mut seen = BTreeSet::new();
    apply_flags(&mut cfg, cli, &cli.given.cmd, true, &mut seen)?;
    for arg in &cli.set {
        set(&mut cfg, arg, &mut seen)?;
    }
    validate(&cfg)?;
    Ok(cfg)
}

/// The library's launch description from the final configuration (the input files already read; nothing is opened here).
pub fn to_boot(
    cfg: &Config,
    runtime: PathBuf,
    kernel: Vec<u8>,
    initrd: Option<Vec<u8>>,
) -> Res<Boot> {
    let who = cfg.who();
    if cfg.disks.len() > 1 {
        return Err(format!("{who}: more than one disk is not built yet"));
    }
    if cfg.disks.iter().any(|d| d.readonly || d.backing.is_some()) {
        return Err(format!(
            "{who}: read-only disks and backing files are not built yet"
        ));
    }
    let mut boot = Boot::new(runtime, cfg.fexc.clone().ok_or("no fexc")?, kernel);
    boot.compile_cache = natemu::compile_cache::Config::new(
        cfg.compile_cache.as_deref().map(|p| p.as_os_str()),
        cfg.compile_cache_mb,
    )?;
    boot.speculation_barriers = cfg.speculation_barriers;
    boot.accurate_fp = cfg.fp;
    boot.page_currency = cfg.page_currency;
    boot.writable_on_read = cfg.writable_on_read;
    boot.rng = !cfg.rngs.is_empty();
    boot.disk = cfg.disks.first().map(|d| d.path.clone());
    boot.net = !cfg.nics.is_empty();
    boot.net_options = cfg.net_options();
    boot.pool = cfg.pool;
    boot.check_reuse = cfg.check_reuse;
    boot.transports = cfg.transports;
    boot.initrd = initrd;
    boot.cmdline = cfg.cmdline.clone();
    boot.ram = cfg.ram.unwrap_or(boot.ram);
    boot.vcpus = cfg.vcpus.unwrap_or(boot.vcpus);
    Ok(boot)
}

#[cfg(test)]
mod tests;
