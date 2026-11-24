//! Generic PCI layer adapted from cloud-hypervisor (see the header of each file), and natemu's own bus: configuration
//! mechanism #1 (PCI Local Bus 3.0 3.2.2.3.2), a host bridge at 00:00.0, up to eight functions per slot, launch-time BAR
//! placement ([`place`]), and routing of the PCI memory window, and of the I/O window when a function has an I/O BAR, to
//! whichever function's BAR currently decodes an address.
pub mod configuration;
pub mod memory;
pub mod msix;
pub mod place;
pub mod testdev;

pub use memory::DeviceMemory;
use crate::devices::{Access, Device, Devices, Direct, mask};
use configuration::{
    PciBarRegionType, PciBridgeSubclass, PciClassCode, PciConfiguration, PciHeaderType,
};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicU64, Ordering::SeqCst},
};

pub mod platform {
    /// The 32-bit memory window the bus forwards, `[start, end)`: natemu's choice, and the window the ACPI root
    /// bridge's `_CRS` declares (`\_SB.PCI0`, ACPI 6.5 6.4.3.5). The registry is frozen at launch, so the bus
    /// registers all of it as one range and decodes BARs itself; a BAR outside it never decodes, as the root bridge
    /// forwards only what `_CRS` declares.
    pub const WINDOW: (u32, u32) = (0xE000_0000, 0xF000_0000);
    /// BAR0's launch-time address for the function in `slot`: the assignment a firmware would make (natemu boots
    /// without one), at natemu's choice of a 64 KiB stride; BAR format and sizing are PCI 3.0 6.2.5.1.
    pub const fn bar(slot: u8) -> u32 {
        WINDOW.0 + 0x1_0000 * slot as u32
    }
    /// The I/O window the bus forwards, inclusive: C000H-FFFFH (16 KiB), natemu's choice, free on this board (the PM
    /// block at B000H, the debug and profile ports at F4H-FBH, pvpanic at 505H). The bus registers it, and the root
    /// bridge's `_CRS` declares it, only when some function requests an I/O BAR ([`super::Bus::io_window`]).
    pub const IO_WINDOW: (u16, u16) = (0xC000, 0xFFFF);
}

/// Device slots on the bus (PCI 3.0 3.2.2.3.2: the device number is 5 bits).
const SLOTS: usize = 32;
/// Functions per slot (the function number is 3 bits).
const FUNCTIONS: usize = 8;
/// Base address registers per function (PCI 3.0 6.2.5.1: registers 10H-24H of a header type 0).
pub const BARS: usize = 6;
/// A deferred token keeps this many bits of the function's own token; above them the BAR (3 bits), the function (3
/// bits) and the slot (5 bits).
const TOKEN_BITS: u32 = 52;
/// Configuration register 3 (header type in bits 23:16): bit 7 of the header type marks a multi-function device (PCI 3.0
/// 6.2.1).
const MULTI_FUNCTION: u32 = 1 << 23;
/// The Command register's I/O Space and Memory Space enables (PCI 3.0 6.2.2).
const COMMAND_IO: u16 = 1 << 0;
const COMMAND_MEMORY: u16 = 1 << 1;

/// What a BAR decodes (PCI 3.0 6.2.5.1): 32-bit or 64-bit memory space, or I/O space.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BarKind {
    Mem32,
    Mem64,
    Io,
}
impl From<BarKind> for PciBarRegionType {
    fn from(kind: BarKind) -> Self {
        match kind {
            BarKind::Mem32 => PciBarRegionType::Memory32BitRegion,
            BarKind::Mem64 => PciBarRegionType::Memory64BitRegion,
            BarKind::Io => PciBarRegionType::IoRegion,
        }
    }
}
/// A BAR as it decodes now: its kind, base address and length in bytes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Bar {
    pub kind: BarKind,
    pub base: u64,
    pub len: u64,
}

/// One function of a slot: its configuration space and its BARs. A 64-bit BAR occupies index n and n + 1; `bars()[n +
/// 1]` is then `None`.
pub trait Function: Send + Sync {
    /// Configuration register `reg_idx` (offset / 4).
    fn config_read(&self, reg_idx: usize) -> u32;
    /// A write of 1, 2 or 4 bytes at byte `offset` of register `reg_idx`, as `PciConfiguration::write_config_register`.
    fn config_write(&self, reg_idx: usize, offset: u64, data: &[u8]);
    /// The BARs as they decode now: base masked with !(len - 1), the Command register's memory (`BarKind::Mem*`) or
    /// I/O (`BarKind::Io`) enable set; lock-free (a function publishes them from its configuration writes, as [`Bars`]
    /// does).
    fn bars(&self) -> [Option<Bar>; BARS];
    /// Launch-time BAR requests: (index, kind, len), len a power of two (memory at least 16, I/O at least 4).
    fn bar_requests(&self) -> Vec<(u8, BarKind, u64)>;
    /// Program BAR `index` to `base` at launch, decoding still off.
    fn assign(&self, index: u8, base: u64);
    /// An access inside BAR `bar`; `offset` from its base. Tokens must be below 2^52. An access to an I/O BAR must not
    /// defer (port accesses are synchronous, `Device::read`/`write`): the bus refuses a deferred one as a refused one,
    /// and the vCPU stops at the instruction (`unsupported_port`, status `device`).
    fn bar_access(&self, vcpu: usize, bar: u8, offset: u64, size: u64, value: Option<u64>) -> Access;
    /// Polls an access `bar_access` deferred on BAR `bar`, by the token it returned (as `Direct::poll`).
    fn bar_poll(&self, vcpu: usize, bar: u8, token: u64) -> Access;
    /// As `Device::quiesce`.
    fn quiesce(&self) -> crate::Result<bool>;
}

/// A function's BARs published for lock-free routing ([`Function::bars`]): their kinds and lengths fixed at creation,
/// their decoding bases republished from the live registers by [`Bars::publish`] under the function's configuration
/// lock after each configuration write. One atomic per BAR, so a 64-bit BAR's two halves are never read torn.
pub struct Bars {
    layout: [Option<(BarKind, u64)>; BARS],
    bases: [AtomicU64; BARS],
}
/// A base while the BAR does not decode (no decoding base has its low bits set).
const NO_BASE: u64 = u64::MAX;
impl Bars {
    /// The layout `requests` gives (as `Function::bar_requests`), nothing decoding yet. Panics on a malformed request:
    /// a length not a power of two or below the minimum (memory 16, I/O 4: PCI 3.0 6.2.5.1), an index past BAR5, or one
    /// a request already holds (a 64-bit BAR holds the next index too).
    pub fn new(requests: &[(u8, BarKind, u64)]) -> Self {
        let mut layout = [None; BARS];
        let mut held = [false; BARS];
        for &(index, kind, len) in requests {
            let i = usize::from(index);
            let upper = i + usize::from(kind == BarKind::Mem64);
            let min = if kind == BarKind::Io { 4 } else { 16 };
            assert!(
                upper < BARS && len.is_power_of_two() && len >= min && !held[i] && !held[upper],
                "BAR {index}: {kind:?} of {len:#x} bytes"
            );
            (held[i], held[upper]) = (true, true);
            layout[i] = Some((kind, len));
        }
        Bars {
            layout,
            bases: std::array::from_fn(|_| AtomicU64::new(NO_BASE)),
        }
    }
    /// Republish every BAR from `config`'s live registers, as hardware decodes them after any write width (the
    /// configuration's cached addresses follow only dword writes): each base masked with !(len - 1), since bits below
    /// the size are hardwired to zero (PCI 3.0 6.2.5.1), and nothing while the Command register's enable for its kind
    /// is clear. A size probe's all-ones then reads as the size mask, which lies outside the window and decodes
    /// nothing.
    pub fn publish(&self, config: &PciConfiguration) {
        let command = config.command();
        for (i, slot) in self.layout.iter().enumerate() {
            let Some((kind, len)) = *slot else { continue };
            let low = config.read_reg(4 + i);
            let (on, address) = match kind {
                BarKind::Mem32 => (command & COMMAND_MEMORY, u64::from(low & !0xf)),
                BarKind::Mem64 => (
                    command & COMMAND_MEMORY,
                    u64::from(config.read_reg(5 + i)) << 32 | u64::from(low & !0xf),
                ),
                BarKind::Io => (command & COMMAND_IO, u64::from(low & !0x3)),
            };
            let base = if on != 0 {
                address & !(len - 1)
            } else {
                NO_BASE
            };
            self.bases[i].store(base, SeqCst);
        }
    }
    /// The BARs as they decode now (for `Function::bars`).
    pub fn get(&self) -> [Option<Bar>; BARS] {
        std::array::from_fn(|i| {
            let (kind, len) = self.layout[i]?;
            let base = self.bases[i].load(SeqCst);
            (base != NO_BASE).then_some(Bar { kind, base, len })
        })
    }
    /// Program BAR `index` of `config` to `base` (for `Function::assign`) and republish: both halves of a 64-bit BAR.
    /// Panics on a base not aligned to the BAR's length, or above 4 GiB for a 32-bit or I/O BAR.
    pub fn assign(&self, config: &mut PciConfiguration, index: u8, base: u64) {
        let i = usize::from(index);
        let Some((kind, len)) = self.layout[i] else {
            panic!("BAR {index} was not requested")
        };
        assert!(
            base & (len - 1) == 0 && (kind == BarKind::Mem64 || base >> 32 == 0),
            "BAR {index}: {kind:?} of {len:#x} bytes at {base:#x}"
        );
        config.write_config_register(4 + i, 0, &(base as u32).to_le_bytes());
        if kind == BarKind::Mem64 {
            config.write_config_register(5 + i, 0, &((base >> 32) as u32).to_le_bytes());
        }
        self.publish(config);
    }
}

