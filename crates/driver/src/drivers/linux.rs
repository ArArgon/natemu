//! `boot-linux`: load a bzImage with the 64-bit boot protocol ([`crate::boot`]) and run it on the library's boot entry
//! ([`natemu::linux::Boot`]): no hook, no fixture. This driver adds the load-only path, the dump, the clock trace, the
//! saves and the JSON report.
//!
//!   natemu-host RUNTIME.elf boot-linux FEXC BZIMAGE [--initrd FILE] [--cmdline TEXT] [--ram BYTES] [--rsdp ADDR]
//!       [--vcpus N] [--code-cache BYTES] [--load-only] [--dump FILE] [--stop-at ADDR] [--save ADDR LEN FILE] [--watchdog-ms MS] [--max-block N]
//!       [--check-reuse] [--code-shape] [--speculation-barriers] [--serial FILE] [--serial-stdin] [--strict-unassigned]
//!
//! `--check-reuse` (perf chunk 4, debug): every translation reused at another RIP than its own is compiled there too and the
//! two must be equal (`Compiler::check_reuse`). `--code-shape` (debug, off by default): admission's code-shape check on every
//! object, as the program driver's option (which also turns on `--check-reuse`; here they are separate).
//!
//! L4a: and the I/O APIC and 8259 pair ([`crate::irq`]). L4b: the PIT, the RTC and the ACPI PM block ([`crate::clock::install`]);
//! S5 or the reset register end the run with the stop `poweroff` or `reset`. L5: `--lost-ticks pit=reinject|discard,rtc=..,
//! lapic=..` sets the periodic sources' lost-tick policies (`clock::LostTicks`); the result gains `ticks` (each source's
//! counts), and `--clock-trace FILE` writes them every 20 ms against the guest's TSC. L3: the VM has COM1, a 16550A at ports 3F8H-3FFH ([`crate::uart`]), whose output goes to `--serial FILE` as it is
//! sent; every other port and every device-range page is unassigned (all-ones reads, ignored writes, counted; with
//! `--strict-unassigned` a typed stop). The result gains `ports` and `unassigned_pages` (counts) and `serial_bytes`.
//!
//! The ACPI tables ([`boot::acpi`]) describe `--vcpus` processors (default 1, at most `cache::MAX_VCPUS`) and start with
//! the RSDP at `--rsdp` (default the ACPI NVS range's start). X4: the VM has that many vCPUs; vCPU 0 is the BSP, each
//! other one starts as an AP in the firmware's waiting state on the tables' wakeup mailbox (`State::mailbox`), with
//! the loader's AP wake state (`Loaded::ap`) and a reset local APIC. The run ends when any vCPU's run ends (the BSP's
//! `--stop-at`, a stop, its watchdog): the others are cancelled. The result's `stop`, `state` and `stats` are the BSP's;
//! `ended_first` names the vCPU whose end ended the run and its stop; `vcpus` lists every vCPU's stop, RIP, blocks,
//! compiles, shootdowns asked and mailbox record ([GPA still waited on, sleeps, the guest TSC at its acknowledgement, us
//! spent marking the page]).
//! The map's low RAM ends at `--ram` (default: the boot map's 2 GiB), which is also the VM's guest RAM (M1).
//! `--code-cache BYTES` sets the code cache (default: the largest, `CODE_BYTES_MAX` less its head). `--load-only` loads into host memory and runs nothing (no VM: RUNTIME and FEXC are not opened).
//! `--dump FILE` writes what the loader wrote into guest RAM (before any run) as records `[u64 gpa][u64 length][bytes]`,
//! one per write ([`Loaded::writes`]; guest RAM is zero elsewhere). `--stop-at ADDR` ends the run when the BSP is about to
//! execute linear ADDR (the execution mode's stop RIP: a block boundary, nothing fetched there). `--save` writes guest-physical
//! bytes after the run. Prints one JSON object: the placement (kernel, window, zero page, command line, initrd, e820,
//! entry state) and, after a run, the stop and the final state. SIGINT or SIGTERM cancels the run (the library's
//! canceller; the report is still printed), a second one exits 130 at once, as the `natemu` stub.
//!
//! Performance phase (perf plan §2): `--perf-log FILE` (or `NATEMU_PERF_LOG=FILE`) turns on the library's timing log (`natemu::perf`): process
//! phases and one line per vCPU in FILE, one line per translation request in FILE.vN. The resident's buckets need a
//! runtime built with `--features perf` (else they read zero and `"magic":false`).
use super::super::*;
use crate::boot::{self, Inputs, Layout, Loaded};
use crate::execution::{self, Stop};
use natemu_abi::execution::{self as exec, Cpu, State};
use natemu_abi::guest_memory::GuestRam;
use std::fmt::Write as _;

