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
//! waiting state on the tables' wakeup mailbox (X4), or without it in the wait-for-SIPI state (M2 Part 2, [`Boot::mailbox`]). [`Prepared::run`] runs the VM: the run ends when any vCPU's run ends
//! (the BSP's `stop_at`, a stop, power off or reset, its watchdog), and the others are cancelled. M2 Part 2: with
//! [`Boot::firmware`] the VM boots a firmware image instead of a kernel (the firmware boot mode, [`boot::firmware`]).
use crate::boot::{self, Inputs, Layout, Loaded};
use crate::execution::{self, Backing, Compiler, Stop};
use crate::vm::{Vcpu, hv, require_vcpus};
use crate::{Result, clock, compile_cache, devices, owners, uart};
use natemu_abi::execution::{self as exec, State};
use natemu_abi::guest_memory::GuestRam;
use std::io::{Read, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering::SeqCst};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

/// The disk's request queues over PCI: one per vCPU, at most `virtio::MAX_QUEUES`; `NATEMU_BLK_QUEUES` overrides it
/// (calibration and comparison; 1 is the single queue of the MMIO transport). Measured on an M2 Max (November 14,
/// 2026, 2 vCPUs, against one queue): depth-1 4 KiB direct reads 75-77 -> 111 MB/s for 74 -> 46 us of host CPU per
/// request (a request no longer wakes a second vCPU), four fio readers at depth 16 +37%; but one fio job at depth 16
/// now submits and completes on one vCPU: mixed 4 KiB 54-56k -> 33-37k IOPS, sequential 128 KiB writes about -17%.
fn disk_queues(vcpus: usize) -> usize {
    let max = crate::virtio::MAX_QUEUES;
    let asked = std::env::var("NATEMU_BLK_QUEUES")
        .ok()
        .and_then(|v| v.parse::<usize>().ok());
    asked.unwrap_or(vcpus).clamp(1, max)
}

/// How a virtio device is attached to the guest.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Transport {
    #[default]
    Mmio,
    Pci,
}
/// The transport of each virtio device (naming one that is not enabled does nothing).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Transports {
    pub disk: Transport,
    pub net: Transport,
    pub rng: Transport,
    pub console: Transport,
}
impl Transports {
    pub const fn all(t: Transport) -> Self {
        Self {
            disk: t,
            net: t,
            rng: t,
            console: t,
        }
    }
}
/// PCI slots of the first disk, the network device and the entropy device (00:01.0, 00:02.0, 00:03.0); more disks follow.
const BLOCK_SLOT: u8 = 1;
const NET_SLOT: u8 = 2;
const RNG_SLOT: u8 = 3;

/// One disk of the guest: the list order is the order the guest names them (`/dev/vda`, `vdb`, ...).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DiskSpec {
    pub id: String,
    /// The disk file: a writable one is locked exclusively for this VM's lifetime (excludes other writers/truncators); a read-only one takes a shared lock.
    pub path: PathBuf,
    pub transport: Transport,
    /// Opened read-only: shared lock, the guest sees the read-only bit and no flush, discard or write-zeroes.
    pub readonly: bool,
    /// The base of a qcow2 overlay at `path` (read-only, shared lock). A writable `path` that does not exist is created
    /// as a new overlay over it; an existing one must name this same file as its backing file.
    pub backing: Option<PathBuf>,
    /// Throw-away writes (QEMU's snapshot=on): `path` is the read-only base of a new overlay in the temporary folder,
    /// deleted as soon as it is open, so the guest's writes vanish when the VM exits. Not with `readonly` or `backing`.
    pub snapshot: bool,
}

impl DiskSpec {
    /// Its image (a new overlay created first when `backing` names a base and a writable `path` does not exist).
    fn open(&self) -> Result<crate::disk::Image> {
        if self.snapshot {
            if self.readonly || self.backing.is_some() {
                return Err(format!(
                    "disk {}: snapshot goes with neither readonly nor backing",
                    self.id
                ));
            }
            return crate::disk::open_snapshot(&self.id, &self.path);
        }
        crate::disk::open_disk(&self.id, &self.path, self.readonly, self.backing.as_deref())
    }
}

/// A virtio console ([`crate::virtio::Console`]): a second guest console, `/dev/hvc0` for the first.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConsoleSpec {
    pub id: String,
    pub backend: ConsoleBackend,
    pub transport: Transport,
}
/// Where a console's bytes go.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ConsoleBackend {
    /// What the guest writes is appended to this file (created when missing); the guest receives nothing.
    Output(PathBuf),
    /// A Unix-domain socket natemu listens on (removed at exit), one client at a time: what the client sends the guest
    /// receives; what the guest writes goes to the client, or is discarded while none is connected.
    Socket(PathBuf),
}

/// GET_ID serials are a file's device and inode in hex, so two read-only disks on one file would share one. A repeat
/// gets a non-hex first character ('g', 'h', ...: never any file's serial) by its place in the list; the first use,
/// and so every launch that names each file once, keeps the file's serial.
fn unique_serial(id: &mut [u8; 20], used: &mut Vec<[u8; 20]>) {
    const MARKS: &[u8] = b"ghijklmnopqrstuvwxyzGHIJKLMNOPQRSTUVWXYZ";
    let mut marks = MARKS.iter();
    while used.contains(id) {
        id[0] = *marks.next().expect("fewer disks than marks");
    }
    used.push(*id);
}

/// The most MMIO disks: each holds an exclusive interrupt line (16, then 19-23), and the controller has lines 16-23.
const MAX_MMIO_DISKS: usize = 6;
/// The most PCI disks: slots 1 and 4-31 (slot 0 is the host bridge, 2 and 3 the network and entropy devices).
const MAX_PCI_DISKS: usize = 29;
/// PCI functions share INTx lines (design step 6, as real PCI). The documented fallback is `false`: each PCI function
/// then takes a line no other device holds, which refuses a launch once lines 16-23 run out (6 disks with the NIC and
/// the entropy device).
const SHARED_INTX: bool = true;

