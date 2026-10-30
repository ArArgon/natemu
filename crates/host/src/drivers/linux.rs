//! `boot-linux`: load a bzImage with the 64-bit boot protocol ([`crate::boot`]) and run the BSP from the protocol's
//! entry state on one vCPU. A driver over the library pieces (boot, execution, vm, owners): no tamper, no fixture.
//!
//!   natemu-host RUNTIME.elf boot-linux FEXC BZIMAGE [--initrd FILE] [--cmdline TEXT] [--ram BYTES] [--rsdp ADDR]
//!       [--code-cache BYTES] [--load-only] [--dump FILE] [--stop-at ADDR] [--save ADDR LEN FILE] [--watchdog-ms MS] [--max-block N]
//!
//! The map's low RAM ends at `--ram` (default: the boot map's 2 GiB), which is also the VM's guest RAM (M1).
//! `--code-cache BYTES` sets the code cache (default: the largest, `CODE_BYTES_MAX` less its head). `--load-only` loads into host memory and runs nothing (no VM: RUNTIME and FEXC are not opened).
//! `--dump FILE` writes what the loader wrote into guest RAM (before any run) as records `[u64 gpa][u64 length][bytes]`,
//! one per write ([`Loaded::writes`]; guest RAM is zero elsewhere). `--stop-at ADDR` ends the run when the BSP is about to
//! execute linear ADDR (the execution mode's stop RIP: a block boundary, nothing fetched there). `--save` writes guest-physical
//! bytes after the run. Prints one JSON object: the placement (kernel, window, zero page, command line, initrd, e820,
//! entry state) and, after a run, the stop and the final state.
use super::super::*;
use crate::boot::{self, Inputs, Layout, Loaded};
use crate::execution::{self, Compiler, Stop};
use natemu_abi::execution::{self as exec, Cpu, State};
use natemu_abi::guest_memory::GuestRam;
use std::fmt::Write as _;
use std::path::Path;

const USAGE: &str = "usage: natemu-host RUNTIME.elf boot-linux FEXC BZIMAGE [--initrd FILE] [--cmdline TEXT] [--ram BYTES] \
                     [--rsdp ADDR] [--code-cache BYTES] [--load-only] [--dump FILE] [--stop-at ADDR] [--save ADDR LEN FILE] [--watchdog-ms MS] [--max-block N]";
/// Largest image or initrd read (the protocol's 32-bit sizes).
const MAX_INPUT: usize = 1 << 32;

fn number(text: &str) -> Result<u64> {
    match text.strip_prefix("0x") {
        Some(h) => u64::from_str_radix(h, 16),
        None => text.parse(),
    }
    .map_err(|_| format!("bad number {text}"))
}