/// The host bridge, 00:00.0: QEMU `pc`'s i440FX (vendor 0x8086 Intel, device 0x1237 "440FX - 82441FX PMC"), class 0x06
/// (bridge), subclass 0x00 (host bridge) (PCI Code and ID Assignment spec, Base Class 06h), header type 0 (PCI 3.0
/// 6.2.1), no BARs, no capabilities.
struct HostBridge(Mutex<PciConfiguration>);
impl HostBridge {
    fn new() -> Self {
        HostBridge(Mutex::new(PciConfiguration::new(
            0x8086,
            0x1237,
            0,
            PciClassCode::BridgeDevice,
            &PciBridgeSubclass::HostBridge,
            None,
            PciHeaderType::Device,
            0,
            0,
            None,
        )))
    }
}
impl Function for HostBridge {
    fn config_read(&self, reg_idx: usize) -> u32 {
        let c = self.0.lock().unwrap_or_else(|e| e.into_inner());
        c.read_reg(reg_idx)
    }
    fn config_write(&self, reg_idx: usize, offset: u64, data: &[u8]) {
        // No BARs, so there is no reprogramming to act on.
        let mut c = self.0.lock().unwrap_or_else(|e| e.into_inner());
        c.write_config_register(reg_idx, offset, data);
    }
    fn bars(&self) -> [Option<Bar>; BARS] {
        [None; BARS]
    }
    fn bar_requests(&self) -> Vec<(u8, BarKind, u64)> {
        Vec::new()
    }
    fn assign(&self, index: u8, _: u64) {
        panic!("the host bridge has no BAR {index}")
    }
    fn bar_access(&self, _: usize, _: u8, _: u64, _: u64, _: Option<u64>) -> Access {
        Access::Refused
    }
    fn bar_poll(&self, _: usize, _: u8, _: u64) -> Access {
        Access::Refused
    }
    fn quiesce(&self) -> crate::Result<bool> {
        Ok(true)
    }
}

type Slots = [[Option<Arc<dyn Function>>; FUNCTIONS]; SLOTS];

/// The functions on bus 0, by slot and function (00:00.0 is the host bridge).
pub struct Bus {
    slots: Slots,
}
impl Default for Bus {
    fn default() -> Self {
        Self::new()
    }
}
impl Bus {
    pub fn new() -> Self {
        let mut slots: Slots = std::array::from_fn(|_| std::array::from_fn(|_| None));
        slots[0][0] = Some(Arc::new(HostBridge::new()));
        Bus { slots }
    }
    /// Function `function` of slot `slot`: slots 1-31 (slot 0 is the host bridge's), functions 0-7, function 0 before
    /// any other of its slot (PCI 3.0 6.2.1: a device implements function 0; software scans the others only when
    /// function 0 says it is multi-function).
    pub fn add(&mut self, slot: u8, function: u8, f: Arc<dyn Function>) -> crate::Result<()> {
        let (s, n) = (usize::from(slot), usize::from(function));
        if slot == 0 || s >= SLOTS || n >= FUNCTIONS {
            return Err(format!(
                "PCI {slot:02x}.{function}: slots 1..{SLOTS}, functions 0..{FUNCTIONS}"
            ));
        }
        let functions = &mut self.slots[s];
        if functions[n].is_some() {
            return Err(format!("PCI {slot:02x}.{function}: in use"));
        }
        if n != 0 && functions[0].is_none() {
            return Err(format!(
                "PCI {slot:02x}.{function}: function 0 of the slot first (PCI 3.0 6.2.1)"
            ));
        }
        functions[n] = Some(f);
        Ok(())
    }
    /// Whether some function requests an I/O BAR: then [`Bus::install`] registers the I/O window
    /// ([`platform::IO_WINDOW`]) and the root bridge's `_CRS` must declare it. Fixed by the functions added so far.
    pub fn io_window(&self) -> bool {
        self.requests().iter().any(|r| r.kind == BarKind::Io)
    }
    /// The BAR requests of the present functions, lowest (slot, function) first.
    fn requests(&self) -> Vec<place::Request> {
        let mut requests = Vec::new();
        for (s, functions) in self.slots.iter().enumerate() {
            for (n, f) in functions.iter().enumerate() {
                for (bar, kind, len) in f.iter().flat_map(|f| f.bar_requests()) {
                    requests.push(place::Request {
                        slot: s as u8,
                        function: n as u8,
                        bar,
                        kind,
                        len,
                    });
                }
            }
        }
        requests
    }
    /// Places every requested BAR ([`place::place`]; `Err` names a BAR that does not fit) and programs it, decoding
    /// still off; then registers the configuration ports (0xCF8..0xD00), the whole memory window and, when some BAR is
    /// an I/O BAR ([`Bus::io_window`]), the whole I/O window. All are fixed from here on.
    pub fn install(self, devices: &mut Devices) -> crate::Result<()> {
        let requests = self.requests();
        for p in place::place(&requests)? {
            let r = p.request;
            let f = self.slots[usize::from(r.slot)][usize::from(r.function)].as_ref();
            f.expect("a requesting function").assign(r.bar, p.base);
        }
        let io = requests.iter().any(|r| r.kind == BarKind::Io);
        let (ports, window, io_window) = self.into_parts();
        devices.register_ports(0xcf8, 8, Arc::new(Mutex::new(ports)))?;
        let size = u64::from(platform::WINDOW.1 - platform::WINDOW.0);
        devices.register(
            platform::WINDOW.0.into(),
            size,
            Arc::new(Mutex::new(window)),
        )?;
        if io {
            let (first, last) = platform::IO_WINDOW;
            devices.register_ports(
                first.into(),
                u32::from(last - first) + 1,
                Arc::new(Mutex::new(io_window)),
            )?;
        }
        Ok(())
    }
    fn into_parts(self) -> (ConfigPorts, Window, IoWindow) {
        // The present functions in (slot, function) order: the order the window decodes in.
        let mut present = Vec::new();
        for (s, functions) in self.slots.iter().enumerate() {
            for (n, f) in functions.iter().enumerate() {
                if let Some(f) = f {
                    present.push(Present {
                        at: (s << 3 | n) as u64,
                        f: f.clone(),
                    });
                }
            }
        }
        let slots = Arc::new(self.slots);
        let present: Arc<[Present]> = present.into();
        (
            ConfigPorts {
                address: 0,
                slots: slots.clone(),
            },
            Window {
                slots,
                present: present.clone(),
            },
            IoWindow { present },
        )
    }
}

/// Configuration mechanism #1 (PCI 3.0 3.2.2.3.2) on ports 0xCF8..0xD00: the address register at offsets 0-3, the
/// data register at 4-7. `Devices::port` delivers an access inside the range as one call with its offset; one that
/// runs past 0xCFF (e.g. 2 bytes at 0xCFF) it splits into bytes, the last of which falls on the unowned port 0xD00:
/// byte 3 of the data register is accessed alone and the rest reads as all-ones / is dropped.
struct ConfigPorts {
    address: u32,
    slots: Arc<Slots>,
}
impl ConfigPorts {
    /// The function the address register selects: enabled (bit 31), bus 0 (23:16), slot (15:11) and function (10:8)
    /// present; its register, and whether it is function 0 of a slot with other functions.
    fn selected(&self) -> Option<(&Arc<dyn Function>, usize, bool)> {
        let a = self.address;
        if a >> 31 != 1 || (a >> 16) & 0xff != 0 {
            return None;
        }
        let functions = &self.slots[((a >> 11) & 0x1f) as usize];
        let n = ((a >> 8) & 7) as usize;
        let multi = n == 0 && functions[1..].iter().any(Option::is_some);
        Some((functions[n].as_ref()?, ((a >> 2) & 0x3f) as usize, multi))
    }
}
impl Device for ConfigPorts {
    fn read(&mut self, _: usize, offset: u64, size: u64) -> Option<u64> {
        let ones = mask(size);
        Some(match offset {
            // The address register is read as a dword; narrower reads are not claimed (3.2.2.3.2).
            0 if size == 4 => self.address.into(),
            4..=7 if offset - 4 + size <= 4 => match self.selected() {
                Some((f, reg, multi)) => {
                    // The bus sets function 0's multi-function bit (QEMU does too), so functions need not know
                    // their neighbours.
                    let multi = if reg == 3 && multi { MULTI_FUNCTION } else { 0 };
                    let value = f.config_read(reg) | multi;
                    u64::from(value) >> (8 * (offset - 4)) & ones
                }
                None => ones,
            },
            _ => ones,
        })
    }
    fn write(&mut self, _: usize, offset: u64, size: u64, value: u64) -> bool {
        match offset {
            // Only a dword write reaches the address register (3.2.2.3.2); narrower ones are ignored. It keeps
            // only the defined bits: enable, bus, device, function, register (bits 30:24 and 1:0 read 0). QEMU's
            // i440FX stores the raw value; this follows the specification.
            0 if size == 4 => self.address = value as u32 & 0x80ff_fffc,
            4..=7 if offset - 4 + size <= 4 => {
                if let Some((f, reg, _)) = self.selected() {
                    f.config_write(reg, offset - 4, &value.to_le_bytes()[..size as usize]);
                }
            }
            _ => {}
        }
        true
    }
}