/// Which devices a launch has and on what transport. The input of [`place`].
struct Wanted {
    rng: Option<Transport>,
    disks: Vec<Transport>,
    net: Option<Transport>,
    /// Each console's id (for messages) and transport.
    consoles: Vec<(String, Transport)>,
    /// The test function ([`Boot::pci_testdev`]).
    testdev: bool,
    /// The mapped-memory test function ([`Boot::mapped_testdev`]).
    mapped_testdev: bool,
    /// The display ([`Boot::display`]).
    display: bool,
}
#[cfg(test)]
impl Wanted {
    /// The old one-of-each form: `enabled` is [rng, disk, net].
    fn old(enabled: [bool; 3], t: Transports) -> Self {
        let [rng, disk, net] = enabled;
        Self {
            rng: rng.then_some(t.rng),
            disks: if disk { vec![t.disk] } else { vec![] },
            net: net.then_some(t.net),
            consoles: vec![],
            testdev: false,
            mapped_testdev: false,
            display: false,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    Rng,
    Disk(usize),
    Net,
    Console(usize),
    /// The test function's two functions ([`crate::pci::testdev`]): no virtio device, no INTx line.
    TestDev,
    /// The mapped-memory test function ([`crate::pci::mapped_testdev`]): function 0 of its slot, no INTx line.
    MappedTestDev,
    /// The display ([`crate::display`]): function 0 of its slot, no INTx line.
    Display,
}
/// Where one device lives: both the ACPI tables and the device wiring in `prepare` read this, so they cannot disagree.
#[derive(Clone, Copy, Debug)]
struct Placed {
    kind: Kind,
    pci: bool,
    /// PCI slot (used when `pci`).
    slot: u8,
    /// MMIO page, ACPI uid and interrupt line (used when not `pci`).
    res: boot::acpi::VirtioMmio,
    /// The PCI function's INTx line (used when `pci`), possibly shared with other PCI functions, never with an MMIO device.
    intx: u32,
}

/// The placement rule (design section 3). First disk, NIC, entropy: PCI slots 1, 2, 3, pages 0xF000_0000/1000/2000, lines
/// 16/17/18, uids 0/1/2. Disks: the m-th disk on MMIO takes page 0xF000_0000 (m = 0) or 0xF000_0000 + (2 + m) * 4 KiB,
/// uid 0 or 2 + m and the exclusive line 16 or 18 + m; the p-th disk on PCI takes slot 1 (p = 0) or 3 + p; both counted
/// among the disks of their own transport, so a mixed list leaves no gaps. PCI INTx lines: slots 1-3 keep 16/17/18 (old
/// launches unchanged) unless an MMIO device holds that line; the other PCI functions take, round robin in slot order,
/// the lines 16-23 that no MMIO device holds, so they share lines with each other ([`SHARED_INTX`]), never with an MMIO
/// device. Every line is inside 16-23, as `Virtio::new` asserts. Consoles come after the disks: the k-th console on
/// MMIO takes page and uid 2 + max(m, 1) + k (m the MMIO disks) and the first line of 16-23 no MMIO device holds; the
/// k-th on PCI takes slot 3 + max(p, 1) + k (p the PCI disks); no line or slot left is a launch error naming the
/// console. Returned in wiring order: entropy, disk 0, network, disks 1 and up, the consoles, then the test function
/// ([`Wanted::testdev`]) at the lowest slot no other function holds, the consoles' included (its functions 0 and 1; no
/// INTx line, no `_PRT` entry); none free is an error. Consoles are placed first, so the test function never moves a
/// console, and without consoles it takes the slot it took before them. The mapped-memory test function
/// ([`Wanted::mapped_testdev`]) comes next, at the lowest slot still free (its function 0; no INTx line, no `_PRT` entry),
/// and the display ([`Wanted::display`]) last, the same way. Each later one takes a slot no earlier one wanted, so a
/// launch without the display (every launch before it existed) keeps every slot it had, and turning the display on
/// moves no other function.
///
/// Per-disk transports exist only in the library: the CLI and the config file have one `transport.disk` for all disks
/// (`Boot::transports.disk` is read only there, to fill each `DiskSpec::transport`). Which names (`vda`..) Linux gives
/// when MMIO and PCI disks are mixed is unverified; the VM test covers each transport on its own.
fn place(w: &Wanted) -> Result<Vec<Placed>> {
    place_lines(w, SHARED_INTX)
}
/// [`place`] with PCI functions sharing INTx lines (`shared`) or each on a line of its own (the fallback).
fn place_lines(w: &Wanted, shared: bool) -> Result<Vec<Placed>> {
    use crate::virtio::{BLOCK_RESOURCE, NET_RESOURCE, RNG_RESOURCE};
    let pci = |t: Transport| t == Transport::Pci;
    let on_pci_disks = w.disks.iter().filter(|&&t| pci(t)).count();
    if w.disks.len() - on_pci_disks > MAX_MMIO_DISKS {
        return Err(format!(
            "at most {MAX_MMIO_DISKS} disks on mmio (interrupt lines 16-23); use pci for more"
        ));
    }
    if on_pci_disks > MAX_PCI_DISKS {
        return Err(format!(
            "PCI slots 1-31 are all used: at most {MAX_PCI_DISKS} disks on pci"
        ));
    }
    let one = |kind, t: Option<Transport>, slot, res: boot::acpi::VirtioMmio| {
        t.map(|t| Placed {
            kind,
            pci: pci(t),
            slot,
            res,
            intx: 0,
        })
    };
    let (mut mmio, mut on_pci) = (0u32, 0u8);
    let mut disk = |k: usize| {
        let t = pci(w.disks[k]);
        let m = mmio;
        mmio += !t as u32;
        let res = if t || m == 0 {
            BLOCK_RESOURCE
        } else {
            boot::acpi::VirtioMmio {
                uid: (2 + m) as u8,
                base: 0xf000_0000 + (2 + m) * 4096,
                gsi: 18 + m,
            }
        };
        let p = on_pci;
        on_pci += t as u8;
        Placed {
            kind: Kind::Disk(k),
            pci: t,
            slot: if p == 0 { BLOCK_SLOT } else { 3 + p },
            res,
            intx: 0,
        }
    };
    let mut out: Vec<_> = one(Kind::Rng, w.rng, RNG_SLOT, RNG_RESOURCE)
        .into_iter()
        .collect();
    out.extend((!w.disks.is_empty()).then(|| disk(0)));
    out.extend(one(Kind::Net, w.net, NET_SLOT, NET_RESOURCE));
    for k in 1..w.disks.len() {
        out.push(disk(k));
    }
    // The next console page (and uid) and slot: after the disks'.
    let mut page = 2 + (w.disks.len() - on_pci_disks).max(1) as u32;
    let mut slot = 3 + on_pci_disks.max(1);
    for (k, (id, t)) in w.consoles.iter().enumerate() {
        let mut p = Placed {
            kind: Kind::Console(k),
            pci: pci(*t),
            slot: 0,
            res: BLOCK_RESOURCE,
            intx: 0,
        };
        if p.pci {
            p.slot = u8::try_from(slot).ok().filter(|&s| s <= 31).ok_or_else(|| {
                format!("console {id:?}: PCI slots 1-31 are all used; use mmio or fewer disks on pci")
            })?;
            slot += 1;
        } else {
            let held: Vec<u32> = out.iter().filter(|p| !p.pci).map(|p| p.res.gsi).collect();
            let gsi = (16..24).find(|l| !held.contains(l)).ok_or_else(|| {
                format!("console {id:?}: no interrupt line left on mmio (lines 16-23 are all held); use pci")
            })?;
            p.res = boot::acpi::VirtioMmio {
                uid: page as u8,
                base: 0xf000_0000 + page * 4096,
                gsi,
            };
            page += 1;
        }
        out.push(p);
    }
    // INTx lines. A slot 1-3 function keeps its old line unless an MMIO device holds it.
    let held: Vec<u32> = out.iter().filter(|p| !p.pci).map(|p| p.res.gsi).collect();
    let fixed = |p: &Placed| {
        let line = 15 + u32::from(p.slot);
        (p.slot <= RNG_SLOT && !held.contains(&line)).then_some(line)
    };
    let mut taken = held.clone();
    if !shared {
        taken.extend(out.iter().filter(|p| p.pci).filter_map(fixed));
    }
    let pool: Vec<u32> = (16..24).filter(|l| !taken.contains(l)).collect();
    let mut next = 0;
    let mut by_slot: Vec<_> = out.iter_mut().filter(|p| p.pci).collect();
    by_slot.sort_unstable_by_key(|p| p.slot);
    for p in by_slot {
        p.intx = match fixed(p) {
            Some(line) => line,
            None => {
                let i = if shared {
                    next % pool.len().max(1)
                } else {
                    next
                };
                next += 1;
                *pool.get(i).ok_or_else(|| {
                    format!(
                        "no interrupt line left for PCI slot {} (lines 16-23 are all held)",
                        p.slot
                    )
                })?
            }
        };
    }
    for (on, kind, key) in [
        (w.testdev, Kind::TestDev, "debug.pci_testdev"),
        (w.mapped_testdev, Kind::MappedTestDev, "debug.mapped_testdev"),
        (w.display, Kind::Display, "display.device"),
    ] {
        if !on {
            continue;
        }
        let held: Vec<u8> = out.iter().filter(|p| p.pci).map(|p| p.slot).collect();
        let slot = (1..32u8)
            .find(|s| !held.contains(s))
            .ok_or(format!("{key}: PCI slots 1-31 are all used"))?;
        out.push(Placed {
            kind,
            pci: true,
            slot,
            // Unused: `res` is read only off PCI, and the test functions and the display have no INTx line.
            res: boot::acpi::VirtioMmio {
                uid: 0,
                base: 0,
                gsi: 0,
            },
            intx: 0,
        });
    }
    Ok(out)
}

/// What the placed devices give the guest: the memory map and the ACPI tables, with the lists the tables were built
/// from. Pure (no host state), so a launch can be pinned by a test.
struct LaunchPlan {
    /// ACPI lists each device once: virtio-MMIO devices here, PCI functions below as (slot, GSI).
    #[allow(dead_code)] // read by the pinning test
    manifest: Vec<boot::acpi::VirtioMmio>,
    #[allow(dead_code)]
    functions: Vec<(u8, u32)>,
    /// The root bridge's `_CRS` declares the I/O window: the bus must register it ([`crate::pci::Bus::io_window`]).
    io_window: bool,
    tables: boot::acpi::Tables,
    layout: Layout,
}

/// The memory map for `ram` bytes; `Err` when it is not a valid one.
fn layout_of(placed: &[Placed], ram: u64) -> Result<Layout> {
    let layout = Layout {
        low_end: ram.min(exec::LOW_RAM_END),
        high: ram.saturating_sub(exec::LOW_RAM_END),
        pci: placed.iter().any(|p| p.pci),
    };
    layout.e820().map_err(|e| e.to_string())?;
    Ok(layout)
}
#[cfg(test)]
fn launch_layout(enabled: [bool; 3], t: Transports, ram: u64) -> Result<Layout> {
    layout_of(&place(&Wanted::old(enabled, t))?, ram)
}

/// The ACPI tables (with the mailbox) and the lists they are built from, for the tests ([`plan_tables_with`]).
#[cfg(test)]
fn plan_tables(vcpus: usize, rsdp: u64, placed: &[Placed], ram: u64) -> Result<LaunchPlan> {
    plan_tables_with(vcpus, rsdp, placed, ram, true, boot::acpi::Board::default())
}
/// The ACPI tables and the lists they are built from (panics on a vCPU count outside 1..=254, as `build_with_devices`);
/// `mailbox` false leaves the multiprocessor wakeup mailbox out ([`Boot::mailbox`]); `board` names the optional platform
/// devices the DSDT lists.
fn plan_tables_with(
    vcpus: usize,
    rsdp: u64,
    placed: &[Placed],
    ram: u64,
    mailbox: bool,
    board: boot::acpi::Board,
) -> Result<LaunchPlan> {
    let layout = layout_of(placed, ram)?;
    let (mut manifest, mut functions) = (Vec::new(), Vec::new());
    for p in placed {
        if matches!(p.kind, Kind::TestDev | Kind::MappedTestDev | Kind::Display) {
            // no INTx line: no `_PRT` entry
        } else if p.pci {
            functions.push((p.slot, p.intx));
        } else {
            manifest.push(p.res);
        }
    }
    functions.sort_unstable();
    // Only the test function has I/O BARs.
    let io_window = placed.iter().any(|p| p.kind == Kind::TestDev);
    let root = boot::acpi::RootBridge {
        functions: &functions,
        io_window,
    };
    let tables = boot::acpi::build_tables(
        vcpus,
        rsdp,
        &manifest,
        layout.pci.then_some(root),
        mailbox,
        board,
    );
    Ok(LaunchPlan {
        manifest,
        functions,
        io_window,
        tables,
        layout,
    })
}
#[cfg(test)]
fn launch_plan(
    vcpus: usize,
    rsdp: u64,
    enabled: [bool; 3],
    t: Transports,
    ram: u64,
) -> Result<LaunchPlan> {
    plan_tables(vcpus, rsdp, &place(&Wanted::old(enabled, t))?, ram)
}

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
    /// Each vCPU's shadow-table pool, in pages (`machine.shadow_pool_pages`; default the whole reservation,
    /// `shadow::POOL_MAX_PAGES`; at least `shadow::POOL_MIN_PAGES`).
    pub shadow_pool_pages: usize,
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
    /// Page-currency plan chunk 1: `Compiler::page_currency` (default on; `--page-currency` / `NATEMU_PAGE_CURRENCY`).
    pub page_currency: u64,
    /// Fault-around plan chunk 1: `Compiler::writable_on_read` (default on; `--writable-on-read` / `NATEMU_WRITABLE_ON_READ`).
    pub writable_on_read: bool,
    /// Fault-around plan chunk 2: `Compiler::fault_around` (default 16 pages; `--fault-around` / `NATEMU_FAULT_AROUND`).
    pub fault_around: u64,
    /// Gather plan: `Compiler::inline_gather` (default on; the CLI's `[debug] inline_gather` or `--set debug.inline_gather`; `NATEMU_INLINE_GATHER` overrides it in `execution::run`).
    pub inline_gather: bool,
    /// Debug: admission's code-shape check on every object (`Compiler::shape`; the test driver's `boot-linux --code-shape`).
    pub code_shape: bool,
    /// PCID plan P1 (owner, November 5): turn on the resident's page-walk speculation barriers. Off by default; on, the
    /// host must have FEAT_SB (checked in [`Boot::prepare`]) and IA32_ARCH_CAPABILITIES advertises RDCL_NO when the host
    /// also has FEAT_CSV3, so a default command line then boots with PTI off. `--speculation-barriers` / `NATEMU_SPECULATION_BARRIERS`.
    pub speculation_barriers: bool,
    /// AFP accurate plan chunk 1: accurate floating-point mode (`Compiler::accurate_fp`; `--fp accurate` / `NATEMU_FP`). Default fast.
    pub accurate_fp: bool,
    /// Expose the modern virtio entropy device (native entropy, copied completions).
    pub rng: bool,
    /// The disks, in the order the guest sees them (each with its own transport; `transports.disk` is not read here).
    pub disks: Vec<DiskSpec>,
    /// Expose the virtio network device on user-mode NAT ([`crate::virtio::Slirp`]).
    pub net: bool,
    /// Static localhost forwards and explicit host-loopback access; requires `net`.
    pub net_options: crate::virtio::SlirpOptions,
    /// The virtio consoles (each with its own transport; `transports.console` is not read here).
    pub consoles: Vec<ConsoleSpec>,
    /// The shared template pool (cross-vCPU reuse plan): back a read-only span of about 274 MiB of address space before guest
    /// RAM (pages are backed as touched), map it into every vCPU, publish admitted templates into it and let each resident look
    /// in it before asking the host. On by default (the gate passed, November 14: configure -3.3%, make -1.8% wall);
    /// `--pool=false` / `NATEMU_POOL=0` turn it off.
    pub pool: bool,
    /// Per device, virtio-MMIO (default) or virtio-PCI.
    pub transports: Transports,
    /// X4: the MADT's multiprocessor wakeup mailbox, on which every AP waits (default on). Off (boot-interpreter plan
    /// section 6), the MADT has none and each AP starts in the INIT state ([`natemu_abi::power_on::init`]) waiting for a
    /// start-up IPI, as firmware leaves the processors it did not boot: Linux then starts them with INIT and SIPI. A
    /// Multiboot kernel's APs always start so (it gets no ACPI tables, [`boot::multiboot`]). Each such AP is published
    /// as waiting for a SIPI before any vCPU runs ([`execution::wait_for_sipi`]), and waits in the resident for one;
    /// the SIPI starts it in the boot-time interpreter, which runs its real-mode start (Linux's trampoline) up to its
    /// hand-off to 64-bit code (`tests/boot` case `boot-startup`: Debian's kernel at 2 and 4 vCPUs, CPUs offline and
    /// online again, the shutdown).
    pub mailbox: bool,
    /// Test only (kvm-unit-tests' exit, plan section 7): QEMU's isa-debug-exit device at ports F4H-F7H
    /// ([`crate::debug_exit`]): a write ends the run with `Stop::Power(pm::DEBUG_EXIT)`, the value in
    /// [`Prepared::debug_exit`]. Default off.
    pub debug_exit: bool,
    /// The pvpanic device at port 505H ([`crate::pvpanic`]), listed in the DSDT as `\_SB.PEVT` (a firmware boot has no
    /// tables but the device): the guest's PANICKED write ends the run with `Stop::Power(pm::PANIC)`; its CRASH_LOADED
    /// writes are counted in [`Prepared::pvpanic`]. Default off (the `natemu` CLI turns it on, `machine.pvpanic`).
    pub pvpanic: bool,
    /// SMBIOS 3.0 tables ([`boot::smbios`], platform-devices plan): the structure table after the ACPI tables in the ACPI
    /// NVS range, the entry point in a read-only legacy ROM page at F0000H (`execution::Backing::write_legacy_rom`), where
    /// the guest's DMI scan finds it. A bzImage boot only: a Multiboot image gets no tables, and a firmware boot gets its
    /// SMBIOS from its firmware. Default off (the `natemu` CLI turns it on, `machine.smbios`).
    pub smbios: bool,
    /// The 8042 keyboard controller with a PS/2 keyboard and mouse ([`crate::ps2`]) at ports 60H and 64H, IRQ 1 and 12,
    /// listed in the DSDT as `\_SB.KBD_` and `\_SB.MOU_` with the FADT's 8042 bit (a firmware boot has no tables but the
    /// device); its host input handle is [`Prepared::ps2`]. Without it port 64H is `pm`'s reset-only register. Default
    /// off (the `natemu` CLI turns it on, `machine.ps2`).
    pub ps2: bool,
    /// Test only (`[debug] pci_testdev`): the PCI test function ([`crate::pci::testdev`], QEMU's `pci-testdev`) as functions
    /// 0 and 1 of the lowest free PCI slot, with the I/O window it needs; it installs the bus even when every virtio device
    /// is on MMIO. Default off.
    pub pci_testdev: bool,
    /// Test only (`[debug] mapped_testdev`): the mapped-memory test function ([`crate::pci::mapped_testdev`]: BAR0 2 MiB
    /// of mapped device memory, BAR2 its control page) as function 0 of the lowest PCI slot still free; it installs the bus
    /// even when every virtio device is on MMIO. Default off.
    pub mapped_testdev: bool,
    /// The display (`[display] device = "bochs"`, [`crate::display`]): QEMU's `bochs-display` as function 0 of the lowest
    /// PCI slot still free (after the test functions), no INTx line; BAR0 its framebuffer of `vram_mib` MiB of mapped
    /// device memory, the EDID's preferred mode `width` x `height` ([`crate::display::Spec::check`] refuses others). It
    /// installs the bus even when every virtio device is on MMIO. Default none.
    pub display: Option<crate::display::Spec>,
    /// EL0 sampler (docs/restructure/el0-sampler-plan.md; default none): ring each vCPU's sample bell and write
    /// `NAME.samples` and `NAME.objects` ([`crate::sampler`]), with the guest's control port at F8H-FBH
    /// ([`crate::profile_port`]). Only a resident built with its `profile` feature samples; others are never rung.
    pub profile: Option<crate::sampler::Options>,
    /// M2 Part 2, the firmware boot mode (docs/restructure/firmware-mode.md; default none): boot this firmware image instead
    /// of a kernel. It lies at guest-physical [4 GiB - its size, 4 GiB), read and fetched as RAM, its stores dropped (x86's
    /// ROM); vCPU 0 starts in the SDM's power-on state at the reset vector ([`boot::firmware::Start`]), the others in the
    /// power-on state waiting for a SIPI, all with the MTRRs at their power-on values. Nothing else is loaded (`kernel`,
    /// `initrd` and `cmdline` must be empty: no ACPI tables, no e820 map, no mailbox) and the board has no virtio device.
    pub firmware: Option<boot::firmware::Firmware>,
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
            shadow_pool_pages: natemu_abi::shadow::POOL_MAX_PAGES,
            serial: None,
            strict_unassigned: false,
            lost_ticks: clock::LostTicks::DEFAULT,
            watchdog: Duration::from_secs(60),
            max_block: None,
            stop_at: None,
            compile_cache: compile_cache::Config::OFF,
            check_reuse: false,
            page_currency: exec::cache::PAGES_ON,
            writable_on_read: true,
            fault_around: natemu_abi::fault_around::DEFAULT_WINDOW,
            inline_gather: true,
            code_shape: false,
            speculation_barriers: false,
            accurate_fp: false,
            rng: false,
            disks: Vec::new(),
            net: false,
            net_options: crate::virtio::SlirpOptions::default(),
            consoles: Vec::new(),
            pool: true,
            transports: Transports::all(Transport::Mmio),
            mailbox: true,
            debug_exit: false,
            pvpanic: false,
            smbios: false,
            ps2: false,
            pci_testdev: false,
            mapped_testdev: false,
            display: None,
            profile: None,
            firmware: None,
        }
    }

    /// Where each device goes ([`place`]).
    fn placed(&self) -> Result<Vec<Placed>> {
        let t = self.transports;
        place(&Wanted {
            rng: self.rng.then_some(t.rng),
            disks: self.disks.iter().map(|d| d.transport).collect(),
            net: self.net.then_some(t.net),
            consoles: self
                .consoles
                .iter()
                .map(|c| (c.id.clone(), c.transport))
                .collect(),
            testdev: self.pci_testdev,
            mapped_testdev: self.mapped_testdev,
            display: self.display.is_some(),
        })
    }

    /// The optional platform devices the DSDT lists ([`boot::acpi::Board`]).
    fn board(&self) -> boot::acpi::Board {
        boot::acpi::Board {
            pvpanic: self.pvpanic,
            ps2: self.ps2,
        }
    }

    /// The memory map and the ACPI tables [`Boot::prepare`] would load, built from the device set alone: nothing is
    /// opened or allocated (the test driver's `boot-linux --load-only`).
    pub fn tables(&self) -> Result<(Layout, boot::acpi::Tables)> {
        let plan = plan_tables_with(
            self.vcpus,
            self.rsdp,
            &self.placed()?,
            self.ram,
            self.mailbox,
            self.board(),
        )?;
        Ok((plan.layout, plan.tables))
    }

    /// Loads everything into a new backing and builds the devices; nothing runs yet.
    pub fn prepare(self) -> Result<Prepared> {
        if self.firmware.is_some() {
            return self.prepare_firmware();
        }
        if !self.net && (self.net_options.host_loopback || !self.net_options.forwards.is_empty()) {
            return Err("network: forwarding and host-loopback options require --net".into());
        }
        self.net_options.validate()?;
        if let Some(spec) = &self.display {
            spec.check()?;
        }
        // PCID plan P1: the barriers' leaf SB needs FEAT_SB (the vCPU's ID_AA64ISAR1_EL1); refuse to turn them on without it
        // (default off needs nothing).
        if self.speculation_barriers && !crate::probe::sb()? {
            return Err(
                "--speculation-barriers: this host lacks FEAT_SB (ID_AA64ISAR1_EL1.SB)"
                    .into(),
            );
        }
        let placed = self.placed()?;
        layout_of(&placed, self.ram)?; // before require_vcpus and before allocating the RAM
        require_vcpus(self.vcpus)?;
        let backing = execution::prepare_with_firmware(
            &self.runtime.to_string_lossy(),
            self.code_bytes,
            self.vcpus,
            self.ram as usize,
            self.pool,
            &[],
            self.shadow_pool_pages,
        )?;
        let plan = plan_tables_with(
            self.vcpus,
            self.rsdp,
            &placed,
            self.ram,
            self.mailbox,
            self.board(),
        )?;
        let (layout, tables, io_window) = (plan.layout, plan.tables, plan.io_window);
        let smbios = (self.smbios && !boot::is_multiboot(&self.kernel))
            .then(|| boot::smbios::for_board(self.vcpus, layout, &tables));
        let inputs = Inputs {
            image: &self.kernel,
            cmdline: &self.cmdline,
            initrd: self.initrd.as_deref(),
            acpi: Some(&tables),
            smbios: smbios.as_ref(),
        };
        let loaded = boot::load_kernel(&backing.ram, &layout, &inputs).map_err(|e| e.to_string())?;
        if let Some(s) = &smbios {
            backing.write_legacy_rom(boot::smbios::ENTRY_POINT, &s.entry);
        }
        execution::guest_ram_written(&backing.memory);
        let lost = self.lost_ticks;
        // The APs: on the mailbox (a bzImage with it), else in the INIT state waiting for a SIPI (`mailbox` off, or a
        // Multiboot kernel, whose `ap` is that state already).
        let mailbox = self.mailbox && loaded.setup.is_some();
        let mut ap = loaded.ap;
        if !mailbox && loaded.setup.is_some() {
            natemu_abi::power_on::init(&mut ap);
        }
        for v in 0..self.vcpus {
            execution::set_cpu(
                backing.vcpu(v),
                if v == 0 { loaded.cpu } else { ap },
                self.speculation_barriers,
            )?;
            execution::set_lapic(
                backing.vcpu(v),
                natemu_abi::lapic::Lapic {
                    reinject: u64::from(lost.lapic),
                    ..natemu_abi::lapic::RESET
                },
            );
            if v > 0 && mailbox {
                let mut state: State = backing.vcpu(v).read_record(exec::STATE_OFFSET);
                state.mailbox = tables.mailbox;
                backing.vcpu(v).write_record(exec::STATE_OFFSET, state);
            } else if v > 0 {
                execution::wait_for_sipi(backing.vcpu(v));
            }
        }
        // L3: COM1 and the unassigned rule.
        let uart = Arc::new(Mutex::new(uart::Uart::new()));
        uart.lock().unwrap().sink = self.serial;
        let mut devices = devices::Devices::new(self.ram);
        // L4a: the I/O APIC, the 8259 pair and their wires.
        let irqs = crate::irq::install(&mut devices, &backing.memory, self.vcpus, &uart)?;
        // L4b: the PIT and port 61H, the RTC, the ACPI PM block and reset register (and port 64H without the 8042).
        let board = clock::install(&mut devices, &irqs, lost, &uart, self.ps2)?;
        // Beside the PM block: the pvpanic device (`Boot::pvpanic`) and the 8042 (`Boot::ps2`).
        let pvpanic = self
            .pvpanic
            .then(|| crate::pvpanic::install(&mut devices))
            .transpose()?;
        let ps2 = self
            .ps2
            .then(|| crate::ps2::install(&mut devices, &irqs))
            .transpose()?;
        let debug_exit = if self.debug_exit {
            let (device, value) = crate::debug_exit::DebugExit::new(devices.power.clone());
            devices.register_ports(
                crate::debug_exit::PORT.into(),
                crate::debug_exit::PORTS,
                Arc::new(Mutex::new(device)),
            )?;
            Some(value)
        } else {
            None
        };
        let sampler = sampler(&self.profile, self.vcpus, &backing, &mut devices)?;
        let mut virtio = Vec::new();
        let mut serials = Vec::new();
        let mut bus = crate::pci::Bus::new();
        let mut mapped = crate::pci::Mapped { regions: Vec::new(), publishes: Arc::default() };
        let mut net = None;
        let mut display = None;
        for p in &placed {
            if p.kind == Kind::TestDev {
                bus.add(p.slot, 0, crate::pci::testdev::TestDev::new(0))?;
                bus.add(p.slot, 1, crate::pci::testdev::TestDev::new(1))?;
                continue;
            }
            if p.kind == Kind::MappedTestDev {
                bus.add(p.slot, 0, crate::pci::mapped_testdev::MappedTestDev::new()?)?;
                continue;
            }
            if p.kind == Kind::Display {
                let spec = self.display.expect("placed only with a display");
                let vram = crate::pci::DeviceMemory::new(spec.vram_mib << 20)?;
                let d = crate::display::Display::new(vram, (spec.width, spec.height));
                bus.add(p.slot, 0, d.clone())?;
                display = Some(d);
                continue;
            }
            let gsi = if p.pci { p.intx } else { p.res.gsi } as usize;
            let v = match p.kind {
                Kind::Rng => crate::virtio::Virtio::rng(irqs.clone(), gsi),
                Kind::Disk(k) => {
                    let mut image = self.disks[k].open()?;
                    unique_serial(&mut image.id, &mut serials);
                    let disk = crate::virtio::Virtio::block_image(irqs.clone(), gsi, image)?;
                    if p.pci {
                        // One request queue per vCPU: the guest then takes each completion on the vCPU that sent the request.
                        disk.block_queues(disk_queues(self.vcpus))?;
                    }
                    disk
                }
                Kind::TestDev | Kind::MappedTestDev | Kind::Display => unreachable!("added above"),
                Kind::Net => {
                    let (v, port) = crate::virtio::Virtio::network(
                        irqs.clone(),
                        gsi,
                        crate::virtio::Slirp::MAC,
                        crate::virtio::Slirp::OFFLOADS,
                    )?;
                    net = Some(crate::virtio::Slirp::new(port, &self.net_options)?);
                    v
                }
                Kind::Console(k) => {
                    let c = &self.consoles[k];
                    let console = match &c.backend {
                        ConsoleBackend::Output(path) => crate::virtio::Console::file(path),
                        ConsoleBackend::Socket(path) => {
                            crate::virtio::Console::socket(path, irqs.clone())
                        }
                    }
                    .map_err(|e| format!("console {:?}: {e}", c.id))?;
                    crate::virtio::Virtio::model(irqs.clone(), gsi, console)
                }
            };
            if p.pci {
                bus.add(p.slot, 0, v.pci(p.slot))?;
            } else {
                devices.register(p.res.base.into(), 4096, v.device())?;
            }
            virtio.push(v);
        }
        if layout.pci {
            if bus.io_window() != io_window {
                return Err(format!(
                    "internal error: the ACPI tables say the I/O window is {}, the PCI bus {}",
                    ["absent", "present"][usize::from(io_window)],
                    ["absent", "present"][usize::from(bus.io_window())]
                ));
            }
            // Mapped BARs publish into the backing's region table; their device memory goes to `Backing::machine` in the
            // bus's order, which is the table's.
            bus.publish_to(Arc::new(backing.mapped_table()));
            mapped = bus.install(&mut devices)?;
        }
        devices.strict = self.strict_unassigned;
        Ok(Prepared {
            backing,
            ram: self.ram,
            mapped: mapped.regions,
            mapped_publishes: mapped.publishes,
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
            launch: crate::probe::Launch {
                speculation_barriers: self.speculation_barriers,
                accurate_fp: self.accurate_fp,
                pool: self.pool,
                compile_cache: self.compile_cache.dir.clone().map(|d| (d, self.compile_cache.cap)),
            },
            compile_cache: self.compile_cache,
            check_reuse: self.check_reuse,
            page_currency: self.page_currency,
            writable_on_read: self.writable_on_read,
            fault_around: self.fault_around,
            inline_gather: self.inline_gather,
            code_shape: self.code_shape,
            accurate_fp: self.accurate_fp,
            ran: AtomicBool::new(false),
            cancel: Arc::default(),
            serial_input: Mutex::new(None),
            debug_exit,
            pvpanic,
            ps2,
            display,
            sampler,
        })
    }

    /// [`Boot::prepare`] of a firmware boot ([`Boot::firmware`]): the image in a new backing, every vCPU at power-on (vCPU 0
    /// at its start, the others waiting for a SIPI), the board of a kernel boot without virtio devices or a PCI bus.
    fn prepare_firmware(mut self) -> Result<Prepared> {
        let firmware = self.firmware.take().unwrap();
        if !self.kernel.is_empty() || self.initrd.is_some() || !self.cmdline.is_empty() {
            return Err("a firmware boot loads no kernel, initrd or command line".into());
        }
        if self.rng || self.net || !self.disks.is_empty() || !self.consoles.is_empty() {
            return Err("a firmware boot has no virtio devices (no disk, network, entropy device or console)".into());
        }
        if self.pci_testdev {
            return Err("a firmware boot has no PCI bus (debug.pci_testdev)".into());
        }
        if self.mapped_testdev {
            return Err("a firmware boot has no PCI bus (debug.mapped_testdev)".into());
        }
        if self.display.is_some() {
            return Err("a firmware boot has no PCI bus (display.device)".into());
        }
        if !exec::firmware_size(firmware.image.len() as u64) {
            return Err(format!(
                "firmware image of {} bytes: whole 4 KiB pages, at most {} MiB",
                firmware.image.len(),
                exec::MAX_FIRMWARE_BYTES >> 20
            ));
        }
        if self.speculation_barriers && !crate::probe::sb()? {
            return Err(
                "--speculation-barriers: this host lacks FEAT_SB (ID_AA64ISAR1_EL1.SB)"
                    .into(),
            );
        }
        layout_of(&[], self.ram)?; // the RAM size's checks, before allocating it
        require_vcpus(self.vcpus)?;
        let backing = execution::prepare_with_firmware(
            &self.runtime.to_string_lossy(),
            self.code_bytes,
            self.vcpus,
            self.ram as usize,
            self.pool,
            &firmware.image,
            self.shadow_pool_pages,
        )?;
        let loaded = boot::firmware::loaded(&firmware);
        let lost = self.lost_ticks;
        for v in 0..self.vcpus {
            execution::set_cpu_at_power_on(
                backing.vcpu(v),
                if v == 0 { loaded.cpu } else { loaded.ap },
                self.speculation_barriers,
            )?;
            execution::set_lapic(
                backing.vcpu(v),
                natemu_abi::lapic::Lapic {
                    reinject: u64::from(lost.lapic),
                    ..natemu_abi::lapic::RESET
                },
            );
            if v > 0 {
                execution::wait_for_sipi(backing.vcpu(v));
            }
        }
        let uart = Arc::new(Mutex::new(uart::Uart::new()));
        uart.lock().unwrap().sink = self.serial.take();
        let mut devices = devices::Devices::new(self.ram);
        let irqs = crate::irq::install(&mut devices, &backing.memory, self.vcpus, &uart)?;
        let board = clock::install(&mut devices, &irqs, lost, &uart, self.ps2)?;
        // Beside the PM block: the pvpanic device (`Boot::pvpanic`) and the 8042 (`Boot::ps2`).
        let pvpanic = self
            .pvpanic
            .then(|| crate::pvpanic::install(&mut devices))
            .transpose()?;
        let ps2 = self
            .ps2
            .then(|| crate::ps2::install(&mut devices, &irqs))
            .transpose()?;
        let debug_exit = if self.debug_exit {
            let (device, value) = crate::debug_exit::DebugExit::new(devices.power.clone());
            devices.register_ports(
                crate::debug_exit::PORT.into(),
                crate::debug_exit::PORTS,
                Arc::new(Mutex::new(device)),
            )?;
            Some(value)
        } else {
            None
        };
        let sampler = sampler(&self.profile, self.vcpus, &backing, &mut devices)?;
        devices.strict = self.strict_unassigned;
        Ok(Prepared {
            backing,
            ram: self.ram,
            mapped: Vec::new(),
            mapped_publishes: Arc::default(),
            loaded,
            uart,
            devices: devices.freeze(),
            irqs,
            board,
            virtio: Vec::new(),
            net: Mutex::new(None),
            fexc: self.fexc,
            max_block: self.max_block,
            stop_at: self.stop_at,
            watchdog: self.watchdog,
            launch: crate::probe::Launch {
                speculation_barriers: self.speculation_barriers,
                accurate_fp: self.accurate_fp,
                pool: self.pool,
                compile_cache: self.compile_cache.dir.clone().map(|d| (d, self.compile_cache.cap)),
            },
            compile_cache: self.compile_cache,
            check_reuse: self.check_reuse,
            page_currency: self.page_currency,
            writable_on_read: self.writable_on_read,
            fault_around: self.fault_around,
            inline_gather: self.inline_gather,
            code_shape: self.code_shape,
            accurate_fp: self.accurate_fp,
            ran: AtomicBool::new(false),
            cancel: Arc::default(),
            serial_input: Mutex::new(None),
            debug_exit,
            pvpanic,
            ps2,
            display: None,
            sampler,
        })
    }

    /// [`Boot::prepare`], then [`Prepared::run`].
    pub fn run(self) -> Result<Outcome> {
        self.prepare()?.run()
    }
}