/// Largest image or initrd read (the protocol's 32-bit sizes).
const MAX_INPUT: usize = 1 << 32;

fn number(text: &str) -> Result<u64> {
    match text.strip_prefix("0x") {
        Some(h) => u64::from_str_radix(h, 16),
        None => text.parse(),
    }
    .map_err(|_| format!("bad number {text}"))
}

/// Boot a bzImage on the library's boot entry and print one JSON report. Numbers are decimal or 0x hex.
#[derive(clap::Args)]
pub(crate) struct Args {
    /// The fexc compiler (`unused` with --load-only)
    fexc: String,
    /// The Linux bzImage
    image: String,
    #[arg(long, value_name = "FILE")]
    initrd: Option<String>,
    #[arg(long, value_name = "TEXT", default_value = "", allow_hyphen_values = true)]
    cmdline: String,
    /// Guest RAM, the map's low RAM end [default: the boot map's 2 GiB]
    #[arg(long, value_name = "BYTES", value_parser = number)]
    ram: Option<u64>,
    /// The RSDP's address [default: the ACPI NVS range's start]
    #[arg(long, value_name = "ADDR", value_parser = number)]
    rsdp: Option<u64>,
    #[arg(long, value_name = "N", value_parser = number, default_value = "1")]
    vcpus: u64,
    /// The code cache [default: the largest]
    #[arg(long = "code-cache", value_name = "BYTES", value_parser = number)]
    code_bytes: Option<u64>,
    /// Load into host memory and run nothing (RUNTIME and FEXC are not opened)
    #[arg(long)]
    load_only: bool,
    /// Write the loader's writes as records [u64 gpa][u64 length][bytes]
    #[arg(long, value_name = "FILE")]
    dump: Option<String>,
    /// End the run when the BSP is about to execute linear ADDR
    #[arg(long, value_name = "ADDR", value_parser = number)]
    stop_at: Option<u64>,
    /// Write guest-physical bytes after the run (repeatable)
    #[arg(long, num_args = 3, value_names = ["ADDR", "LEN", "FILE"])]
    save: Vec<String>,
    #[arg(long, value_name = "MS", value_parser = number, default_value = "60000")]
    watchdog_ms: u64,
    #[arg(long, value_name = "N", value_parser = number)]
    max_block: Option<u64>,
    #[arg(long)]
    check_reuse: bool,
    #[arg(long)]
    code_shape: bool,
    #[arg(long, env = "NATEMU_SPECULATION_BARRIERS", value_parser = clap::builder::BoolishValueParser::new())]
    speculation_barriers: bool,
    #[arg(long, value_name = "FILE")]
    serial: Option<String>,
    #[arg(long, env = "NATEMU_SERIAL_STDIN", value_parser = clap::builder::BoolishValueParser::new())]
    serial_stdin: bool,
    #[arg(long = "strict-unassigned")]
    strict: bool,
    /// pit=reinject|discard,rtc=..,lapic=..
    #[arg(long = "lost-ticks", value_name = "SPEC", value_parser = crate::clock::LostTicks::parse)]
    lost: Option<crate::clock::LostTicks>,
    #[arg(long = "clock-trace", value_name = "FILE")]
    trace: Option<String>,
    /// The persistent compile cache's directory, or off [default: ~/Library/Caches/natemu/fexc]
    #[arg(long, value_name = "DIR|off", env = "NATEMU_COMPILE_CACHE")]
    compile_cache: Option<std::path::PathBuf>,
    /// The compile cache's cap in MiB [default: 512]
    #[arg(long, value_name = "N", env = "NATEMU_COMPILE_CACHE_MB")]
    compile_cache_mb: Option<u64>,
    /// Write the timing log (natemu::perf) to FILE
    #[arg(long, value_name = "FILE", env = "NATEMU_PERF_LOG")]
    perf_log: Option<std::path::PathBuf>,
}