/// A present function and its place: slot << 3 | function.
struct Present {
    at: u64,
    f: Arc<dyn Function>,
}

/// The memory window as one registered range: each access goes to the function whose memory BAR decodes it. `Direct`,
/// so the registry never takes a mutex around a slow function; the function lists are immutable and
/// `Function::bars` lock-free.
#[derive(Clone)]
struct Window {
    /// By slot and function, for polls.
    slots: Arc<Slots>,
    /// The present functions, lowest (slot, function) first, for decoding.
    present: Arc<[Present]>,
}
impl Window {
    /// The function whose memory BAR decodes `[addr, addr + size)`, lowest (slot, function, BAR) first if BARs
    /// overlap (a guest error, kept deterministic): the function, its token prefix (slot << 6 | function << 3 | BAR),
    /// the BAR and the offset in it. A BAR not wholly inside the window does not decode; I/O BARs are not in it.
    fn decode(&self, addr: u64, size: u64) -> Option<(&Arc<dyn Function>, u64, u8, u64)> {
        let (lo, hi) = (u64::from(platform::WINDOW.0), u64::from(platform::WINDOW.1));
        self.present.iter().find_map(|p| {
            p.f.bars().iter().enumerate().find_map(|(i, bar)| {
                let bar = bar.filter(|b| b.kind != BarKind::Io)?;
                let end = bar.base.checked_add(bar.len)?;
                (bar.base >= lo && end <= hi && addr >= bar.base && addr + size <= end)
                    .then(|| (&p.f, p.at << 3 | i as u64, i as u8, addr - bar.base))
            })
        })
    }
}
impl Direct for Window {
    fn access(&self, vcpu: usize, offset: u64, size: u64, value: Option<u64>) -> Access {
        match self.decode(u64::from(platform::WINDOW.0) + offset, size) {
            Some((f, prefix, bar, off)) => match f.bar_access(vcpu, bar, off, size, value) {
                Access::Deferred(t) => {
                    assert!(
                        t >> TOKEN_BITS == 0,
                        "function token {t:#x} uses the slot, function and BAR bits"
                    );
                    Access::Deferred(prefix << TOKEN_BITS | t)
                }
                other => other,
            },
            // As an unclaimed address on a PC: reads all-ones, writes are dropped.
            None => Access::Completed(if value.is_none() { mask(size) } else { 0 }),
        }
    }
    fn poll(&self, vcpu: usize, token: u64) -> Access {
        let prefix = token >> TOKEN_BITS;
        let (slot, function, bar) = (prefix >> 6, prefix >> 3 & 7, prefix & 7);
        let f = self
            .slots
            .get(slot as usize)
            .and_then(|fs| fs[function as usize].as_ref());
        match f {
            Some(f) if bar < BARS as u64 => {
                match f.bar_poll(vcpu, bar as u8, token & ((1 << TOKEN_BITS) - 1)) {
                    Access::Deferred(t) => Access::Deferred(prefix << TOKEN_BITS | t),
                    other => other,
                }
            }
            _ => Access::Refused,
        }
    }
}
impl Device for Window {
    fn read(&mut self, _: usize, _: u64, _: u64) -> Option<u64> {
        None
    }
    fn write(&mut self, _: usize, _: u64, _: u64, _: u64) -> bool {
        false
    }
    fn access(&mut self, vcpu: usize, offset: u64, size: u64, value: Option<u64>) -> Access {
        Direct::access(self, vcpu, offset, size, value)
    }
    fn poll(&mut self, vcpu: usize, token: u64) -> Access {
        Direct::poll(self, vcpu, token)
    }
    /// Every function, even after one is not done (`Devices::quiesce` reaches only registered devices).
    fn quiesce(&mut self) -> crate::Result<bool> {
        let results: Vec<_> = self.present.iter().map(|p| p.f.quiesce()).collect();
        results.into_iter().try_fold(true, |done, r| Ok(done & r?))
    }
    fn direct(&self) -> Option<Arc<dyn Direct>> {
        Some(Arc::new(self.clone()))
    }
}

/// The I/O window ([`platform::IO_WINDOW`]) as one registered port range, present only when some function has an I/O
/// BAR: each access goes to the function whose I/O BAR decodes it, lowest (slot, function, BAR) first. An access lying
/// wholly in one BAR is one call; otherwise it is made byte by byte, each byte decoded on its own, as the registry does
/// at a range's end. A byte no BAR claims reads FFH and a write to it is dropped, as an unclaimed port on a PC (the
/// registry sees the whole window as owned, so under `strict_unassigned` such bytes are not refused, and the run report
/// counts them as owned). Port accesses are synchronous: a function's deferred or refused access is a refusal (the
/// vCPU stops), and earlier bytes of a split access stay made, as in the registry.
struct IoWindow {
    present: Arc<[Present]>,
}
impl IoWindow {
    /// The function whose I/O BAR decodes ports `[port, port + size)`: the BAR, and the offset in it. A BAR decodes
    /// only when it ends at or below 10000H: its upper 16 bits stay writable (PCI 3.0 6.2.5.1), and x86 port addresses
    /// have none.
    fn decode(&self, port: u64, size: u64) -> Option<(&Arc<dyn Function>, u8, u64)> {
        self.present.iter().find_map(|p| {
            p.f.bars().iter().enumerate().find_map(|(i, bar)| {
                let bar = bar.filter(|b| b.kind == BarKind::Io)?;
                let end = bar.base.checked_add(bar.len)?;
                (end <= 0x1_0000 && port >= bar.base && port + size <= end)
                    .then(|| (&p.f, i as u8, port - bar.base))
            })
        })
    }
    /// A read (`value` `None`, its value returned) or write of `size` bytes at `offset` in the window; `None` refused.
    fn serve(&self, vcpu: usize, offset: u64, size: u64, value: Option<u64>) -> Option<u64> {
        let port = u64::from(platform::IO_WINDOW.0) + offset;
        let call = |(f, bar, off): (&Arc<dyn Function>, u8, u64), size, value| {
            match f.bar_access(vcpu, bar, off, size, value) {
                Access::Completed(v) => Some(v & mask(size)),
                Access::Deferred(_) | Access::Refused => None,
            }
        };
        if let Some(d) = self.decode(port, size) {
            return call(d, size, value);
        }
        let mut out = 0;
        for k in 0..size {
            let byte = match self.decode(port + k, 1) {
                Some(d) => call(d, 1, value.map(|v| v >> (8 * k) & 0xff))?,
                None => 0xff,
            };
            out |= byte << (8 * k);
        }
        Some(out)
    }
}
impl Device for IoWindow {
    fn read(&mut self, vcpu: usize, offset: u64, size: u64) -> Option<u64> {
        self.serve(vcpu, offset, size, None)
    }
    fn write(&mut self, vcpu: usize, offset: u64, size: u64, value: u64) -> bool {
        self.serve(vcpu, offset, size, Some(value)).is_some()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::devices::{Device, Direct};
    use configuration::{PciBarConfiguration, PciBarPrefetchable};
    use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering::SeqCst};