/// [`Boot::profile`]: the sampler, and its control port registered beside debug-exit (both prepare paths, before the
/// registry is frozen).
fn sampler(
    profile: &Option<crate::sampler::Options>,
    vcpus: usize,
    backing: &Backing,
    devices: &mut devices::Devices,
) -> Result<Option<Arc<crate::sampler::Sampler>>> {
    let Some(options) = profile else {
        return Ok(None);
    };
    let sampler = crate::sampler::Sampler::new(options, vcpus, Some(backing.blocks()))?;
    devices.register_ports(
        crate::profile_port::PORT.into(),
        crate::profile_port::PORTS,
        Arc::new(Mutex::new(crate::profile_port::ProfilePort(sampler.clone()))),
    )?;
    Ok(Some(sampler))
}

/// A loaded VM, ready to run.
pub struct Prepared {
    backing: Backing,
    /// The guest's RAM in bytes (the memory cache's cap, `Compiler::share_for`).
    ram: u64,
    /// Mapped device memory, in the region table's order (the bus's (slot, function, BAR) order, `pci::Mapped::regions`):
    /// the VM maps each above the backing and every window aliases it at `MAPPED_VA` (`vm::mapping_plan`). Empty: none,
    /// the default launch.
    mapped: Vec<Arc<crate::pci::DeviceMemory>>,
    /// The bus's publishes of mapped BARs ([`crate::pci::Mapped::publishes`]).
    mapped_publishes: Arc<std::sync::atomic::AtomicU64>,
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
    /// The options the startup lines report.
    launch: crate::probe::Launch,
    check_reuse: bool,
    page_currency: u64,
    writable_on_read: bool,
    fault_around: u64,
    inline_gather: bool,
    code_shape: bool,
    accurate_fp: bool,
    /// [`Prepared::run`] has started: the backing is used.
    ran: AtomicBool,
    /// The run's cancellation flag ([`Prepared::canceller`]).
    cancel: Arc<AtomicBool>,
    /// [`Prepared::serial_input`], taken by the run.
    serial_input: Mutex<Option<Box<dyn Read + Send>>>,
    /// [`Boot::debug_exit`]: the value its device was written ([`crate::debug_exit::NONE`] until then).
    debug_exit: Option<Arc<std::sync::atomic::AtomicU64>>,
    /// [`Boot::pvpanic`]: the device's counts.
    pvpanic: Option<Arc<crate::pvpanic::PvPanicStats>>,
    /// [`Boot::ps2`]: the 8042's host input.
    ps2: Option<crate::ps2::Ps2Input>,
    /// [`Boot::display`]: the display function.
    display: Option<Arc<crate::display::Display>>,
    /// [`Boot::profile`]'s sampler.
    sampler: Option<Arc<crate::sampler::Sampler>>,
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
    /// Whether the run was cancelled (by any holder of this handle or a peer's failure).
    pub fn cancelled(&self) -> bool {
        self.0.load(SeqCst)
    }
    /// A canceller no run watches (tests).
    #[cfg(test)]
    pub(crate) fn detached() -> Canceller {
        Canceller(Arc::new(AtomicBool::new(false)))
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
    /// M2 Part 2: each vCPU's boot-time interpreter decode requests (`execution::Requests::interpreted`; 0 for one that
    /// never ran).
    pub decodes: Vec<u64>,
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
        let mut machine = backing.machine(&self.mapped)?;
        // EL0 sampler: the sample bells, and each one's INTID in its vCPU's head before any vCPU exists.
        if let Some(s) = &self.sampler {
            machine.bells()?;
            s.begin(&backing.blocks(), |v| machine.bell_intid(v));
        }
        let machine = machine;
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
            crate::probe::announce(&cpu, &self.launch);
            machine.route(v)?;
            Ok(cpu)
        };
        let ended_first = AtomicUsize::new(usize::MAX);
        let decodes: Vec<std::sync::atomic::AtomicU64> = (0..vcpus).map(|_| Default::default()).collect();
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
            compiler.pool = backing.pool.clone();
            compiler.share_for(&answers, self.ram);
            compiler.check_reuse = self.check_reuse;
            compiler.page_currency = self.page_currency;
            compiler.writable_on_read = self.writable_on_read;
            compiler.fault_around = self.fault_around;
            compiler.inline_gather = self.inline_gather;
            compiler.shape = self.code_shape;
            compiler.accurate_fp = self.accurate_fp;
            compiler.report = true; // M2 task D: stop reports and the host-side progress check
            compiler.objects = self.sampler.as_ref().filter(|s| s.objects_on).map(|s| s.objects.clone());
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
            decodes[v].store(requests.interpreted, SeqCst);
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
                    dropped_disk_hits: compiler.dropped_disk_hits,
                    admissions: crate::execution::why::NAMES
                        .into_iter()
                        .zip(&compiler.admissions)
                        .map(|(k, n)| (k, n.get()))
                        .collect(),
                    pool: execution::K1Pool::new(
                        &area.read_record(exec::cache::LOOKUP_OFFSET + exec::cache::CONTROL),
                    ),
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
        // now; zero without a `perf` resident): `snap NS` and `perf::BUCKETS` ns values per vCPU; then `snapc NS`, its CR3
        // loads, flushes, faults, link suspensions and relinks and entries per bucket (`Blocks::snap_counts`).
        let sampling = AtomicBool::new(crate::perf::on());
        // EL0 sampler: its ringing and drain threads end once every vCPU has.
        let sampler_done = AtomicBool::new(false);
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
            // The loops below end when their flag drops, also when `supervise` unwinds: the scope joins them before the
            // panic goes on, so a flag left set would hold the process.
            struct Off<'a>([&'a AtomicBool; 2]);
            impl Drop for Off<'_> {
                fn drop(&mut self) {
                    self.0[0].store(false, SeqCst);
                    self.0[1].store(true, SeqCst);
                }
            }
            let off = Off([&sampling, &sampler_done]);
            // EL0 sampler: the ringing thread and the drain (`crate::sampler`).
            if let Some(s) = &self.sampler {
                let (blocks, bell, done) = (backing.blocks(), machine.bell(), &sampler_done);
                scope.spawn(move || s.ring(&blocks, bell, done));
                let blocks = backing.blocks();
                scope.spawn(move || s.drain_until(&blocks, done));
            }
            if sampling.load(SeqCst) {
                let (blocks, sampling) = (backing.blocks(), &sampling);
                scope.spawn(move || {
                    while sampling.load(SeqCst) {
                        crate::perf::line(&blocks.snap());
                        crate::perf::line(&blocks.snap_counts());
                        std::thread::sleep(std::time::Duration::from_millis(5));
                    }
                });
            }
            let supervision = owners::supervise(vcpus, &prepare, &body, &self.cancel);
            drop(off); // the perf snapshots stop (`sampling` false), the sampler's threads end (`sampler_done`)
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
        if let Some(p) = &backing.pool {
            crate::perf::line(&p.report());
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
        // AFP accurate plan chunk 6: `NATEMU_FP_TRAP_COUNTS=1` reports each vCPU's trapped-FP counts on stderr after the run, for `tools/bench/run.py`
        // (`fp_hist`: cause * 8 + class, see `cache::Control`).
        if std::env::var_os("NATEMU_FP_TRAP_COUNTS").is_some() {
            for v in 0..vcpus {
                let c: exec::cache::Control = backing.vcpu(v).read_record(exec::cache::LOOKUP_OFFSET + exec::cache::CONTROL);
                eprintln!("natemu-fp-traps vcpu {v} traps {} steps {} hist {:?}", c.fp_traps, c.fp_steps, c.fp_hist);
                // M2 task 4: system instructions the resident ran, by kind (hex, `execution::system`), and stack-check firings;
                // split-check plan step A: linker entries through a split block's cold code (`cold_exits`).
                let kinds: Vec<String> = (0..128).filter(|&k| c.system_counts[k] != 0).map(|k| format!("{k:x}:{}", c.system_counts[k])).collect();
                eprintln!("natemu-system-counts vcpu {v} stack {} kinds {} cold_exits {}", c.stack_checks, kinds.join(","), c.cold_exits);
            }
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
            decodes: decodes.iter().map(|d| d.load(SeqCst)).collect(),
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
    /// Mapped device memory after (or during) the run, `None` without any region (the default launch): each region's
    /// table entry (`base`, `len`, `enabled`), the bus's publishes, and per vCPU the shadows installed onto regions
    /// (`mapped_installs`) and the remap shootdowns it ran (`remaps`, `cache::Control`).
    pub fn mapped_report(&self) -> Option<serde_json::Value> {
        if self.mapped.is_empty() {
            return None;
        }
        let (_, entries) = self.backing.mapped_regions().read();
        let regions: Vec<_> = entries[..self.mapped.len()]
            .iter()
            .map(|r| serde_json::json!({"base": r.base, "len": r.len, "enabled": r.enabled}))
            .collect();
        let control = |v| -> exec::cache::Control {
            self.backing.vcpu(v).read_record(exec::cache::LOOKUP_OFFSET + exec::cache::CONTROL)
        };
        let n = self.backing.n;
        Some(serde_json::json!({
            "regions": regions,
            "publishes": self.mapped_publishes.load(SeqCst),
            "mapped_installs": (0..n).map(|v| control(v).mapped_installs).collect::<Vec<_>>(),
            "remaps": (0..n).map(|v| control(v).remaps).collect::<Vec<_>>(),
        }))
    }
    pub fn devices(&self) -> &devices::Devices {
        &self.devices
    }
    /// [`Boot::debug_exit`]: the value the guest wrote to its port (the run then ended with `Stop::Power(pm::DEBUG_EXIT)`;
    /// QEMU's exit status is [`crate::debug_exit::DebugExit::status`] of it), or `None` (no device, or no write).
    pub fn debug_exit(&self) -> Option<u64> {
        self.debug_exit
            .as_ref()
            .map(|v| v.load(SeqCst))
            .filter(|&v| v != crate::debug_exit::NONE)
    }
    /// [`Boot::pvpanic`]: the device's counts (`None` without the device).
    pub fn pvpanic(&self) -> Option<&crate::pvpanic::PvPanicStats> {
        self.pvpanic.as_deref()
    }
    /// [`Boot::ps2`]: the 8042's host input, keyboard and mouse (`None` without the device). A producer holds an
    /// [`crate::irq::Irqs::busy`] guard while input can still arrive ([`crate::ps2`]).
    pub fn ps2(&self) -> Option<&crate::ps2::Ps2Input> {
        self.ps2.as_ref()
    }
    /// [`Boot::display`]: the display function, its framebuffer and published mode (`None` without a display).
    pub fn display(&self) -> Option<Arc<crate::display::Display>> {
        self.display.clone()
    }
    /// The fixed power button of the PM block, which a host thread presses ([`crate::pm::PowerButton::press`]). The PM
    /// block is on every board, so this is always `Some`; it is an `Option` as the control socket's `Target` is.
    pub fn power_button(&self) -> Option<crate::pm::PowerButton> {
        Some(self.board.power_button())
    }
    /// The interrupt routing, for asynchronous sources and their [`crate::irq::Irqs::busy`] guards (the control API's).
    pub fn irqs(&self) -> &crate::irq::Irqs {
        &self.irqs
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

    #[test]
    fn an_overlay_named_without_its_base_says_which_key_to_add() {
        let dir = std::env::temp_dir().join(format!("natemu-linux-overlay-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let (base, top) = (dir.join("base.raw"), dir.join("top.qcow2"));
        std::fs::write(&base, vec![0u8; 1 << 20]).unwrap();
        let spec = |readonly, backing: Option<&std::path::Path>| DiskSpec {
            id: "disk0".into(),
            path: top.clone(),
            transport: Transport::Mmio,
            readonly,
            backing: backing.map(Into::into),
            snapshot: false,
        };
        // Read-only: never created.
        let e = spec(true, Some(&base)).open().err().unwrap();
        assert!(e.contains("never created") && !top.exists(), "{e}");
        // Created, then reopened with its base; without it, the error names the key to add.
        let image = spec(false, Some(&base)).open().unwrap();
        assert_eq!(image.capacity, 1 << 20);
        drop(image);
        assert!(crate::disk::retry_lock(|| spec(false, Some(&base)).open()).is_ok());
        let e = crate::disk::retry_lock(|| spec(false, None).open()).err().unwrap();
        let named = std::fs::canonicalize(&base).unwrap();
        assert!(
            e.contains(&format!("add backing = \"{}\"", named.display()))
                && e.contains(&format!("--set disk.disk0.backing={}", named.display())),
            "{e}"
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_snapshot_disk_keeps_its_base_unchanged_and_leaves_no_file() {
        let dir =
            std::env::temp_dir().join(format!("natemu-linux-snapshot-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let base = dir.join("base.raw");
        let pattern: Vec<u8> = (0..1u32 << 20).map(|i| (i * 7) as u8).collect();
        std::fs::write(&base, &pattern).unwrap();
        let spec = |readonly, backing: Option<&std::path::Path>| DiskSpec {
            id: "snapdisk".into(),
            path: base.clone(),
            transport: Transport::Mmio,
            readonly,
            backing: backing.map(Into::into),
            snapshot: true,
        };
        let leftovers = || {
            std::fs::read_dir(std::env::temp_dir())
                .unwrap()
                .flatten()
                .filter(|e| {
                    e.file_name()
                        .to_string_lossy()
                        .starts_with(&format!("natemu-snapshot-{}-snapdisk-", std::process::id()))
                })
                .count()
        };
        let image = spec(false, None).open().unwrap();
        assert_eq!(image.capacity, 1 << 20);
        assert_eq!(leftovers(), 0, "the overlay file is deleted once open");
        // Writes go to the open overlay: read back there, the base keeps its bytes (and stays shareable read-only).
        let (calls, mut buf) = (&image.backend, vec![0u8; 4096]);
        calls.write(&[0xAB; 4096], 8192).unwrap();
        calls.read(&mut buf, 8192).unwrap();
        assert_eq!(buf, vec![0xAB; 4096]);
        calls.read(&mut buf, 0).unwrap();
        assert_eq!(buf, pattern[..4096]);
        calls.sync(false).unwrap();
        assert!(crate::disk::open(&base, true, None).is_ok());
        drop(image);
        assert_eq!(std::fs::read(&base).unwrap(), pattern);
        for (readonly, backing) in [(true, None), (false, Some(base.as_path()))] {
            let e = spec(readonly, backing).open().err().unwrap();
            assert!(
                e.contains("snapshot goes with neither readonly nor backing"),
                "{e}"
            );
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_repeated_file_gets_a_serial_no_file_has_and_the_first_use_keeps_its_own() {
        let mut used = vec![];
        let (a, b) = (*b"0100001000000000002a", *b"0100001000000000002b");
        let ids: Vec<[u8; 20]> = [a, b, a, a, b]
            .into_iter()
            .map(|mut id| {
                unique_serial(&mut id, &mut used);
                id
            })
            .collect();
        assert_eq!(ids[..2], [a, b]);
        assert_eq!(&ids[2][..], b"g100001000000000002a");
        assert_eq!(&ids[3][..], b"h100001000000000002a");
        assert_eq!(&ids[4][..], b"g100001000000000002b");
    }

    #[test]
    fn network_defaults_keep_forwarding_and_host_loopback_off() {
        let boot = Boot::new("missing-runtime", "missing-fexc", vec![]);
        assert!(!boot.net);
        assert!(boot.net_options.forwards.is_empty());
        assert!(!boot.net_options.host_loopback);
    }

    #[test]
    fn udp_policy_is_checked_before_ram_or_vm_preparation() {
        let mut boot = Boot::new("missing-runtime", "missing-fexc", vec![]);
        boot.net = true;
        boot.ram = 0;
        boot.net_options.forwards = vec![crate::virtio::PortForward {
            protocol: crate::virtio::ForwardProtocol::Udp,
            host_port: 15201.try_into().unwrap(),
            guest_port: 5201.try_into().unwrap(),
        }];
        let error = boot.prepare().err().expect("UDP needs explicit opt-in");
        assert!(error.contains("requires --net-host-loopback"), "{error}");
    }

    #[test]
    fn network_options_are_validated_before_ram_or_vm_preparation() {
        use crate::virtio::{ForwardProtocol, PortForward, SlirpOptions};
        let forward = PortForward {
            protocol: ForwardProtocol::Tcp,
            host_port: 2222.try_into().unwrap(),
            guest_port: 22.try_into().unwrap(),
        };
        for options in [
            SlirpOptions {
                host_loopback: true,
                ..Default::default()
            },
            SlirpOptions {
                forwards: vec![forward],
                ..Default::default()
            },
        ] {
            let mut boot = Boot::new("missing-runtime", "missing-fexc", vec![]);
            boot.ram = 0; // A later preparation error must not mask the option error.
            boot.net_options = options;
            let error = boot.prepare().err().expect("options require networking");
            assert!(error.contains("require --net"), "{error}");
        }
        let mut boot = Boot::new("missing-runtime", "missing-fexc", vec![]);
        boot.ram = 0;
        boot.net = true;
        boot.net_options.forwards = vec![
            forward,
            PortForward {
                guest_port: 80.try_into().unwrap(),
                ..forward
            },
        ];
        let error = boot.prepare().err().expect("duplicate listener");
        assert!(
            error.contains("duplicate tcp listener 127.0.0.1:2222"),
            "{error}"
        );
    }

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

    /// An entropy device on PCI installs the bus (the memory window and the ports at 0xCF8) and leaves the MMIO
    /// register page unregistered; on MMIO, as before, there is no window.
    #[test]
    fn a_pci_device_installs_the_bus_and_an_mmio_one_does_not() {
        let resident =
            std::env::temp_dir().join(format!("natemu-linux-pci-{}.elf", std::process::id()));
        std::fs::write(
            &resident,
            crate::memory::tests::resident_elf(&[(crate::PRIVATE, 4)]),
        )
        .unwrap();
        for (transport, pci) in [(Transport::Pci, true), (Transport::Mmio, false)] {
            let mut boot = Boot::new(&resident, "fexc", boot::tests::image(0x1000));
            (boot.ram, boot.code_bytes) = (64 << 20, exec::cache::MAX_BLOCK_BYTES);
            boot.rng = true;
            boot.transports.rng = transport;
            let prepared = boot.prepare().unwrap();
            let found = |gpa: u64| prepared.devices().find(gpa).is_some();
            assert_eq!(found(crate::pci::platform::WINDOW.0.into()), pci);
            assert_eq!(found(crate::virtio::RNG_RESOURCE.base.into()), !pci);
        }
        std::fs::remove_file(&resident).unwrap();
    }

    /// The test function with every virtio device on MMIO: the bus at slot 1 (functions 0 and 1), the memory window and
    /// the I/O window registered; without it neither.
    #[test]
    fn the_test_function_installs_the_bus_and_the_io_window() {
        let resident =
            std::env::temp_dir().join(format!("natemu-linux-testdev-{}.elf", std::process::id()));
        std::fs::write(
            &resident,
            crate::memory::tests::resident_elf(&[(crate::PRIVATE, 4)]),
        )
        .unwrap();
        for testdev in [true, false] {
            let mut boot = Boot::new(&resident, "fexc", boot::tests::image(0x1000));
            (boot.ram, boot.code_bytes) = (64 << 20, exec::cache::MAX_BLOCK_BYTES);
            (boot.rng, boot.pci_testdev, boot.strict_unassigned) = (true, testdev, true);
            let prepared = boot.prepare().unwrap();
            let d = prepared.devices();
            assert_eq!(d.find(crate::pci::platform::WINDOW.0.into()).is_some(), testdev);
            assert!(d.find(crate::virtio::RNG_RESOURCE.base.into()).is_some());
            assert_eq!(d.port(0, 0xc000, 1, None).is_ok(), testdev, "the I/O window");
            if testdev {
                for f in 0..2u64 {
                    d.port(0, 0xcf8, 4, Some(1 << 31 | 1 << 11 | f << 8)).unwrap();
                    assert_eq!(d.port(0, 0xcfc, 4, None), Ok(0x0005_1b36), "01.{f}");
                }
            }
        }
        std::fs::remove_file(&resident).unwrap();
    }

    /// The display: QEMU's `bochs-display` identity at the lowest free slot, its framebuffer the launch's only mapped region
    /// (of `vram_mib`), `Prepared::display` the function; a spec `check` refuses is a launch error; none without it.
    #[test]
    fn the_display_installs_its_framebuffer() {
        let resident =
            std::env::temp_dir().join(format!("natemu-linux-display-{}.elf", std::process::id()));
        std::fs::write(&resident, crate::memory::tests::resident_elf(&[(crate::PRIVATE, 4)])).unwrap();
        let spec = crate::display::Spec { vram_mib: 8, ..Default::default() };
        for display in [Some(spec), None] {
            let mut boot = Boot::new(&resident, "fexc", boot::tests::image(0x1000));
            (boot.ram, boot.code_bytes) = (64 << 20, exec::cache::MAX_BLOCK_BYTES);
            (boot.rng, boot.display) = (true, display);
            let prepared = boot.prepare().unwrap();
            let on = display.is_some();
            assert_eq!(prepared.mapped.iter().map(|m| m.len()).collect::<Vec<_>>(), if on { vec![8 << 20] } else { vec![] });
            assert_eq!(prepared.display().is_some(), on);
            if let Some(d) = prepared.display() {
                assert!(Arc::ptr_eq(d.vram(), &prepared.mapped[0]), "the framebuffer is the mapped region");
                assert_eq!(d.mode(), None);
            }
            let d = prepared.devices();
            assert_eq!(d.find(crate::pci::platform::WINDOW.0.into()).is_some(), on, "the bus with the rng on MMIO");
            if on {
                d.port(0, 0xcf8, 4, Some(1 << 31 | 1 << 11)).unwrap();
                assert_eq!(d.port(0, 0xcfc, 4, None), Ok(0x1111_1234), "01.0: the display");
            }
        }
        let mut boot = Boot::new(&resident, "fexc", boot::tests::image(0x1000));
        (boot.ram, boot.code_bytes) = (64 << 20, exec::cache::MAX_BLOCK_BYTES);
        boot.display = Some(crate::display::Spec { vram_mib: 4, ..Default::default() });
        let e = boot.prepare().err().expect("4 MiB holds fewer than two 1024 x 768 frames");
        assert!(e.starts_with("display.vram_mib: 4 MiB"), "{e}");
        std::fs::remove_file(&resident).unwrap();
    }

    /// The mapped-memory test function: the bus at the lowest free slot (function 0, vendor 1B36H device 0005H subsystem
    /// 0100H), its region handed to the machine (one, 2 MiB), and the run report's `mapped` part; neither without it.
    #[test]
    fn the_mapped_test_function_installs_its_region() {
        let resident =
            std::env::temp_dir().join(format!("natemu-linux-mapped-{}.elf", std::process::id()));
        std::fs::write(&resident, crate::memory::tests::resident_elf(&[(crate::PRIVATE, 4)])).unwrap();
        for on in [true, false] {
            let mut boot = Boot::new(&resident, "fexc", boot::tests::image(0x1000));
            (boot.ram, boot.code_bytes) = (64 << 20, exec::cache::MAX_BLOCK_BYTES);
            (boot.rng, boot.pci_testdev, boot.mapped_testdev) = (true, true, on);
            let prepared = boot.prepare().unwrap();
            assert_eq!(prepared.mapped.iter().map(|m| m.len()).collect::<Vec<_>>(), if on { vec![2 << 20] } else { vec![] });
            assert_eq!(prepared.mapped_report().is_some(), on);
            let d = prepared.devices();
            // the test function at slot 1, the mapped one at slot 2
            d.port(0, 0xcf8, 4, Some(1 << 31 | 2 << 11)).unwrap();
            assert_eq!(d.port(0, 0xcfc, 4, None), Ok(if on { 0x0005_1b36 } else { 0xffff_ffff }));
            d.port(0, 0xcf8, 4, Some(1 << 31 | 2 << 11 | 11 << 2)).unwrap();
            assert_eq!(d.port(0, 0xcfc, 4, None), Ok(if on { 0x0100_1b36 } else { 0xffff_ffff }));
        }
        let mut boot = Boot::new(&resident, "fexc", boot::tests::image(0x1000));
        (boot.ram, boot.code_bytes, boot.mapped_testdev) = (64 << 20, exec::cache::MAX_BLOCK_BYTES, true);
        let prepared = boot.prepare().unwrap();
        assert!(prepared.devices().find(crate::pci::platform::WINDOW.0.into()).is_some(), "the bus with every virtio device on MMIO");
        std::fs::remove_file(&resident).unwrap();
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
    /// Disk hits of answers the memory cache had dropped ([`execution::Compiler::dropped_disk_hits`]).
    dropped_disk_hits: u64,
    /// Object answers by why they were admitted again or not ([`execution::why`]; their sum is every object answer).
    admissions: crate::report::Members<&'static str, u64>,
    /// The template pool's lookups, copies and refusals (cross-vCPU reuse plan chunk 3; zero without a pool).
    pool: execution::K1Pool,
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
            dropped_disk_hits: next(),
            admissions: [("skipped", 1), ("fresh", 2)].into_iter().collect(),
            pool: Default::default(),
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
        let mut old: serde_json::Value = serde_json::from_str(&old).unwrap();
        old["dropped_disk_hits"] = serde_json::json!(v.dropped_disk_hits);
        old["admissions"] = serde_json::json!({"skipped": 1, "fresh": 2});
        old["pool"] = serde_json::json!({"probes": 0, "hits": 0, "candidates": 0, "refused": {}, "recheck": {}});
        assert_eq!(old, serde_json::to_value(&v).unwrap());
    }
}

/// Placement of disks on MMIO, PCI and mixed lists, with the other devices (design section 3).
#[cfg(test)]
mod placement_tests {
    use super::*;
    use Transport::{Mmio, Pci};
    fn wanted(disks: &[Transport], other: Transport) -> Wanted {
        Wanted {
            rng: Some(other),
            disks: disks.to_vec(),
            net: Some(other),
            consoles: vec![],
            testdev: false,
            mapped_testdev: false,
            display: false,
        }
    }
    fn lists(n: usize) -> Vec<Vec<Transport>> {
        let alt = |first: Transport| {
            (0..n)
                .map(|k| {
                    if k % 2 == 0 {
                        first
                    } else if first == Mmio {
                        Pci
                    } else {
                        Mmio
                    }
                })
                .collect()
        };
        let mut v = vec![vec![Mmio; n], vec![Pci; n], alt(Mmio), alt(Pci)];
        v.push((0..n).map(|k| if k < n / 2 { Pci } else { Mmio }).collect());
        v.push((0..n).map(|k| if k < n / 2 { Mmio } else { Pci }).collect());
        v
    }
    fn distinct<T: Ord + std::fmt::Debug + Copy>(v: Vec<T>, what: &str) {
        let mut u = v.clone();
        u.sort_unstable();
        u.dedup();
        assert_eq!(u.len(), v.len(), "{what} collide: {v:?}");
    }
    /// Distinct slots, pages and uids; MMIO lines exclusive (no other MMIO device and no PCI function on them); every
    /// line in 16-23.
    fn collision_free(p: &[Placed]) {
        distinct(
            p.iter().filter(|q| q.pci).map(|q| q.slot).collect(),
            "slots",
        );
        distinct(
            p.iter().filter(|q| !q.pci).map(|q| q.res.base).collect(),
            "pages",
        );
        distinct(
            p.iter().filter(|q| !q.pci).map(|q| q.res.uid).collect(),
            "uids",
        );
        let mmio: Vec<_> = p.iter().filter(|q| !q.pci).map(|q| q.res.gsi).collect();
        distinct(mmio.clone(), "MMIO lines");
        for q in p.iter().filter(|q| q.pci) {
            assert!(
                !mmio.contains(&q.intx),
                "slot {} on MMIO line {}",
                q.slot,
                q.intx
            );
        }
        let lines = p.iter().map(|q| if q.pci { q.intx } else { q.res.gsi });
        assert!(
            lines.clone().all(|l| (16..24).contains(&l)),
            "{:?}",
            lines.collect::<Vec<_>>()
        );
    }
    /// The invariants of any placement: distinct slots, pages and uids; MMIO lines exclusive (no other MMIO device and
    /// no PCI function on them); every line in 16-23; disks dense per transport; NIC and entropy at their old places.
    fn check(disks: &[Transport], other: Transport) -> Vec<Placed> {
        let p = place(&wanted(disks, other)).unwrap();
        assert_eq!(p.len(), disks.len() + 2);
        collision_free(&p);
        let (mut m, mut pc) = (0u32, 0u8);
        for (k, &t) in disks.iter().enumerate() {
            let d = p.iter().find(|q| q.kind == Kind::Disk(k)).unwrap();
            assert_eq!(d.pci, t == Pci);
            if d.pci {
                assert_eq!(d.slot, if pc == 0 { 1 } else { 3 + pc });
                pc += 1;
            } else {
                let want = if m == 0 {
                    (0, 0xf000_0000, 16)
                } else {
                    (2 + m, 0xf000_0000 + (2 + m) * 4096, 18 + m)
                };
                assert_eq!((d.res.uid as u32, d.res.base, d.res.gsi), want);
                m += 1;
            }
        }
        let line = |q: &Placed| if q.pci { q.intx } else { q.res.gsi };
        let net = p.iter().find(|q| q.kind == Kind::Net).unwrap();
        let rng = p.iter().find(|q| q.kind == Kind::Rng).unwrap();
        assert_eq!(
            (net.slot, net.res.base, line(net), net.res.uid),
            (2, 0xf000_1000, 17, 1)
        );
        assert_eq!(
            (rng.slot, rng.res.base, line(rng), rng.res.uid),
            (3, 0xf000_2000, 18, 2)
        );
        let plan = plan_tables(1, boot::ACPI_NVS.0, &p, 1 << 30).unwrap();
        assert_eq!(plan.manifest.len() + plan.functions.len(), p.len());
        // The routing table (`_PRT`) routes each slot to its INTx line.
        let mut f: Vec<_> = p
            .iter()
            .filter(|q| q.pci)
            .map(|q| (q.slot, q.intx))
            .collect();
        f.sort_unstable();
        assert_eq!(plan.functions, f);
        p
    }
    #[test]
    fn placement_is_dense_per_transport_and_never_collides() {
        for n in 1..=MAX_MMIO_DISKS {
            for disks in lists(n) {
                for other in [Mmio, Pci] {
                    check(&disks, other);
                }
            }
        }
    }
    /// 29 PCI disks with the NIC and entropy device on PCI: slots 1-31, slots 1-3 on 16/17/18, slots 4-31 round robin
    /// over 16-23. With the NIC and entropy device on MMIO, their lines 17 and 18 are left out of the rotation.
    #[test]
    fn twenty_nine_pci_disks_share_lines_round_robin() {
        let p = check(&[Pci; MAX_PCI_DISKS], Pci);
        let mut f: Vec<_> = p.iter().map(|q| (q.slot, q.intx)).collect();
        f.sort_unstable();
        let want: Vec<_> = (1..=31u8)
            .map(|s| {
                (
                    s,
                    if s <= 3 {
                        15 + u32::from(s)
                    } else {
                        16 + u32::from(s - 4) % 8
                    },
                )
            })
            .collect();
        assert_eq!(f, want);
        let p = check(&[Pci; MAX_PCI_DISKS], Mmio);
        let rot = [16, 19, 20, 21, 22, 23];
        for q in p.iter().filter(|q| q.pci) {
            let want = if q.slot == 1 {
                16
            } else {
                rot[usize::from(q.slot - 4) % rot.len()]
            };
            assert_eq!(q.intx, want, "slot {}", q.slot);
        }
        // The pinned case's lines: eight PCI disks share 16, 17 and 18 with slots 1-3.
        let p = check(&[Pci; 8], Pci);
        let on = |l| p.iter().filter(|q| q.intx == l).count();
        assert_eq!([16, 17, 18, 19, 23].map(on), [2, 2, 2, 1, 0]);
    }
    /// Mixed lists up to the line budget: MMIO disks hold 16 and 19-23; PCI disks rotate over what is left.
    #[test]
    fn mixed_lists_keep_mmio_lines_exclusive() {
        let mut big = vec![Mmio; MAX_MMIO_DISKS];
        big.extend([Pci; MAX_PCI_DISKS]);
        let p = check(&big, Pci);
        assert!(
            p.iter()
                .filter(|q| q.pci)
                .all(|q| [17, 18].contains(&q.intx))
        );
        // The first PCI disk takes slot 1, but line 16 is the first MMIO disk's: it rotates too.
        let p = check(&[Mmio, Pci, Pci], Mmio);
        let d1 = p.iter().find(|q| q.kind == Kind::Disk(1)).unwrap();
        assert_eq!((d1.slot, d1.intx), (1, 19));
        // Every line held by MMIO: no line is left for a PCI disk.
        let mut full = vec![Mmio; MAX_MMIO_DISKS];
        full.push(Pci);
        assert_eq!(
            place(&wanted(&full, Mmio)).unwrap_err(),
            "no interrupt line left for PCI slot 1 (lines 16-23 are all held)"
        );
    }
    /// The fallback (`SHARED_INTX = false`): every device on a line of its own, so PCI is capped by lines again.
    #[test]
    fn the_unique_line_fallback_gives_exclusive_lines_and_six_pci_disks() {
        for other in [Mmio, Pci] {
            for n in 1..=MAX_MMIO_DISKS {
                let p = place_lines(&wanted(&vec![Pci; n], other), false).unwrap();
                let lines = p.iter().map(|q| if q.pci { q.intx } else { q.res.gsi });
                distinct(lines.collect(), "lines");
            }
            assert_eq!(
                place_lines(&wanted(&[Pci; 7], other), false).unwrap_err(),
                "no interrupt line left for PCI slot 9 (lines 16-23 are all held)"
            );
        }
        let shared = place_lines(&wanted(&[Pci; 8], Pci), true).unwrap();
        assert_eq!(shared.len(), 10);
    }
    #[test]
    fn too_many_disks_are_refused_per_transport() {
        let mmio = "at most 6 disks on mmio (interrupt lines 16-23); use pci for more";
        let pci = "PCI slots 1-31 are all used: at most 29 disks on pci";
        let mut seven = vec![Mmio; 7];
        assert_eq!(place(&wanted(&seven, Pci)).unwrap_err(), mmio);
        seven.extend([Pci; 3]);
        assert_eq!(place(&wanted(&seven, Pci)).unwrap_err(), mmio);
        assert_eq!(place(&wanted(&[Pci; 30], Pci)).unwrap_err(), pci);
        assert_eq!(place(&wanted(&[Pci; 30], Mmio)).unwrap_err(), pci);
        assert_eq!(place(&wanted(&[], Mmio)).unwrap().len(), 2);
    }
    /// The test function: the lowest slot no virtio function holds, after the others (which it leaves where they were),
    /// no `_PRT` entry, the I/O window in the tables, and the bus even with every virtio device on MMIO.
    #[test]
    fn the_test_function_takes_the_lowest_free_slot() {
        let with = |w: Wanted| Wanted { testdev: true, ..w };
        for (w, slot) in [
            (wanted(&[], Mmio), 1),
            (wanted(&[Mmio; 3], Mmio), 1),
            (Wanted { rng: Some(Pci), disks: vec![], net: None, consoles: vec![], testdev: false, mapped_testdev: false, display: false }, 1),
            (wanted(&[], Pci), 1),
            (wanted(&[Pci], Pci), 4),
            (wanted(&[Pci; 3], Pci), 6),
            (wanted(&[Pci; 28], Pci), 31),
        ] {
            let before = place(&w).unwrap();
            let p = place(&with(w)).unwrap();
            let t = p.last().unwrap();
            assert_eq!((t.kind, t.pci, t.slot), (Kind::TestDev, true, slot));
            assert_eq!(format!("{:?}", &p[..p.len() - 1]), format!("{before:?}"));
            let plan = plan_tables(1, boot::ACPI_NVS.0, &p, 1 << 30).unwrap();
            let old = plan_tables(1, boot::ACPI_NVS.0, &before, 1 << 30).unwrap();
            assert_eq!(plan.functions, old.functions, "no _PRT entry");
            assert!(plan.io_window && !old.io_window && plan.layout.pci);
        }
        assert_eq!(
            place(&with(wanted(&[Pci; 29], Pci))).unwrap_err(),
            "debug.pci_testdev: PCI slots 1-31 are all used"
        );
    }
    /// The display: the lowest slot still free after the test functions, no `_PRT` entry and no I/O window; every other
    /// function where it was without it; the bus even with every virtio device on MMIO.
    #[test]
    fn the_display_takes_the_lowest_free_slot_last() {
        for (w, slot) in [
            (wanted(&[], Mmio), 1),
            (Wanted { testdev: true, ..wanted(&[], Mmio) }, 2),
            (Wanted { testdev: true, mapped_testdev: true, ..wanted(&[], Mmio) }, 3),
            (wanted(&[Pci], Pci), 4),
            (Wanted { testdev: true, mapped_testdev: true, ..wanted(&[Pci; 3], Pci) }, 8),
            (wanted(&[Pci; 28], Pci), 31),
        ] {
            let before = place(&w).unwrap();
            let p = place(&Wanted { display: true, ..w }).unwrap();
            let d = p.last().unwrap();
            assert_eq!((d.kind, d.pci, d.slot), (Kind::Display, true, slot));
            assert_eq!(format!("{:?}", &p[..p.len() - 1]), format!("{before:?}"), "nothing else moves");
            let plan = plan_tables(1, boot::ACPI_NVS.0, &p, 1 << 30).unwrap();
            let old = plan_tables(1, boot::ACPI_NVS.0, &before, 1 << 30).unwrap();
            assert_eq!(plan.functions, old.functions, "no _PRT entry");
            assert_eq!(plan.io_window, old.io_window, "no I/O BAR");
            assert!(plan.layout.pci);
        }
        assert_eq!(
            place(&Wanted { display: true, ..wanted(&[Pci; 29], Pci) }).unwrap_err(),
            "display.device: PCI slots 1-31 are all used"
        );
    }
    fn with_consoles(disks: &[Transport], other: Transport, consoles: &[Transport]) -> Wanted {
        Wanted {
            consoles: consoles
                .iter()
                .enumerate()
                .map(|(k, &t)| (format!("hvc{k}"), t))
                .collect(),
            ..wanted(disks, other)
        }
    }
    /// Consoles come after the disks: the other devices keep their slots, pages, uids and MMIO lines (PCI functions'
    /// shared lines may rotate), an MMIO console takes the page and uid after the disks' and the first free line, a PCI
    /// console the slot after theirs, and the tables list each device once.
    #[test]
    fn consoles_are_placed_after_the_disks() {
        // Up to three MMIO disks: with the NIC and entropy device on MMIO, three lines stay, for the two MMIO consoles
        // and the PCI console's INTx.
        for n in 0..=3 {
            for disks in lists(n) {
                for other in [Mmio, Pci] {
                    let alone = place(&wanted(&disks, other)).unwrap();
                    let p = place(&with_consoles(&disks, other, &[Mmio, Pci, Mmio])).unwrap();
                    assert_eq!(p.len(), alone.len() + 3);
                    collision_free(&p);
                    for a in &alone {
                        let b = p.iter().find(|q| q.kind == a.kind).unwrap();
                        assert_eq!((b.pci, b.slot), (a.pci, a.slot), "{:?}", a.kind);
                        if !a.pci {
                            let (x, y) = (a.res, b.res);
                            assert_eq!((y.uid, y.base, y.gsi), (x.uid, x.base, x.gsi));
                        }
                    }
                    let m = disks.iter().filter(|&&t| t == Mmio).count() as u32;
                    let pc = disks.len() as u32 - m;
                    let held: Vec<u32> =
                        alone.iter().filter(|q| !q.pci).map(|q| q.res.gsi).collect();
                    let free: Vec<u32> = (16..24).filter(|l| !held.contains(l)).collect();
                    let console = |k| p.iter().find(|q| q.kind == Kind::Console(k)).unwrap();
                    for (k, i) in [(0, 0), (2, 1)] {
                        let c = console(k);
                        let page = 2 + m.max(1) + i;
                        assert!(!c.pci);
                        assert_eq!(
                            (u32::from(c.res.uid), c.res.base, c.res.gsi),
                            (page, 0xf000_0000 + page * 4096, free[i as usize])
                        );
                    }
                    assert!(console(1).pci);
                    assert_eq!(u32::from(console(1).slot), 3 + pc.max(1));
                    let plan = plan_tables(1, boot::ACPI_NVS.0, &p, 1 << 30).unwrap();
                    assert_eq!(plan.manifest.len() + plan.functions.len(), p.len());
                }
            }
        }
    }
    /// A console on MMIO when every line 16-23 is held by an MMIO device is a launch error naming it; so is one on PCI
    /// when the disks hold every slot.
    #[test]
    fn mmio_without_free_gsi_is_refused() {
        let full = with_consoles(&[Mmio; MAX_MMIO_DISKS], Mmio, &[Mmio]);
        assert_eq!(
            place(&full).unwrap_err(),
            "console \"hvc0\": no interrupt line left on mmio (lines 16-23 are all held); use pci"
        );
        // One line fewer held: the console takes the last one, and a second console is refused.
        let p = place(&with_consoles(&[Mmio; 5], Mmio, &[Mmio])).unwrap();
        let c = p.iter().find(|q| q.kind == Kind::Console(0)).unwrap();
        assert_eq!((c.res.uid, c.res.base, c.res.gsi), (7, 0xf000_7000, 23));
        assert_eq!(
            place(&with_consoles(&[Mmio; 5], Mmio, &[Mmio, Mmio])).unwrap_err(),
            "console \"hvc1\": no interrupt line left on mmio (lines 16-23 are all held); use pci"
        );
        assert_eq!(
            place(&with_consoles(&[Pci; MAX_PCI_DISKS], Pci, &[Pci])).unwrap_err(),
            "console \"hvc0\": PCI slots 1-31 are all used; use mmio or fewer disks on pci"
        );
    }
    /// Consoles and the test function: the consoles take their slots first, the test function the lowest slot still
    /// free (a console's included), and neither moves the other's devices.
    #[test]
    fn the_test_function_comes_after_the_consoles() {
        for (disks, consoles, slot) in [
            (&[Pci][..], &[Pci][..], 5),
            (&[Pci][..], &[Mmio][..], 4),
            (&[Pci; 3][..], &[Pci, Pci][..], 8),
            (&[][..], &[Mmio, Pci][..], 1),
        ] {
            let w = with_consoles(disks, Pci, consoles);
            let before = place(&w).unwrap();
            let p = place(&Wanted { testdev: true, ..w }).unwrap();
            let t = p.last().unwrap();
            assert_eq!((t.kind, t.pci, t.slot), (Kind::TestDev, true, slot));
            assert_eq!(format!("{:?}", &p[..p.len() - 1]), format!("{before:?}"));
        }
        let full = Wanted { testdev: true, ..with_consoles(&[Pci; 28], Pci, &[Pci]) };
        assert_eq!(place(&full).unwrap_err(), "debug.pci_testdev: PCI slots 1-31 are all used");
    }
}

/// Pins what old command lines produce, before any refactor of how devices are configured. Each line of `EXPECTED` is
/// `manifest|PCI functions|digest of the ACPI blobs (address and bytes)|mailbox|(low_end, high, pci)|digest of the e820
/// map`, for 1 and 4 vCPUs, the 8 subsets of {rng, disk, net} (bit 0 rng, 1 disk, 2 net) and four transport mixes (all
/// MMIO, all PCI, disk=pci only, net and rng pci with disk mmio), at 1 GiB, then one 5 GiB all-PCI case. RECORDED on the
/// unmodified `prepare()` (commit 33a99fb8: the same logic inline in a throwaway test, printing these strings), then
/// the table building was extracted into `launch_plan` and this test, now calling it, passes unchanged.
#[cfg(test)]
mod pin_tests {
    use super::*;
    fn fnv(parts: impl IntoIterator<Item = u8>) -> u64 {
        parts.into_iter().fold(0xcbf2_9ce4_8422_2325, |h, b| {
            (h ^ u64::from(b)).wrapping_mul(0x100_0000_01b3)
        })
    }
    fn describe(vcpus: usize, en: [bool; 3], t: Transports, ram: u64) -> String {
        let p = launch_plan(vcpus, boot::ACPI_NVS.0, en, t, ram).unwrap();
        let tdig = fnv(p
            .tables
            .blobs
            .iter()
            .flat_map(|(a, b)| a.to_le_bytes().into_iter().chain(b.iter().copied())));
        let edig = fnv(p.layout.e820().unwrap().iter().flat_map(|e| {
            [e.addr, e.size, e.kind.into()]
                .into_iter()
                .flat_map(u64::to_le_bytes)
        }));
        let m: Vec<_> = p.manifest.iter().map(|r| (r.uid, r.base, r.gsi)).collect();
        format!(
            "{m:?}|{:?}|{tdig}|{}|{:?}|{edig}",
            p.functions,
            p.tables.mailbox,
            (p.layout.low_end, p.layout.high, p.layout.pci)
        )
    }
    #[test]
    fn launch_plans_are_unchanged() {
        use Transport::{Mmio, Pci};
        let mk = |disk, net, rng| Transports {
            disk,
            net,
            rng,
            console: Mmio,
        };
        let mixes = [
            Transports::all(Mmio),
            Transports::all(Pci),
            mk(Pci, Mmio, Mmio),
            mk(Mmio, Pci, Pci),
        ];
        let mut got = Vec::new();
        for vcpus in [1usize, 4] {
            for bits in 0..8u8 {
                let en = [bits & 1 != 0, bits & 2 != 0, bits & 4 != 0];
                for t in mixes {
                    got.push(describe(vcpus, en, t, 1 << 30));
                }
            }
        }
        got.push(describe(2, [true; 3], Transports::all(Pci), 5 << 30));
        assert_eq!(got.len(), EXPECTED.len());
        for (i, (g, e)) in got.iter().zip(EXPECTED).enumerate() {
            assert_eq!(g, e, "case {i}");
        }
    }
    /// Bad RAM sizes give the memory map's error (before any vCPU check in `prepare`), and 0 or 255 vCPUs the table
    /// builder's assert, as on the original code (checked at 33a99fb8 with a throwaway test on the original calls). The
    /// host-limit error is `require_vcpus`'s, which `prepare` still calls between the two.
    #[test]
    fn bad_sizes_give_the_original_errors() {
        let t = Transports::default();
        for ram in [1u64 << 20, (1 << 30) + 4096, 0] {
            let want = format!(
                "load refused: BAD_LAYOUT: BadLayout(\"low RAM end {:#x}: a 2 MiB multiple above the map's RAM start 0x1000000, at most 3 GiB\")",
                ram.min(exec::LOW_RAM_END)
            );
            assert_eq!(launch_layout([true; 3], t, ram).unwrap_err(), want);
            assert_eq!(
                launch_plan(1, boot::ACPI_NVS.0, [true; 3], t, ram).err(),
                Some(want)
            );
        }
        for v in [0usize, 255] {
            let r = std::panic::catch_unwind(|| {
                launch_plan(v, boot::ACPI_NVS.0, [false; 3], t, 1 << 30).map(|_| ())
            });
            let msg = *r.unwrap_err().downcast::<&str>().unwrap();
            assert_eq!(msg, "xAPIC MADT entries hold APIC IDs below 255");
        }
    }
    fn describe_original(
        vcpus: usize,
        rsdp: u64,
        en: [bool; 3],
        t: Transports,
        ram: u64,
    ) -> String {
        let [rng, disk, net] = en;
        let on_pci = |enabled: bool, t: Transport| enabled && t == Transport::Pci;
        let pci_rng = on_pci(rng, t.rng);
        let pci_disk = on_pci(disk, t.disk);
        let pci_net = on_pci(net, t.net);
        let layout = Layout {
            low_end: ram.min(exec::LOW_RAM_END),
            high: ram.saturating_sub(exec::LOW_RAM_END),
            pci: pci_rng || pci_disk || pci_net,
        };
        let e820 = layout.e820().unwrap();
        let (mut manifest, mut functions) = (Vec::new(), Vec::new());
        for (enabled, pci, resource, slot) in [
            (rng, pci_rng, crate::virtio::RNG_RESOURCE, RNG_SLOT),
            (disk, pci_disk, crate::virtio::BLOCK_RESOURCE, BLOCK_SLOT),
            (net, pci_net, crate::virtio::NET_RESOURCE, NET_SLOT),
        ] {
            if pci {
                functions.push((slot, resource.gsi));
            } else if enabled {
                manifest.push(resource);
            }
        }
        functions.sort_unstable();
        let tables = boot::acpi::build_with_devices(vcpus, rsdp, &manifest, &functions);
        let tdig = fnv(tables
            .blobs
            .iter()
            .flat_map(|(a, b)| a.to_le_bytes().into_iter().chain(b.iter().copied())));
        let edig = fnv(e820.iter().flat_map(|e| {
            [e.addr, e.size, e.kind.into()]
                .into_iter()
                .flat_map(u64::to_le_bytes)
        }));
        let m: Vec<_> = manifest.iter().map(|r| (r.uid, r.base, r.gsi)).collect();
        format!(
            "{m:?}|{functions:?}|{tdig}|{}|{:?}|{edig}",
            tables.mailbox,
            (layout.low_end, layout.high, layout.pci)
        )
    }
    /// Provenance: `describe_original` is the table-building code of the unmodified `prepare()` (33a99fb8), inline and
    /// verbatim, with the real RSDP (`boot::ACPI_NVS.0`). `cargo test -p natemu --lib -- --ignored original_logic`
    /// regenerates every pinned string from it and compares; `--nocapture` prints them.
    #[test]
    #[ignore]
    fn original_logic_matches_the_pins() {
        let mk = |disk, net, rng| Transports {
            disk,
            net,
            rng,
            console: Mmio,
        };
        use Transport::{Mmio, Pci};
        let mixes = [
            Transports::all(Mmio),
            Transports::all(Pci),
            mk(Pci, Mmio, Mmio),
            mk(Mmio, Pci, Pci),
        ];
        let mut got = Vec::new();
        for vcpus in [1usize, 4] {
            for bits in 0..8u8 {
                let en = [bits & 1 != 0, bits & 2 != 0, bits & 4 != 0];
                for t in mixes {
                    got.push(describe_original(vcpus, boot::ACPI_NVS.0, en, t, 1 << 30));
                }
            }
        }
        got.push(describe_original(
            2,
            boot::ACPI_NVS.0,
            [true; 3],
            Transports::all(Pci),
            5 << 30,
        ));
        for g in &got {
            println!("    \"{g}\",");
        }
        assert_eq!(got, EXPECTED);
    }
    const EXPECTED: [&str; 65] = [
        "[]|[]|18415784877665836832|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[]|18415784877665836832|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[]|18415784877665836832|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[]|18415784877665836832|6295552|(1073741824, 0, false)|11246234427793253812",
        "[(2, 4026540032, 18)]|[]|3444692196014619991|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(3, 18)]|17472892613165306677|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18)]|[]|3444692196014619991|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(3, 18)]|17472892613165306677|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[]|2944441749038479963|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(1, 16)]|12622583267433883005|6295552|(1073741824, 0, true)|10185747342727102902",
        "[]|[(1, 16)]|12622583267433883005|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[]|2944441749038479963|6295552|(1073741824, 0, false)|11246234427793253812",
        "[(2, 4026540032, 18), (0, 4026531840, 16)]|[]|10735271959007425989|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(1, 16), (3, 18)]|13792071679463081151|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18)]|[(1, 16)]|18131088272321461727|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[(3, 18)]|13311620853055878131|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(1, 4026535936, 17)]|[]|16074747690373841533|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(2, 17)]|3790317606079308799|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(1, 4026535936, 17)]|[]|16074747690373841533|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(2, 17)]|3790317606079308799|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18), (1, 4026535936, 17)]|[]|6048492150710054675|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(2, 17), (3, 18)]|17097972880732849769|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18), (1, 4026535936, 17)]|[]|6048492150710054675|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(2, 17), (3, 18)]|17097972880732849769|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16), (1, 4026535936, 17)]|[]|11636300326577872741|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(1, 16), (2, 17)]|10138172638859746085|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(1, 4026535936, 17)]|[(1, 16)]|18209356342807899333|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[(2, 17)]|2438782909292830041|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18), (0, 4026531840, 16), (1, 4026535936, 17)]|[]|11879701209480616447|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(1, 16), (2, 17), (3, 18)]|704756924329953899|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18), (1, 4026535936, 17)]|[(1, 16)]|15113218587310913917|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[(2, 17), (3, 18)]|18086586185133981191|6295552|(1073741824, 0, true)|10185747342727102902",
        "[]|[]|150797512702890517|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[]|150797512702890517|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[]|150797512702890517|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[]|150797512702890517|6295552|(1073741824, 0, false)|11246234427793253812",
        "[(2, 4026540032, 18)]|[]|17257018383566105557|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(3, 18)]|7668949667313669293|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18)]|[]|17257018383566105557|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(3, 18)]|7668949667313669293|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[]|8125311451615939063|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(1, 16)]|2561036390405840357|6295552|(1073741824, 0, true)|10185747342727102902",
        "[]|[(1, 16)]|2561036390405840357|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[]|8125311451615939063|6295552|(1073741824, 0, false)|11246234427793253812",
        "[(2, 4026540032, 18), (0, 4026531840, 16)]|[]|685955031401469039|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(1, 16), (3, 18)]|2282458943409544929|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18)]|[(1, 16)]|9922772884338512113|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[(3, 18)]|12990299782277238375|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(1, 4026535936, 17)]|[]|5065958628211627487|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(2, 17)]|590807252964984123|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(1, 4026535936, 17)]|[]|5065958628211627487|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(2, 17)]|590807252964984123|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18), (1, 4026535936, 17)]|[]|6743207254693383149|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(2, 17), (3, 18)]|10017376746478338695|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18), (1, 4026535936, 17)]|[]|6743207254693383149|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(2, 17), (3, 18)]|10017376746478338695|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16), (1, 4026535936, 17)]|[]|11477464473974112751|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(1, 16), (2, 17)]|3563416199795316955|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(1, 4026535936, 17)]|[(1, 16)]|15518724622879309227|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[(2, 17)]|17559797663622712537|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18), (0, 4026531840, 16), (1, 4026535936, 17)]|[]|14458187756917140890|6295552|(1073741824, 0, false)|11246234427793253812",
        "[]|[(1, 16), (2, 17), (3, 18)]|10527073856450925885|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(2, 4026540032, 18), (1, 4026535936, 17)]|[(1, 16)]|8807839146401585182|6295552|(1073741824, 0, true)|10185747342727102902",
        "[(0, 4026531840, 16)]|[(2, 17), (3, 18)]|11823347122285390973|6295552|(1073741824, 0, true)|10185747342727102902",
        "[]|[(1, 16), (2, 17), (3, 18)]|2398346617000464241|6295552|(3221225472, 2147483648, true)|161888604255309990",
    ];
}