pub(crate) fn run(resident: &str, args: Args) -> Result<()> {
    let Args { fexc, image, initrd, cmdline, ram, rsdp, vcpus, code_bytes, load_only, dump, stop_at, save, watchdog_ms, max_block, check_reuse, code_shape, speculation_barriers, serial, serial_stdin, strict, lost, trace, compile_cache, compile_cache_mb, perf_log } = args;
    if let Some(path) = perf_log {
        natemu::perf::enable(path)?;
        natemu::perf::mark("main");
    }
    let (fexc, image, rsdp, vcpus) = (&fexc, &image, rsdp.unwrap_or(boot::ACPI_NVS.0), vcpus as usize);
    let code_bytes = code_bytes.map_or(exec::cache::CODE_BYTES_MAX - exec::cache::HEAD_BYTES, |b| b as usize);
    let max_block = max_block.map(|n| u32::try_from(n).map_err(|_| "--max-block: too large")).transpose()?;
    let saves = save.chunks(3).map(|s| Ok((number(&s[0])?, number(&s[1])?, s[2].clone()))).collect::<Result<Vec<_>>>()?;
    let lost = lost.unwrap_or(crate::clock::LostTicks::DEFAULT);
    let image = memory::read_input(image, MAX_INPUT)?;
    let initrd = initrd.map(|p| memory::read_input(&p, MAX_INPUT)).transpose()?;
    if !(1..=exec::cache::MAX_VCPUS).contains(&vcpus) {
        return Err(format!("--vcpus {vcpus}: 1..={}", exec::cache::MAX_VCPUS));
    }
    let layout = Layout { low_end: ram.unwrap_or(Layout::DEFAULT.low_end), high: 0 };
    layout.e820().map_err(|e| e.to_string())?; // before allocating L bytes
    if load_only {
        let tables = boot::acpi::build(vcpus, rsdp);
        let inputs = Inputs { image: &image, cmdline: &cmdline, initrd: initrd.as_deref(), acpi: Some(&tables) };
        // Zero-filled on demand: only the pages the loader writes are touched.
        let mut host = vec![0u64; layout.low_end as usize / 8];
        // SAFETY: `host` backs [0, L) and outlives `guest`.
        let guest = unsafe { GuestRam::new(0, layout.low_end, host.as_mut_ptr() as u64) };
        let loaded = boot::load(&guest, &layout, &inputs).map_err(|e| e.to_string())?;
        if let Some(path) = &dump {
            write_dump(path, &guest, &loaded)?;
        }
        let _ = (guest, &mut host); // `guest` is not used past here; `host` lives to the end of this block
        println!("{}}}", placement(&loaded, &layout));
        return Ok(());
    }

    let mut boot = natemu::linux::Boot {
        initrd,
        cmdline,
        ram: layout.low_end,
        vcpus,
        rsdp,
        code_bytes,
        strict_unassigned: strict,
        lost_ticks: lost,
        watchdog: std::time::Duration::from_millis(watchdog_ms),
        max_block,
        stop_at,
        compile_cache: crate::compile_cache::Config::new(compile_cache.as_deref().map(|p| p.as_os_str()), compile_cache_mb)?,
        check_reuse,
        code_shape,
        speculation_barriers,
        ..natemu::linux::Boot::new(resident, fexc, image)
    };
    if let Some(path) = &serial {
        boot.serial = Some(Box::new(fs::File::create(path).map_err(|e| format!("{path}: {e}"))?));
    }
    let trace = match &trace {
        Some(path) => Some(std::io::BufWriter::new(fs::File::create(path).map_err(|e| format!("{path}: {e}"))?)),
        None => None,
    };
    let prepared = boot.prepare()?;
    natemu::perf::mark("prepared");
    let (backing, loaded, board, devices) = (prepared.backing(), prepared.loaded(), prepared.board(), prepared.devices());
    // SAFETY: `guest` is used only while `prepared` lives (this function).
    let guest = unsafe { prepared.guest() };
    if let Some(path) = &dump {
        write_dump(path, &guest, loaded)?;
    }
    let started = std::time::Instant::now();
    // L5: `--clock-trace FILE`, every 20 ms: TSC ns since the VM's epoch (the guest's TSC), then the PIT's and RTC's tick
    // counts (`clock::TickStats`) and the LAPIC timer's expiries taken, merged and reinjected (its record, read racily).
    let tracing = std::sync::atomic::AtomicBool::new(trace.is_some());
    prepared.canceller().on_signals();
    if serial_stdin {
        prepared.serial_input(natemu::linux::serial_stdin()); // the stub's `--serial-stdin`, after the signals
    }
    let outcome = std::thread::scope(|scope| -> Result<_> {
        if let Some(mut file) = trace {
            let tracing = &tracing;
            scope.spawn(move || {
                use std::io::Write as _;
                let area = backing.vcpu(0);
                let n = |a: &std::sync::atomic::AtomicU64| a.load(std::sync::atomic::Ordering::SeqCst);
                while tracing.load(std::sync::atomic::Ordering::SeqCst) {
                    let t = execution::ticks_ns(crate::clock::now()) - execution::ticks_ns(area.epoch());
                    let (p, r, l) = (&board.pit, &board.rtc, execution::lapic(area));
                    let _ = writeln!(file, "{t} pit {} {} {} {} {} {} rtc {} {} {} lapic {} {} {}", n(&p.rises), n(&p.fresh), n(&p.coalesced), n(&p.dropped),
                        n(&p.missed), n(&p.reinjected), n(&r.rises), n(&r.missed), n(&r.reinjected), l.fired, l.missed, l.reinjected);
                    std::thread::sleep(std::time::Duration::from_millis(20));
                }
            });
        }
        let outcome = prepared.run();
        tracing.store(false, std::sync::atomic::Ordering::SeqCst);
        outcome
    })?;
    let (stop, compiles) = outcome.each[0].as_ref().unwrap();
    let compiles = *compiles;
    let each: Vec<(String, u64)> = outcome.each.iter().map(|o| o.as_ref().map_or(("none".into(), 0), |(s, c)| (stop_name(s), *c))).collect();
    let vcpu_json: Vec<String> = each
        .iter()
        .enumerate()
        .map(|(v, (stop, compiles))| {
            let s: State = backing.vcpu(v).read_record(exec::STATE_OFFSET);
            let acked = if s.mailbox_acked == 0 { 0 } else { execution::ticks_ns(s.mailbox_acked) - execution::ticks_ns(backing.vcpu(v).epoch()) };
            // Perf chunk 10: its host requests and its blocks copied in the resident (refused by kind, checked).
            let c: exec::cache::Control = backing.vcpu(v).read_record(exec::cache::LOOKUP_OFFSET + exec::cache::CONTROL);
            format!(
                "{{\"stop\":\"{}\",\"rip\":{},\"blocks\":{},\"compiles\":{compiles},\"requests\":{},\"reuse\":[{},{:?},{}],\"halts\":{},\"interrupts\":{},\"mailbox\":[{},{},{acked},{}],\"shootdowns\":{},\"fault\":\"{:x?}\",\"state\":{}}}",
                stop.replace('"', "'"), hex(s.cpu.rip), s.blocks, s.requests, c.reuse_copies, c.reuse_refused, c.reuse_checks, s.halts, s.interrupts, hex(s.mailbox), s.mailbox_wakes,
                execution::ticks_ns(s.mailbox_mark_ticks) / 1000, s.shootdowns, s.fault, cpu_json(&s.cpu)
            )
        })
        .collect();
    let first = outcome.ended_first;
    let first_json = match each.get(first) {
        Some((stop, _)) => format!("{{\"vcpu\":{first},\"stop\":\"{}\"}}", stop.replace('"', "'")),
        None => "null".into(),
    };
    for (gpa, length, path) in &saves {
        let mut bytes = vec![0u8; *length as usize];
        if !guest.copy_out(*gpa, &mut bytes) {
            return Err(format!("--save {gpa:#x}+{length:#x}: outside guest RAM"));
        }
        fs::write(path, bytes).map_err(|e| format!("{path}: {e}"))?;
    }
    natemu::perf::mark("report");
    let s: State = backing.vcpu(0).read_record(exec::STATE_OFFSET);
    let lapic_end = execution::lapic(backing.vcpu(0));
    // M1 pool use: the code cache and metadata arena filled since the last flush, and the shadow pool's resets.
    let c: exec::cache::Control = backing.vcpu(0).read_record(exec::cache::LOOKUP_OFFSET + exec::cache::CONTROL);
    let (kind, reason) = match stop {
        Stop::Reached => ("reached".to_string(), String::new()),
        Stop::Resident { stop: exec::stop::SHUTDOWN, .. } => ("shutdown".into(), format!("fault {:x?}", s.fault)),
        Stop::Power(p) => (super::program::power_name(*p).into(), String::new()),
        Stop::Resident { stop, status, detail } => {
            let m: exec::Mmio = backing.vcpu(0).read_record(exec::MMIO_OFFSET);
            // The access only where the host answered for it (a refusal before any request names none: just its status).
            let access = match m.status {
                exec::mmio::UNASSIGNED | exec::mmio::DEVICE => format!(" access={:#x}/{}/{} {}", m.gpa, m.width, if m.write != 0 { "w" } else { "r" }, exec::mmio::name(m.status)),
                status => format!(" mmio={}", exec::mmio::name(status)),
            };
            ("resident".into(), format!("{} status={status} detail={detail} fault={:x?}{access}", exec::stop::name(*stop), s.fault))
        }
        other => (format!("{other:?}").split(['(', ' ', '{']).next().unwrap().to_lowercase(), format!("{other:?}")),
    };
    println!(
        "{},\"stop\":\"{kind}\",\"reason\":\"{}\",\"state\":{},\"stats\":{{\"blocks\":{},\"compiles\":{compiles},\"systems\":{},\"flushes\":{},\"interrupts\":{},\"evictions\":{},\"retries\":{},\"shadow_resets\":{},\"cache\":{{\"code_used\":{},\"code_bytes\":{},\"meta_used\":{},\"blocks\":{}}},\"seconds\":{:.3},\"requests\":{},\"invalidations\":{},\"steps\":{},\"links\":{},\"unlinks\":{},\"k1\":{}}},{},\"serial_bytes\":{},\"serial_error\":{},\"ticks\":{{\"pit\":{},\"rtc\":{},\"lapic\":{{\"fired\":{},\"missed\":{},\"reinjected\":{}}}}},\"ended_first\":{},\"vcpus\":[{}]}}",
        placement(&loaded, &layout),
        reason.replace('"', "'"),
        cpu_json(&s.cpu),
        s.blocks,
        s.systems,
        s.generation,
        s.interrupts,
        s.evictions,
        s.retries,
        execution::shadow_resets(backing.vcpu(0)),
        c.code_used,
        c.code_bytes,
        c.meta_used,
        c.blocks,
        started.elapsed().as_secs_f64(),
        s.requests,
        s.invalidations,
        s.steps,
        s.links,
        s.unlinks,
        execution::k1_stats(backing.vcpu(0)),
        devices.report(),
        prepared.serial_bytes(),
        prepared.serial_error().map_or("null".into(), |e| format!("\"{}\"", e.replace('"', "'"))),
        board.pit.json(),
        board.rtc.json(),
        lapic_end.fired,
        lapic_end.missed,
        lapic_end.reinjected,
        first_json,
        vcpu_json.join(",")
    );
    natemu::perf::mark("exit");
    Ok(())
}