    /// A function with movable BARs that records its accesses: writes are stored (`bar << 48 | offset << 8 | value`),
    /// reads return `id << 32 | bar << 24 | offset`, a read at [`DEFER`] defers with token 5, and polls are stored
    /// (`bar << 52 | token`) and defer again with the token plus one.
    struct Stub {
        id: u64,
        layout: [Option<(BarKind, u64)>; BARS],
        base: [AtomicU64; BARS],
        on: AtomicBool,
        quiesced: AtomicBool,
        done: bool,
        last_write: AtomicU64,
        last_config: AtomicU64,
        polled: AtomicU64,
    }
    const OFF: u64 = u64::MAX;
    const DEFER: u64 = 0xd0;
    impl Stub {
        /// BAR0 alone: 32 KiB of 32-bit memory at `base`.
        fn new(id: u64, base: u64) -> Arc<Self> {
            Self::with(id, &[(0, BarKind::Mem32, 0x8000, base)])
        }
        /// BARs (index, kind, len, base).
        fn with(id: u64, bars: &[(u8, BarKind, u64, u64)]) -> Arc<Self> {
            let mut layout = [None; BARS];
            let base: [AtomicU64; BARS] = std::array::from_fn(|_| AtomicU64::new(OFF));
            for &(i, kind, len, b) in bars {
                layout[usize::from(i)] = Some((kind, len));
                base[usize::from(i)].store(b, SeqCst);
            }
            Arc::new(Stub {
                id,
                layout,
                base,
                on: AtomicBool::new(true),
                quiesced: AtomicBool::new(false),
                done: true,
                last_write: AtomicU64::new(OFF),
                last_config: AtomicU64::new(OFF),
                polled: AtomicU64::new(OFF),
            })
        }
    }
    impl Function for Stub {
        fn config_read(&self, reg_idx: usize) -> u32 {
            (self.id as u32) << 24 | reg_idx as u32
        }
        fn config_write(&self, reg_idx: usize, offset: u64, data: &[u8]) {
            self.last_config
                .store((reg_idx as u64) << 16 | offset << 8 | u64::from(data[0]), SeqCst);
        }
        fn bars(&self) -> [Option<Bar>; BARS] {
            std::array::from_fn(|i| {
                let (kind, len) = self.layout[i]?;
                self.on.load(SeqCst).then(|| Bar {
                    kind,
                    base: self.base[i].load(SeqCst),
                    len,
                })
            })
        }
        fn bar_requests(&self) -> Vec<(u8, BarKind, u64)> {
            (0..BARS)
                .filter_map(|i| self.layout[i].map(|(k, len)| (i as u8, k, len)))
                .collect()
        }
        fn assign(&self, index: u8, base: u64) {
            self.base[usize::from(index)].store(base, SeqCst);
        }
        fn bar_access(&self, _: usize, bar: u8, offset: u64, _: u64, value: Option<u64>) -> Access {
            let bar = u64::from(bar);
            match value {
                Some(v) => {
                    self.last_write.store(bar << 48 | offset << 8 | v, SeqCst);
                    Access::Completed(0)
                }
                None if offset == DEFER => Access::Deferred(5),
                None => Access::Completed(self.id << 32 | bar << 24 | offset),
            }
        }
        fn bar_poll(&self, _: usize, bar: u8, token: u64) -> Access {
            self.polled.store(u64::from(bar) << 52 | token, SeqCst);
            Access::Deferred(token + 1)
        }
        fn quiesce(&self) -> crate::Result<bool> {
            self.quiesced.store(true, SeqCst);
            Ok(self.done)
        }
    }

    /// A function as the real ones are built: a `PciConfiguration` with its BARs, published through [`Bars`] from
    /// each configuration write (reads and writes as `Stub`'s).
    struct Live {
        id: u64,
        config: Mutex<PciConfiguration>,
        requests: Vec<(u8, BarKind, u64)>,
        bars: Bars,
        last_write: AtomicU64,
    }
    impl Live {
        /// BARs (index, kind, len, base), decoding off.
        fn new(id: u64, bars: &[(u8, BarKind, u64, u64)]) -> Arc<Self> {
            let mut config = PciConfiguration::new(
                0x1234,
                0x5678,
                0,
                PciClassCode::BridgeDevice,
                &PciBridgeSubclass::HostBridge,
                None,
                PciHeaderType::Device,
                0,
                0,
                None,
            );
            for &(i, kind, len, base) in bars {
                let bar = PciBarConfiguration::new(
                    i.into(),
                    len,
                    kind.into(),
                    PciBarPrefetchable::NotPrefetchable,
                )
                .set_address(base);
                config.add_pci_bar(&bar).unwrap();
            }
            let requests: Vec<_> = bars.iter().map(|&(i, k, len, _)| (i, k, len)).collect();
            let bars = Bars::new(&requests);
            bars.publish(&config);
            Arc::new(Live {
                id,
                config: Mutex::new(config),
                requests,
                bars,
                last_write: AtomicU64::new(OFF),
            })
        }
        fn write(&self, offset: usize, bytes: &[u8]) {
            self.config_write(offset / 4, (offset % 4) as u64, bytes);
        }
        fn reg(&self, reg_idx: usize) -> u32 {
            self.config_read(reg_idx)
        }
    }
    impl Function for Live {
        fn config_read(&self, reg_idx: usize) -> u32 {
            self.config.lock().unwrap().read_reg(reg_idx)
        }
        fn config_write(&self, reg_idx: usize, offset: u64, data: &[u8]) {
            let mut c = self.config.lock().unwrap();
            c.write_config_register(reg_idx, offset, data);
            self.bars.publish(&c);
        }
        fn bars(&self) -> [Option<Bar>; BARS] {
            self.bars.get()
        }
        fn bar_requests(&self) -> Vec<(u8, BarKind, u64)> {
            self.requests.clone()
        }
        fn assign(&self, index: u8, base: u64) {
            self.bars
                .assign(&mut self.config.lock().unwrap(), index, base);
        }
        fn bar_access(&self, _: usize, bar: u8, offset: u64, _: u64, value: Option<u64>) -> Access {
            let bar = u64::from(bar);
            match value {
                Some(v) => {
                    self.last_write.store(bar << 48 | offset << 8 | v, SeqCst);
                    Access::Completed(0)
                }
                None => Access::Completed(self.id << 32 | bar << 24 | offset),
            }
        }
        fn bar_poll(&self, _: usize, _: u8, _: u64) -> Access {
            Access::Refused
        }
        fn quiesce(&self) -> crate::Result<bool> {
            Ok(true)
        }
    }

    fn ports() -> Devices {
        ports_with(&[])
    }
    /// The configuration ports of a bus with functions (slot, function, f).
    fn ports_with(fs: &[(u8, u8, Arc<dyn Function>)]) -> Devices {
        let mut b = Bus::new();
        for (s, n, f) in fs {
            b.add(*s, *n, f.clone()).unwrap();
        }
        let mut d = Devices::new(1 << 30);
        b.install(&mut d).unwrap();
        d
    }
    fn win(fs: &[(u8, &Arc<Stub>)]) -> Window {
        let fs: Vec<_> = fs.iter().map(|&(s, f)| (s, 0, f.clone() as Arc<dyn Function>)).collect();
        win_at(&fs)
    }
    /// The window of a bus with functions (slot, function, f).
    fn win_at(fs: &[(u8, u8, Arc<dyn Function>)]) -> Window {
        let mut b = Bus::new();
        for (s, n, f) in fs {
            b.add(*s, *n, f.clone()).unwrap();
        }
        b.into_parts().1
    }
    /// The I/O window of a bus with functions (slot, function, f), BARs where the functions have them.
    fn io_at(fs: &[(u8, u8, Arc<dyn Function>)]) -> IoWindow {
        let mut b = Bus::new();
        for (s, n, f) in fs {
            b.add(*s, *n, f.clone()).unwrap();
        }
        b.into_parts().2
    }
    /// A read of `size` bytes at window address `addr`.
    fn read(w: &Window, addr: u64, size: u64) -> u64 {
        match w.access(0, addr - BASE, size, None) {
            Access::Completed(v) => v,
            other => panic!("read {addr:#x}: {other:?}"),
        }
    }
    const BASE: u64 = platform::WINDOW.0 as u64;
    const ONES: u64 = 0xffff_ffff;
    fn bar(slot: u8) -> u64 {
        platform::bar(slot).into()
    }
    /// Configuration address of register `reg` of slot.function.
    fn address(slot: u32, function: u32, reg: u32) -> u64 {
        u64::from(0x8000_0000 | slot << 11 | function << 8 | reg << 2)
    }