pub(crate) fn run(args: &[String]) -> Result<()> {
    let [_, resident, _, fexc, image, options @ ..] = args else {
        return Err(USAGE.into());
    };
    let (mut initrd, mut cmdline, mut ram, mut rsdp, mut load_only, mut dump) = (None, String::new(), None, None, false, None);
    let (mut stop_at, mut saves, mut watchdog_ms, mut max_block) = (None, Vec::new(), 60_000, None);
    let mut code_bytes = exec::cache::CODE_BYTES_MAX - exec::cache::HEAD_BYTES;
    let mut options = options.iter();
    while let Some(option) = options.next() {
        let mut value = || options.next().ok_or_else(|| USAGE.to_string());
        match option.as_str() {
            "--initrd" => initrd = Some(value()?.clone()),
            "--cmdline" => cmdline = value()?.clone(),
            "--ram" => ram = Some(number(value()?)?),
            "--rsdp" => rsdp = Some(number(value()?)?),
            "--code-cache" => code_bytes = number(value()?)? as usize,
            "--load-only" => load_only = true,
            "--dump" => dump = Some(value()?.clone()),
            "--stop-at" => stop_at = Some(number(value()?)?),
            "--save" => saves.push((number(value()?)?, number(value()?)?, value()?.clone())),
            "--watchdog-ms" => watchdog_ms = number(value()?)?,
            "--max-block" => max_block = Some(u32::try_from(number(value()?)?).map_err(|_| "--max-block: too large")?),
            _ => return Err(USAGE.into()),
        }
    }
    let image = memory::read_input(image, MAX_INPUT)?;
    let initrd = initrd.map(|p| memory::read_input(&p, MAX_INPUT)).transpose()?;
    let inputs = Inputs { image: &image, cmdline: &cmdline, initrd: initrd.as_deref(), rsdp };

    let layout = Layout { low_end: ram.unwrap_or(Layout::DEFAULT.low_end), high: 0 };
    layout.e820().map_err(|e| e.to_string())?; // before allocating L bytes
    if load_only {
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

    let backing = execution::prepare(resident, code_bytes, 1, layout.low_end as usize)?;
    require_vcpus(1)?;
    let guest = backing.ram;
    let loaded = boot::load(&guest, &layout, &inputs).map_err(|e| e.to_string())?;
    if let Some(path) = &dump {
        write_dump(path, &guest, &loaded)?;
    }
    execution::guest_ram_written(&backing.memory);
    execution::set_cpu(backing.vcpu(0), loaded.cpu);

    let machine = Machine::create(&backing.memory)?;
    let stops = execution::Stops::new(&backing, false);
    let watchdog = std::time::Duration::from_millis(watchdog_ms);
    let prepare = |v: usize| -> Result<Vcpu> {
        let cpu = Vcpu::create()?;
        execution::enter(&cpu, backing.vcpu(v), true)?;
        machine.route(v)?;
        Ok(cpu)
    };
    let body = |v: usize, cpu: &Vcpu, cancelled: &std::sync::atomic::AtomicBool| -> Result<(Stop, u64)> {
        let mut compiler = Compiler::new(Path::new(fexc), cpu, max_block)?;
        let mut requests = execution::Requests { doorbell: Some(machine.doorbell()), ..Default::default() };
        let stop = execution::run(cpu, backing.vcpu(v), &mut compiler, stop_at, watchdog, &mut requests, &stops, cancelled);
        stops.end(v);
        Ok((stop?, compiler.compiles))
    };
    let started = std::time::Instant::now();
    let supervision = owners::supervise(1, &prepare, &body);
    let destroyed = machine.destroy();
    let supervision = supervision?;
    hv(destroyed, "destroy VM")?;
    if let Some(e) = supervision.error {
        return Err(e);
    }
    let (stop, compiles) = supervision.outputs.into_iter().next().flatten().ok_or("vCPU without a result")?;
    for (gpa, length, path) in &saves {
        let mut bytes = vec![0u8; *length as usize];
        if !guest.copy_out(*gpa, &mut bytes) {
            return Err(format!("--save {gpa:#x}+{length:#x}: outside guest RAM"));
        }
        fs::write(path, bytes).map_err(|e| format!("{path}: {e}"))?;
    }
    let s: State = backing.vcpu(0).read_record(exec::STATE_OFFSET);
    // M1 pool use: the code cache and metadata arena filled since the last flush, and the shadow pool's resets.
    let c: exec::cache::Control = backing.vcpu(0).read_record(exec::cache::LOOKUP_OFFSET + exec::cache::CONTROL);
    let (kind, reason) = match &stop {
        Stop::Reached => ("reached".to_string(), String::new()),
        Stop::Resident { stop: exec::stop::SHUTDOWN, .. } => ("shutdown".into(), format!("fault {:x?}", s.fault)),
        Stop::Resident { stop, status, detail } => ("resident".into(), format!("{} status={status} detail={detail} fault={:x?}", exec::stop::name(*stop), s.fault)),
        other => (format!("{other:?}").split(['(', ' ', '{']).next().unwrap().to_lowercase(), format!("{other:?}")),
    };
    println!(
        "{},\"stop\":\"{kind}\",\"reason\":\"{}\",\"state\":{},\"stats\":{{\"blocks\":{},\"compiles\":{compiles},\"systems\":{},\"flushes\":{},\"interrupts\":{},\"evictions\":{},\"retries\":{},\"shadow_resets\":{},\"cache\":{{\"code_used\":{},\"code_bytes\":{},\"meta_used\":{},\"blocks\":{}}},\"seconds\":{:.3}}}}}",
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
        started.elapsed().as_secs_f64()
    );
    Ok(())
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