/// A stop as the result names it: its kind, and for a resident stop its reason.
fn stop_name(stop: &Stop) -> String {
    match stop {
        Stop::Resident { stop, status, detail } => format!("resident {} status={status} detail={detail}", exec::stop::name(*stop)),
        other => format!("{other:?}").chars().take(200).collect(),
    }
}

/// The loader's writes, read back from guest RAM.
fn write_dump(path: &str, guest: &GuestRam, loaded: &Loaded) -> Result<()> {
    let mut out = Vec::new();
    for &(gpa, length) in &loaded.writes {
        let mut data = vec![0u8; length as usize];
        guest.copy_out(gpa, &mut data);
        out.extend(gpa.to_le_bytes().into_iter().chain(length.to_le_bytes()).chain(data));
    }
    fs::write(path, out).map_err(|e| format!("{path}: {e}"))
}

fn hex(v: u64) -> String {
    format!("\"{v:#x}\"")
}
/// The placement and the entry state, as an unclosed JSON object.
fn placement(l: &Loaded, layout: &Layout) -> String {
    let map: Vec<String> = l.map.iter().map(|e| format!("[{},{},{}]", hex(e.addr), hex(e.size), e.kind)).collect();
    let initrd = l.initrd.map_or("null".into(), |(a, n)| format!("[{},{}]", hex(a), hex(n)));
    let mut s = String::new();
    let _ = write!(
        s,
        "{{\"low_end\":{},\"kernel\":{},\"window\":{},\"zero_page\":{},\"cmdline\":{},\"initrd\":{initrd},\"version\":{},\"memory_map\":[{}],\"entry\":{}",
        hex(layout.low_end),
        hex(l.kernel),
        hex(l.window),
        hex(l.zero_page),
        hex(l.cmdline),
        hex(l.setup.version.into()),
        map.join(","),
        cpu_json(&l.cpu)
    );
    s
}
fn cpu_json(c: &Cpu) -> String {
    let gpr: Vec<String> = c.gpr.iter().map(|&v| hex(v)).collect();
    let seg: Vec<String> = c.seg.iter().map(|s| hex(s.selector.into())).collect();
    format!(
        "{{\"gpr\":[{}],\"rip\":{},\"rflags\":{},\"cr0\":{},\"cr2\":{},\"cr3\":{},\"cr4\":{},\"efer\":{},\"cpl\":{},\"seg\":[{}],\"fs_base\":{},\"gs_base\":{},\"gdtr\":[{},{}],\"idtr\":[{},{}]}}",
        gpr.join(","),
        hex(c.rip),
        hex(c.rflags),
        hex(c.cr0),
        hex(c.cr2),
        hex(c.cr3),
        hex(c.cr4),
        hex(c.efer),
        c.cpl,
        seg.join(","),
        hex(c.fs_base),
        hex(c.gs_base),
        hex(c.gdtr.base),
        hex(c.gdtr.limit),
        hex(c.idtr.base),
        hex(c.idtr.limit)
    )
}