    #[test]
    fn type1_probe_as_linux_does_it() {
        let d = ports();
        assert_eq!(d.port(0, 0xcfb, 1, Some(0x80)), Ok(0)); // the probe's first, byte-wide write
        d.port(0, 0xcf8, 4, Some(0x8000_0000)).unwrap();
        assert_eq!(d.port(0, 0xcf8, 4, None), Ok(0x8000_0000));
        // pci_sanity_check: class 0x0600 at 00:00.0 (register 2, bits 31:16)
        d.port(0, 0xcf8, 4, Some(0x8000_0008)).unwrap();
        assert_eq!(d.port(0, 0xcfe, 2, None), Ok(0x0600));
        d.port(0, 0xcf8, 4, Some(0x8000_0000)).unwrap();
        assert_eq!(d.port(0, 0xcfc, 4, None), Ok(0x1237_8086));
        d.port(0, 0xcf8, 4, Some(0x8000_000c)).unwrap();
        assert_eq!(d.port(0, 0xcfe, 1, None), Ok(0)); // header type 0
    }
    #[test]
    fn absent_function_reads_all_ones_and_ignores_writes() {
        let d = ports();
        d.port(0, 0xcf8, 4, Some(0x8000_0000 | 5 << 11)).unwrap();
        assert_eq!(d.port(0, 0xcfc, 4, None), Ok(0xffff_ffff));
        assert_eq!(d.port(0, 0xcfd, 1, None), Ok(0xff));
        assert_eq!(d.port(0, 0xcfc, 2, None), Ok(0xffff));
        d.port(0, 0xcfc, 4, Some(0x1234)).unwrap();
        d.port(0, 0xcf8, 4, Some(0x8000_0000)).unwrap();
        assert_eq!(d.port(0, 0xcfc, 4, None), Ok(0x1237_8086));
    }
    #[test]
    fn byte_and_word_data_accesses_address_within_the_register() {
        let d = ports();
        d.port(0, 0xcf8, 4, Some(0x8000_0000)).unwrap();
        assert_eq!(d.port(0, 0xcfd, 1, None), Ok(0x80));
        assert_eq!(d.port(0, 0xcfe, 2, None), Ok(0x1237));
        assert_eq!(d.port(0, 0xcfd, 2, None), Ok(0x3780)); // 2 bytes at byte 1
        // A 2-byte access at 0xCFF crosses the end of the range: Devices::port splits it into byte 3 of the data
        // register and the unowned port 0xD00 (all-ones).
        assert_eq!(d.port(0, 0xcff, 2, None), Ok(0xff12));
    }
    #[test]
    fn address_without_enable_bit_reads_ones() {
        let d = ports();
        d.port(0, 0xcf8, 4, Some(0)).unwrap();
        assert_eq!(d.port(0, 0xcfc, 4, None), Ok(0xffff_ffff));
        assert_eq!(d.port(0, 0xcfc, 1, None), Ok(0xff));
    }
    #[test]
    fn byte_write_to_cf9_does_not_change_the_address() {
        let d = ports();
        d.port(0, 0xcf8, 4, Some(0x8000_0008)).unwrap();
        d.port(0, 0xcf9, 1, Some(0xff)).unwrap();
        d.port(0, 0xcf8, 2, Some(0)).unwrap();
        assert_eq!(d.port(0, 0xcf8, 4, None), Ok(0x8000_0008));
    }
    #[test]
    fn nonzero_bus_or_function_is_absent() {
        let d = ports();
        for addr in [0x8001_0000u64, 0x8000_0100] {
            d.port(0, 0xcf8, 4, Some(addr)).unwrap();
            assert_eq!(d.port(0, 0xcfc, 4, None), Ok(0xffff_ffff));
        }
    }
    #[test]
    fn only_a_dword_write_changes_the_address_and_cf8_reads_as_unclaimed_below_dword() {
        let d = ports();
        d.port(0, 0xcf8, 4, Some(0x8000_0800)).unwrap();
        assert_eq!(d.port(0, 0xcf8, 1, None), Ok(0xff));
        assert_eq!(d.port(0, 0xcf9, 1, None), Ok(0xff));
        assert_eq!(d.port(0, 0xcfa, 2, None), Ok(0xffff));
        d.port(0, 0xcfb, 1, Some(0)).unwrap();
        d.port(0, 0xcfa, 2, Some(0)).unwrap();
        assert_eq!(d.port(0, 0xcf8, 4, None), Ok(0x8000_0800));
        d.port(0, 0xcf8, 4, Some(0xffff_ffff)).unwrap();
        assert_eq!(d.port(0, 0xcf8, 4, None), Ok(0x80ff_fffc));
    }

    /// The BAR0-only routing tests from before functions per slot and BARs beyond BAR0, unchanged but for the call
    /// signatures and the token layout.
    mod bar0_routing_unchanged {
        use super::*;

        #[test]
        fn installed_window_serves_accesses_through_the_registry() {
            let a = Stub::new(1, bar(1));
            let mut b = Bus::new();
            b.add(1, 0, a.clone()).unwrap();
            let mut d = Devices::new(1 << 30);
            b.install(&mut d).unwrap();
            // the registry's own lookup, then the handle it would call without the mutex
            let (dev, off) = d.find(bar(1) + 0x10).unwrap();
            assert_eq!(off, 0x1_0010);
            let direct = dev.lock().unwrap().direct().unwrap();
            assert_eq!(
                direct.access(0, off, 4, None),
                Access::Completed(1 << 32 | 0x10)
            );
            assert_eq!(direct.access(0, off, 4, Some(9)), Access::Completed(0));
            assert_eq!(a.last_write.load(SeqCst), 0x10 << 8 | 9);
        }
        #[test]
        fn window_routes_to_the_bar_that_decodes_the_address() {
            let (a, b) = (Stub::new(1, bar(1)), Stub::new(2, bar(2)));
            let w = win(&[(1, &a), (2, &b)]);
            assert_eq!(
                w.access(0, 0x1_0010, 4, None),
                Access::Completed(1 << 32 | 0x10)
            );
            assert_eq!(
                w.access(0, 0x2_7ff8, 8, None),
                Access::Completed(2 << 32 | 0x7ff8)
            );
            w.access(0, 0x2_0004, 4, Some(7));
            assert_eq!(b.last_write.load(SeqCst), 4 << 8 | 7);
            assert_eq!(a.last_write.load(SeqCst), OFF);
        }
        #[test]
        fn a_moved_bar_decodes_at_its_new_address_only() {
            let a = Stub::new(1, bar(1));
            let w = win(&[(1, &a)]);
            a.base[0].store(BASE + 0x8_0000, SeqCst);
            assert_eq!(
                w.access(0, 0x1_0000, 4, None),
                Access::Completed(0xffff_ffff)
            );
            assert_eq!(
                w.access(0, 0x8_0008, 4, None),
                Access::Completed(1 << 32 | 8)
            );
        }
        #[test]
        fn memory_decoding_off_reads_all_ones_and_ignores_writes() {
            let a = Stub::new(1, bar(1));
            let w = win(&[(1, &a)]);
            a.on.store(false, SeqCst);
            assert_eq!(w.access(0, 0x1_0000, 2, None), Access::Completed(0xffff));
            assert_eq!(w.access(0, 0x1_0000, 4, Some(5)), Access::Completed(0));
            assert_eq!(a.last_write.load(SeqCst), OFF);
        }
        #[test]
        fn window_addresses_no_bar_claims_read_all_ones() {
            let a = Stub::new(1, bar(1));
            let w = win(&[(1, &a)]);
            for (off, size, want) in [
                (0x1_8000, 4, 0xffff_ffff),
                (0x0, 1, 0xff),
                (0xfff_fff8, 8, u64::MAX),
            ] {
                assert_eq!(w.access(0, off, size, None), Access::Completed(want));
            }
            assert_eq!(w.access(0, 0x1_8000, 4, Some(1)), Access::Completed(0));
            assert_eq!(a.last_write.load(SeqCst), OFF);
        }
        #[test]
        fn overlapping_bars_route_to_the_lowest_slot() {
            let (a, b) = (Stub::new(1, BASE + 0x4_0000), Stub::new(2, BASE + 0x4_0000));
            let w = win(&[(5, &b), (3, &a)]);
            assert_eq!(
                w.access(0, 0x4_0008, 4, None),
                Access::Completed(1 << 32 | 8)
            );
        }
        #[test]
        fn a_bar_outside_the_window_does_not_decode() {
            let a = Stub::new(1, 0xf000_0000);
            let w = win(&[(1, &a)]);
            assert_eq!(
                w.access(0, 0x0fff_fff8, 4, None),
                Access::Completed(0xffff_ffff)
            );
            // straddling the end of the window: the part inside must not decode either
            a.base[0].store(0xf000_0000 - 0x4000, SeqCst);
            assert_eq!(
                w.access(0, 0x0fff_c000, 4, None),
                Access::Completed(0xffff_ffff)
            );
        }
        #[test]
        fn deferred_tokens_carry_the_slot() {
            let a = Stub::new(1, bar(4));
            let w = win(&[(4, &a)]);
            assert_eq!(w.poll(0, 4 << 58 | 9), Access::Deferred(4 << 58 | 10));
            assert_eq!(w.poll(0, 7 << 58), Access::Refused);
        }
    }

    #[test]
    fn routes_to_bar_n_with_offset() {
        // BARs 1, 3 (64-bit, with 4) and 5 of one function, and BAR0 of another.
        let a = Stub::with(
            1,
            &[
                (1, BarKind::Mem32, 0x1000, BASE + 0x10_0000),
                (3, BarKind::Mem64, 0x1_0000, BASE + 0x20_0000),
                (5, BarKind::Mem32, 0x100, BASE + 0x30_0000),
            ],
        );
        let b = Stub::new(2, bar(2));
        let w = win(&[(1, &a), (2, &b)]);
        assert_eq!(read(&w, BASE + 0x10_0ffc, 4), 1 << 32 | 1 << 24 | 0xffc);
        assert_eq!(read(&w, BASE + 0x20_8000, 8), 1 << 32 | 3 << 24 | 0x8000);
        assert_eq!(read(&w, BASE + 0x30_0010, 2), 1 << 32 | 5 << 24 | 0x10);
        assert_eq!(read(&w, bar(2) + 0x20, 4), 2 << 32 | 0x20);
        // past BAR5's end, and one access straddling it: unclaimed
        assert_eq!(read(&w, BASE + 0x30_0100, 4), ONES);
        assert_eq!(read(&w, BASE + 0x30_00fe, 4), ONES);
        w.access(0, 0x20_0040, 4, Some(3));
        assert_eq!(a.last_write.load(SeqCst), 3 << 48 | 0x40 << 8 | 3);
        // an I/O BAR is not in the memory window, even at an address inside it
        let c = Stub::with(3, &[(0, BarKind::Io, 0x100, BASE + 0x40_0000)]);
        let w = win(&[(3, &c)]);
        assert_eq!(read(&w, BASE + 0x40_0000, 4), ONES);
    }

    #[test]
    fn mem64_halves_written_in_either_order() {
        // a: a 64-bit BAR2-3 of 64 KiB at E010_0000; b: BAR0 elsewhere, in a higher slot, so a would win an overlap.
        let a = Live::new(1, &[(2, BarKind::Mem64, 0x1_0000, 0xe010_0000)]);
        let b = Live::new(2, &[(0, BarKind::Mem32, 0x8000, bar(2))]);
        let fs: [(u8, u8, Arc<dyn Function>); 2] = [(1, 0, a.clone()), (2, 0, b.clone())];
        let w = win_at(&fs);
        for f in [&a, &b] {
            f.write(4, &[COMMAND_MEMORY as u8]);
        }
        let at = |addr: u64| read(&w, addr + 0x10, 4);
        let a_bar2 = 1 << 32 | 2 << 24 | 0x10;
        assert_eq!(at(0xe010_0000), a_bar2);
        // Upper half first: the BAR is at 1_E010_0000, outside the window; its low 32 bits must not decode, even when
        // another function's BAR sits there.
        a.write(0x1c, &1u32.to_le_bytes());
        assert_eq!(at(0xe010_0000), ONES);
        b.write(0x10, &0xe010_0000u32.to_le_bytes());
        assert_eq!(at(0xe010_0000), 2 << 32 | 0x10);
        // then the lower half: still above 4 GiB
        a.write(0x18, &0xe030_0000u32.to_le_bytes());
        assert_eq!(at(0xe030_0000), ONES);
        a.write(0x1c, &0u32.to_le_bytes());
        assert_eq!(at(0xe030_0000), a_bar2);
        assert_eq!(at(0xe010_0000), 2 << 32 | 0x10);
        // Sizing, lower half first: neither the probe nor the restore of the lower half decodes until the upper is back.
        let probe = |order: [usize; 2]| {
            for off in order {
                a.write(off, &u32::MAX.to_le_bytes());
                assert_eq!(at(0xe030_0000), ONES, "probe of {off:#x}");
                assert_eq!(at(0xe010_0000), 2 << 32 | 0x10);
            }
            assert_eq!(
                (a.reg(6), a.reg(7)),
                (0xffff_0004, 0xffff_ffff),
                "64 KiB, 64-bit"
            );
            let [first, second] = order;
            for (off, value) in [(first, restore(first)), (second, restore(second))] {
                assert_eq!(at(0xe030_0000), ONES, "before restoring {off:#x}");
                a.write(off, &value.to_le_bytes());
            }
            assert_eq!(at(0xe030_0000), a_bar2);
        };
        fn restore(off: usize) -> u32 {
            if off == 0x18 { 0xe030_0000 } else { 0 }
        }
        probe([0x18, 0x1c]);
        probe([0x1c, 0x18]);
    }

    #[test]
    fn sizing_probe_decodes_nothing() {
        // a's BAR0 (32 KiB) sits just below b's, so a base not masked to its size would reach into b's BAR.
        let a = Live::new(1, &[(0, BarKind::Mem32, 0x8000, bar(1) + 0x8000)]);
        let b = Live::new(2, &[(0, BarKind::Mem32, 0x8000, bar(2))]);
        let fs: [(u8, u8, Arc<dyn Function>); 2] = [(1, 0, a.clone()), (2, 0, b.clone())];
        let w = win_at(&fs);
        for f in [&a, &b] {
            f.write(4, &[COMMAND_MEMORY as u8]);
        }
        let (old, b_at) = (bar(1) + 0x8000, 2 << 32 | 0x10);
        assert_eq!(read(&w, old + 0x10, 4), 1 << 32 | 0x10);
        // A dword probe: the register reads the size mask, a's old base decodes nothing, b is untouched.
        a.write(0x10, &u32::MAX.to_le_bytes());
        assert_eq!(a.reg(4), 0xffff_8000, "32 KiB, 32-bit memory");
        assert_eq!(read(&w, old + 0x10, 4), ONES);
        assert_eq!(read(&w, bar(2) + 0x10, 4), b_at);
        a.write(0x10, &(old as u32).to_le_bytes());
        assert_eq!(read(&w, old + 0x10, 4), 1 << 32 | 0x10);
        // A probe a byte at a time (the register keeps bits below the size until it is read back): a goes to its
        // old base or nowhere, never into b's BAR.
        for byte in 0..4 {
            a.write(0x10 + byte, &[0xff]);
            assert_eq!(read(&w, bar(2) + 0x10, 4), b_at, "after byte {byte}");
            assert!(
                [ONES, 1 << 32 | 0x10].contains(&read(&w, old + 0x10, 4)),
                "after byte {byte}"
            );
        }
        a.write(0x10, &(old as u32).to_le_bytes());
        assert_eq!(read(&w, old + 0x10, 4), 1 << 32 | 0x10);
    }

    #[test]
    fn overlapping_bars_pick_lowest() {
        let x = BASE + 0x4_0000;
        // 03.0: BAR1 elsewhere, BAR4 at x; 03.1: BAR0 at x; 05.0: BAR0 at x.
        let a = Stub::with(
            1,
            &[
                (1, BarKind::Mem32, 0x1000, BASE + 0x8_0000),
                (4, BarKind::Mem32, 0x1000, x),
            ],
        );
        let (b, c) = (Stub::new(2, x), Stub::new(3, x));
        let fs: [(u8, u8, Arc<dyn Function>); 3] =
            [(5, 0, c.clone()), (3, 0, a.clone()), (3, 1, b.clone())];
        let w = win_at(&fs);
        // the lowest slot, and in it function 0 before function 1 whatever their BAR numbers
        assert_eq!(read(&w, x + 8, 4), 1 << 32 | 4 << 24 | 8);
        // in one function, the lowest BAR
        a.base[1].store(x, SeqCst);
        assert_eq!(read(&w, x + 8, 4), 1 << 32 | 1 << 24 | 8);
        a.on.store(false, SeqCst);
        assert_eq!(read(&w, x + 8, 4), 2 << 32 | 8);
        b.on.store(false, SeqCst);
        assert_eq!(read(&w, x + 8, 4), 3 << 32 | 8);
    }

    #[test]
    fn function_bits_select_function() {
        let (a, b) = (Stub::new(1, bar(2)), Stub::new(2, BASE));
        let d = ports_with(&[(2, 0, a.clone()), (2, 3, b.clone())]);
        for (function, want) in [(0, 1 << 24 | 2), (3, 2 << 24 | 2), (5, 0xffff_ffff)] {
            d.port(0, 0xcf8, 4, Some(address(2, function, 2))).unwrap();
            assert_eq!(d.port(0, 0xcfc, 4, None), Ok(want), "function {function}");
        }
        // a write through function 3 reaches it alone
        d.port(0, 0xcf8, 4, Some(address(2, 3, 0x11))).unwrap();
        d.port(0, 0xcfd, 1, Some(0x5a)).unwrap();
        assert_eq!(b.last_config.load(SeqCst), 0x11 << 16 | 1 << 8 | 0x5a);
        assert_eq!(a.last_config.load(SeqCst), OFF);
        // functions 1-7 of slot 0 (the host bridge's) are absent
        d.port(0, 0xcf8, 4, Some(address(0, 3, 0))).unwrap();
        assert_eq!(d.port(0, 0xcfc, 4, None), Ok(0xffff_ffff));
    }

    #[test]
    fn multifunction_bit_on_function_0_only_with_siblings() {
        let header = |d: &Devices, slot, function| {
            d.port(0, 0xcf8, 4, Some(address(slot, function, 3))).unwrap();
            d.port(0, 0xcfe, 1, None).unwrap()
        };
        // alone in its slot: header type 0, as the function reads it
        let d = ports_with(&[(2, 0, Stub::new(1, BASE))]);
        assert_eq!(header(&d, 2, 0), 0);
        // with a sibling: function 0's header type has bit 7; the sibling's, function 0's other registers, other
        // slots and the host bridge do not
        let d = ports_with(&[
            (2, 0, Stub::new(1, BASE)),
            (2, 1, Stub::new(2, BASE)),
            (4, 0, Stub::new(3, BASE)),
        ]);
        assert_eq!(header(&d, 2, 0), 0x80);
        d.port(0, 0xcf8, 4, Some(address(2, 0, 3))).unwrap();
        assert_eq!(d.port(0, 0xcfc, 4, None), Ok(0x0180_0003));
        assert_eq!(header(&d, 2, 1), 0);
        assert_eq!(header(&d, 4, 0), 0);
        assert_eq!(header(&d, 0, 0), 0);
        d.port(0, 0xcf8, 4, Some(address(2, 0, 2))).unwrap();
        assert_eq!(d.port(0, 0xcfc, 4, None), Ok(0x0100_0002));
    }

    #[test]
    fn function_without_function_0_is_refused() {
        let mut b = Bus::new();
        let f = |id| -> Arc<dyn Function> { Stub::new(id, BASE) };
        assert!(b.add(2, 1, f(1)).is_err(), "no function 0 yet");
        assert!(b.add(2, 7, f(1)).is_err());
        assert!(b.add(2, 0, f(1)).is_ok());
        assert!(b.add(2, 7, f(2)).is_ok());
        assert!(b.add(2, 1, f(3)).is_ok());
        assert!(b.add(2, 1, f(4)).is_err(), "in use");
        assert!(b.add(2, 8, f(5)).is_err(), "functions 0-7");
        assert!(b.add(0, 1, f(6)).is_err(), "the host bridge's slot");
    }

    #[test]
    fn token_carries_slot_function_bar() {
        let x = BASE + 0x10_0000;
        let a = Stub::new(1, x + 0x1_0000);
        let b = Stub::with(
            2,
            &[
                (0, BarKind::Mem32, 0x1000, BASE + 0x20_0000),
                (4, BarKind::Mem32, 0x1000, x),
            ],
        );
        let fs: [(u8, u8, Arc<dyn Function>); 2] = [(3, 0, a.clone()), (3, 2, b.clone())];
        let w = win_at(&fs);
        let prefix = 3 << 58 | 2 << 55 | 4 << 52;
        assert_eq!(
            w.access(0, x + DEFER - BASE, 4, None),
            Access::Deferred(prefix | 5)
        );
        assert_eq!(w.poll(0, prefix | 5), Access::Deferred(prefix | 6));
        assert_eq!(b.polled.load(SeqCst), 4 << 52 | 5, "BAR 4, the function's token");
        assert_eq!(a.polled.load(SeqCst), OFF);
        // an absent function, and a BAR number past BAR5
        assert_eq!(w.poll(0, 3 << 58 | 1 << 55 | 5), Access::Refused);
        assert_eq!(w.poll(0, 3 << 58 | 2 << 55 | 6 << 52 | 5), Access::Refused);
    }

    #[test]
    fn bus_add_refuses_slot_zero_occupied_and_out_of_range() {
        let mut b = Bus::new();
        let a: Arc<dyn Function> = Stub::new(1, BASE);
        assert!(b.add(0, 0, a.clone()).is_err());
        assert!(b.add(32, 0, a.clone()).is_err());
        assert!(b.add(31, 0, a.clone()).is_ok());
        assert!(b.add(31, 0, a).is_err());
    }
    #[test]
    fn quiesce_reaches_every_function_and_reports_done_only_when_all_are() {
        let (a, mut b) = (Stub::new(1, BASE), Stub::new(2, BASE));
        Arc::get_mut(&mut b).unwrap().done = false;
        let c = Stub::new(3, BASE);
        let fs: [(u8, u8, Arc<dyn Function>); 3] =
            [(1, 0, a.clone()), (1, 1, b.clone()), (3, 0, c.clone())];
        let mut w = win_at(&fs);
        assert_eq!(Device::quiesce(&mut w), Ok(false));
        assert!([&a, &b, &c].iter().all(|f| f.quiesced.load(SeqCst)));
        let d = Stub::new(4, BASE);
        assert_eq!(Device::quiesce(&mut win(&[(1, &d)])), Ok(true));
    }
    #[test]
    fn install_registers_the_whole_window_with_the_registry() {
        let d = ports();
        assert!(d.find(BASE).is_some() && d.find(BASE + 0xfff_f000).is_some());
        assert!(d.find(BASE - 0x1000).is_none() && d.find(platform::WINDOW.1.into()).is_none());
    }

    /// The ports the registry gives to some device: every port that strict mode does not refuse.
    fn owned_ports(d: &mut Devices) -> Vec<std::ops::Range<u32>> {
        d.strict = true;
        let mut ranges: Vec<std::ops::Range<u32>> = Vec::new();
        for p in 0..=0xffffu32 {
            if d.port(0, p as u16, 1, None).is_ok() {
                match ranges.last_mut() {
                    Some(r) if r.end == p => r.end = p + 1,
                    _ => ranges.push(p..p + 1),
                }
            }
        }
        d.strict = false;
        ranges
    }
    /// A 4-byte port read.
    fn inl(d: &Devices, port: u16) -> u64 {
        d.port(0, port, 4, None).unwrap()
    }

    #[test]
    fn install_places_and_programs_the_bars() {
        // 01.0: a virtio function's BAR0, created at its stride address; 03.0 and 03.1: BARs anywhere, placed at install
        let v = Live::new(1, &[(0, BarKind::Mem32, 0x8000, bar(1))]);
        let a = Live::new(
            2,
            &[
                (0, BarKind::Mem32, 0x1000, 0),
                (2, BarKind::Mem64, 0x1_0000, 0),
                (4, BarKind::Io, 0x40, 0),
            ],
        );
        let b = Live::new(3, &[(1, BarKind::Io, 0x100, 0)]);
        let before: Vec<u32> = (0..64).map(|r| v.reg(r)).collect();
        ports_with(&[(1, 0, v.clone()), (3, 0, a.clone()), (3, 1, b.clone())]);
        assert_eq!((0..64).map(|r| v.reg(r)).collect::<Vec<_>>(), before, "virtio BAR0 unchanged");
        let up = BASE as u32 + 0x20_0000;
        // 03.0 BAR0 keeps its stride; BAR2-3 (64-bit, below 4 GiB) above 2 MiB; the I/O BARs largest first
        assert_eq!(a.reg(4), bar(3) as u32);
        assert_eq!((a.reg(6), a.reg(7)), (up | 4, 0));
        assert_eq!(a.reg(8), 0xc100 | 1);
        assert_eq!(b.reg(5), 0xc000 | 1);
        // decoding stays off until the guest enables it
        assert!(a.bars().iter().chain(b.bars().iter()).all(Option::is_none));
        // a BAR that does not fit fails the launch, naming it
        let c = Live::new(4, &[(1, BarKind::Io, 0x4000, 0)]);
        let e = Live::new(5, &[(3, BarKind::Io, 0x4, 0)]);
        let mut bus = Bus::new();
        bus.add(6, 0, c).unwrap();
        bus.add(6, 1, e).unwrap();
        let err = bus.install(&mut Devices::new(1 << 30)).unwrap_err();
        assert!(err.contains("PCI 06.1 BAR3"), "{err}");
    }

    #[test]
    fn io_bar_routes_when_io_decoding_on() {
        let a = Live::new(1, &[(0, BarKind::Mem32, 0x1000, 0), (1, BarKind::Io, 0x40, 0)]);
        let b = Live::new(2, &[(2, BarKind::Io, 0x100, 0)]);
        let d = ports_with(&[(3, 0, a.clone()), (5, 0, b.clone())]);
        // b (256 bytes) at C000H, a (64) at C100H
        for f in [&a, &b] {
            f.write(4, &[COMMAND_IO as u8]);
        }
        assert_eq!(inl(&d, 0xc104), 1 << 24 | 4);
        assert_eq!(inl(&d, 0xc0fc), 2 << 24 | 0xfc);
        assert_eq!(d.port(0, 0xc13e, 2, None), Ok(0x3e), "the BAR's last two bytes");
        assert_eq!(d.port(0, 0xc011, 1, None), Ok(0x11));
        d.port(0, 0xc108, 2, Some(0xbeef)).unwrap();
        assert_eq!(a.last_write.load(SeqCst), 1 << 48 | 8 << 8 | 0xbeef);
        assert_eq!(b.last_write.load(SeqCst), OFF);
        // the memory enable alone does not open an I/O BAR; I/O enable does not open a memory BAR
        a.write(4, &[COMMAND_MEMORY as u8]);
        assert_eq!(inl(&d, 0xc104), ONES);
        assert_eq!(inl(&d, 0xc000), 2 << 24, "b still decodes");
        a.write(4, &[COMMAND_IO as u8]);
        assert_eq!(d.find(bar(3)).map(|(_, off)| off), Some(0x3_0000));
        assert_eq!(a.bars()[0], None);
        // a moved I/O BAR decodes at its new ports only
        a.write(0x14, &0xd000u32.to_le_bytes());
        assert_eq!(inl(&d, 0xc104), ONES);
        assert_eq!(inl(&d, 0xd004), 1 << 24 | 4);
        // overlapping I/O BARs: the lowest (slot, function, BAR)
        b.write(0x18, &0xd000u32.to_le_bytes());
        assert_eq!(inl(&d, 0xd010), 1 << 24 | 0x10);
    }

    #[test]
    fn io_bar_silent_when_io_decoding_off() {
        let a = Live::new(1, &[(0, BarKind::Io, 0x20, 0)]);
        let d = ports_with(&[(2, 0, a.clone())]);
        let silent = |why: &str| {
            assert_eq!(inl(&d, 0xc000), ONES, "{why}");
            assert_eq!(d.port(0, 0xc01f, 1, None), Ok(0xff), "{why}");
            d.port(0, 0xc004, 4, Some(5)).unwrap();
            assert_eq!(a.last_write.load(SeqCst), OFF, "{why}");
        };
        silent("as launched");
        a.write(4, &[(COMMAND_MEMORY | 1 << 2) as u8]);
        silent("memory and bus master enables only");
        a.write(4, &[COMMAND_IO as u8]);
        assert_eq!(inl(&d, 0xc01c), 0x1c);
        a.write(4, &[0]);
        silent("I/O enable cleared again");
    }

    #[test]
    fn io_access_straddling_bar_end() {
        // a: 16 ports at C000H, b: 16 ports at C010H, nothing at C020H
        let a = Stub::with(1, &[(0, BarKind::Io, 0x10, 0)]);
        let b = Stub::with(2, &[(3, BarKind::Io, 0x10, 0)]);
        let d = ports_with(&[(2, 0, a.clone()), (3, 0, b.clone())]);
        // bytes inside b, then unclaimed bytes past the last BAR
        assert_eq!(inl(&d, 0xc01e), 0xffff_0f0e, "b's last two bytes, then FFH");
        assert_eq!(d.port(0, 0xc01f, 2, None), Ok(0xff0f));
        // across the boundary of two BARs: each byte from its own function
        assert_eq!(d.port(0, 0xc00f, 2, None), Ok(0x000f));
        assert_eq!(inl(&d, 0xc00e), 0x0100_0f0e, "a's 0EH and 0FH, b's 00H and 01H");
        // a write: bytes inside are made one by one, the rest dropped
        d.port(0, 0xc01e, 4, Some(0x4433_2211)).unwrap();
        assert_eq!(b.last_write.load(SeqCst), 3 << 48 | 0xf << 8 | 0x22);
        d.port(0, 0xc00f, 2, Some(0x6655)).unwrap();
        assert_eq!(a.last_write.load(SeqCst), 0xf << 8 | 0x55);
        assert_eq!(b.last_write.load(SeqCst), 3 << 48 | 0x66);
    }

    #[test]
    fn io_bar_past_64k_does_not_decode() {
        // a: a 128 KiB I/O BAR at 0, so ports 0-1FFFFH, past 10000H; b: 64 ports whose upper 16 bits are set (the
        // register keeps them writable), its low 16 bits inside the window
        let a = Stub::with(1, &[(0, BarKind::Io, 0x2_0000, 0)]);
        let b = Stub::with(2, &[(0, BarKind::Io, 0x40, 0x1_c040)]);
        let fs: [(u8, u8, Arc<dyn Function>); 2] = [(2, 0, a.clone()), (3, 0, b.clone())];
        let mut w = io_at(&fs);
        for (offset, size) in [(0, 4), (0x40, 4), (0x3fff, 1)] {
            assert_eq!(w.read(0, offset, size), Some(mask(size)), "{offset:#x}");
        }
        assert!(w.write(0, 0x44, 4, 7));
        assert_eq!(a.last_write.load(SeqCst), OFF);
        assert_eq!(b.last_write.load(SeqCst), OFF);
        // ending at 10000H decodes
        b.base[0].store(0xffc0, SeqCst);
        assert_eq!(w.read(0, 0x3ffc, 4), Some(0x3c));
    }

    #[test]
    fn io_bar_deferral_is_refused() {
        let a = Stub::with(1, &[(0, BarKind::Io, 0x100, 0)]);
        let d = ports_with(&[(2, 0, a.clone())]);
        let refused = Err(natemu_abi::execution::mmio::DEVICE);
        assert_eq!(d.port(0, 0xc000 + DEFER as u16, 4, None), refused);
    }

    #[test]
    fn no_io_window_without_io_bars() {
        // a launch's bus: virtio functions with BAR0 only, one slot with two functions
        let mut bus = Bus::new();
        for (s, n) in [(1, 0), (2, 0), (3, 0), (3, 1)] {
            let f = Live::new(s.into(), &[(0, BarKind::Mem32, 0x8000, bar(s))]);
            bus.add(s, n, f).unwrap();
        }
        assert!(!bus.io_window());
        let mut d = Devices::new(1 << 30);
        bus.install(&mut d).unwrap();
        assert_eq!(owned_ports(&mut d), [0xcf8..0xd00], "the configuration ports alone");
        // one I/O BAR: the whole window, C000H-FFFFH
        let mut bus = Bus::new();
        bus.add(1, 0, Stub::new(1, 0)).unwrap();
        bus.add(4, 0, Stub::with(2, &[(5, BarKind::Io, 4, 0)])).unwrap();
        assert!(bus.io_window());
        let mut d = Devices::new(1 << 30);
        bus.install(&mut d).unwrap();
        assert_eq!(owned_ports(&mut d), [0xcf8..0xd00, 0xc000..0x1_0000]);
    }

    #[test]
    fn bars_refuse_malformed_requests_and_bases() {
        let refused = |requests: &[(u8, BarKind, u64)]| {
            std::panic::catch_unwind(|| Bars::new(requests)).is_err()
        };
        use BarKind::{Io, Mem32, Mem64};
        assert!(!refused(&[(0, Mem64, 16), (2, Io, 4), (5, Mem32, 1 << 31)]));
        for (bad, what) in [
            (&[(0, Mem64, 0x1000), (1, Mem32, 0x1000)][..], "BAR1 is BAR0-1's upper half"),
            (&[(2, Mem64, 0x1000), (3, Io, 4)][..], "BAR3 is BAR2-3's upper half"),
            (&[(1, Mem32, 0x1000), (0, Mem64, 0x1000)][..], "BAR0-1 over BAR1"),
            (&[(1, Io, 4), (1, Io, 4)][..], "BAR1 twice"),
            (&[(5, Mem64, 0x1000)][..], "BAR5-6"),
            (&[(0, Mem32, 8)][..], "memory below 16"),
            (&[(0, Io, 2)][..], "I/O below 4"),
            (&[(0, Mem32, 0x3000)][..], "not a power of two"),
        ] {
            assert!(refused(bad), "{what}");
        }
        let assign = |index: u8, base: u64| {
            let f = Live::new(1, &[(0, Mem32, 0x1000, 0), (2, Mem64, 0x1000, 0), (4, Io, 0x10, 0)]);
            let call = std::panic::AssertUnwindSafe(|| f.assign(index, base));
            std::panic::catch_unwind(call).is_err()
        };
        assert!(!assign(2, 1 << 32), "a 64-bit BAR above 4 GiB");
        assert!(assign(0, 1 << 32), "a 32-bit BAR above 4 GiB");
        assert!(assign(4, 0x1_0000_c000), "an I/O BAR above 4 GiB");
        assert!(assign(0, 0xe000_0800), "unaligned");
    }

    /// A [`Stub`] that counts its `bars()` calls: the decode's cost is one call per present function.
    struct Counting {
        inner: Arc<Stub>,
        calls: AtomicUsize,
    }
    impl Function for Counting {
        fn config_read(&self, reg_idx: usize) -> u32 {
            self.inner.config_read(reg_idx)
        }
        fn config_write(&self, reg_idx: usize, offset: u64, data: &[u8]) {
            self.inner.config_write(reg_idx, offset, data)
        }
        fn bars(&self) -> [Option<Bar>; BARS] {
            self.calls.fetch_add(1, SeqCst);
            self.inner.bars()
        }
        fn bar_requests(&self) -> Vec<(u8, BarKind, u64)> {
            self.inner.bar_requests()
        }
        fn assign(&self, index: u8, base: u64) {
            self.inner.assign(index, base)
        }
        fn bar_access(&self, vcpu: usize, bar: u8, offset: u64, size: u64, value: Option<u64>) -> Access {
            self.inner.bar_access(vcpu, bar, offset, size, value)
        }
        fn bar_poll(&self, vcpu: usize, bar: u8, token: u64) -> Access {
            self.inner.bar_poll(vcpu, bar, token)
        }
        fn quiesce(&self) -> crate::Result<bool> {
            self.inner.quiesce()
        }
    }

    /// An access walks the present functions once each, calling `bars()` once per function it reaches, not once per
    /// slot, function or BAR entry of the bus (the walk is over `Bus::into_parts`' flattened list).
    #[test]
    fn decode_calls_bars_once_per_present_function() {
        // functions on non-adjacent slots, slot 3 multi-function (functions 0 and 1)
        let at = [(3u8, 0u8, bar(3)), (3, 1, bar(5)), (9, 0, bar(9)), (20, 0, bar(20)), (31, 0, bar(31))];
        let fs: Vec<Arc<Counting>> = at
            .iter()
            .enumerate()
            .map(|(i, &(_, _, base))| {
                Arc::new(Counting { inner: Stub::new(i as u64 + 1, base), calls: AtomicUsize::new(0) })
            })
            .collect();
        let placed: Vec<(u8, u8, Arc<dyn Function>)> =
            at.iter().zip(&fs).map(|(&(s, n, _), f)| (s, n, f.clone() as Arc<dyn Function>)).collect();
        let calls = || fs.iter().map(|f| f.calls.swap(0, SeqCst)).collect::<Vec<_>>();
        let w = win_at(&placed);
        let io = io_at(&placed);
        // the last present function decodes: every function's bars() once
        assert_eq!(read(&w, bar(31) + 0x10, 4), 5 << 32 | 0x10);
        assert_eq!(calls(), [1; 5]);
        // nothing decodes: every function once again
        assert_eq!(read(&w, BASE + 0x3f_0000, 4), ONES);
        assert_eq!(calls(), [1; 5]);
        // the first decodes: the walk stops there
        assert_eq!(read(&w, bar(3) + 8, 4), 1 << 32 | 8);
        assert_eq!(calls(), [1, 0, 0, 0, 0]);
        // the I/O window walks the same list, once per function
        assert!(io.decode(0x1000, 4).is_none());
        assert_eq!(calls(), [1; 5]);
    }
}
