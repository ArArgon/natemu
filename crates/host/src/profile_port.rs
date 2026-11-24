//! The EL0 sampler's control port (docs/restructure/el0-sampler-plan.md section 3), a test and profiling device beside
//! [`crate::debug_exit`] (F4H-F7H): registered at F8H-FBH only in a run that samples (`--profile`). Byte-oriented, since the
//! guest's simplest writer, `/dev/port`, writes one byte per port in sequence; a wider access is taken byte by byte, the
//! low byte at its first port.
//!
//! | Port | Write | Read |
//! | --- | --- | --- |
//! | F8H | 1 starts sampling, 2 stops it; other values are ignored and counted | 1 while sampling, else 0 |
//! | F9H | a mark: the byte is its number ([`natemu_abi::sample::MARK`] in `NAME.samples`) | 0 |
//! | FAH-FBH | reserved: ignored and counted | 0 |
//!
//! Every access is counted ([`natemu_abi::sample::C_PORT_ACCESSES`]): a sampling boot whose scripts never write the port
//! must count none, which shows that Linux itself never touches it.
use crate::devices::Device;
use crate::sampler::Sampler;
use natemu_abi::sample;
use std::sync::Arc;
use std::sync::atomic::Ordering::SeqCst;

pub const PORT: u16 = 0xf8;
pub const PORTS: u32 = 4;
/// F8H's commands.
pub const START: u8 = 1;
pub const STOP: u8 = 2;

pub struct ProfilePort(pub(crate) Arc<Sampler>);
impl ProfilePort {
    fn byte(&self, vcpu: usize, port: u64, value: u8) {
        let s = &self.0;
        match (port, value) {
            (0, START) => s.set(true, sample::BY_PORT, vcpu as u32),
            (0, STOP) => s.set(false, sample::BY_PORT, vcpu as u32),
            (1, n) => s.mark(vcpu, n, crate::clock::now()),
            _ => {
                s.port_ignored.fetch_add(1, SeqCst);
            }
        }
    }
}
impl Device for ProfilePort {
    fn read(&mut self, _vcpu: usize, offset: u64, size: u64) -> Option<u64> {
        self.0.port_accesses.fetch_add(1, SeqCst);
        // F8H's byte is the state; every other byte reads 0.
        Some(if offset == 0 && size >= 1 { u64::from(self.0.on()) } else { 0 })
    }
    fn write(&mut self, vcpu: usize, offset: u64, size: u64, value: u64) -> bool {
        self.0.port_accesses.fetch_add(1, SeqCst);
        for k in 0..size.min(8) {
            self.byte(vcpu, offset + k, (value >> (8 * k)) as u8);
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::devices::Devices;
    use crate::sampler::Options;
    use std::sync::Mutex;

    /// Each command through the port registry: F8H starts and stops (and reads back the state), F9H marks with its byte,
    /// every other value and FAH-FBH are ignored and counted, every access is counted; a word write is two bytes; nothing
    /// outside F8H-FBH is the device's (F7H is debug-exit's, FCH unassigned).
    #[test]
    fn the_control_port_starts_stops_marks_and_counts() {
        let name = std::env::temp_dir().join(format!("natemu-profile-port-{}", std::process::id()));
        let mut options = Options::new(&name);
        options.from_start = false;
        let sampler = Sampler::new(&options, 2, None).unwrap();
        let mut devices = Devices::new(1 << 30);
        devices
            .register_ports(PORT.into(), PORTS, Arc::new(Mutex::new(ProfilePort(sampler.clone()))))
            .unwrap();
        assert_eq!(devices.port(0, 0xf8, 1, None), Ok(0), "off until started");
        assert_eq!(devices.port(1, 0xf8, 1, Some(u64::from(START))), Ok(0));
        assert!(sampler.on());
        assert_eq!(devices.port(0, 0xf8, 1, None), Ok(1));
        assert_eq!(devices.port(0, 0xf9, 1, None), Ok(0));
        assert_eq!(devices.port(1, 0xf9, 1, Some(7)), Ok(0)); // mark 7
        assert_eq!(devices.port(0, 0xf8, 1, Some(3)), Ok(0)); // ignored
        assert!(sampler.on());
        assert_eq!(devices.port(0, 0xfa, 1, Some(1)), Ok(0)); // reserved: ignored
        assert_eq!(devices.port(0, 0xfb, 1, Some(2)), Ok(0)); // reserved: ignored
        assert!(sampler.on());
        // A word write at F8H: stop at F8H, mark 9 at F9H.
        assert_eq!(devices.port(0, 0xf8, 2, Some(0x0900 | u64::from(STOP))), Ok(0));
        assert!(!sampler.on());
        assert_eq!(sampler.port_accesses.load(SeqCst), 9);
        assert_eq!(sampler.port_ignored.load(SeqCst), 3);
        // Not the device's: F7H and FCH stay unassigned (all ones) and are not counted.
        assert_eq!(devices.port(0, 0xf7, 1, None), Ok(0xff));
        assert_eq!(devices.port(0, 0xfc, 1, None), Ok(0xff));
        assert_eq!(sampler.port_accesses.load(SeqCst), 9);
        // The toggles (sequence 1 and 2) and the marks wait for the drain, in order.
        let pending = sampler.pending_for_tests();
        let got: Vec<(u8, u64, u64, u32)> = pending
            .iter()
            .map(|&r| sample::HostRecord::decode(r))
            .map(|r| (r.kind, r.value1, r.value2, r.value3))
            .collect();
        assert_eq!(
            got,
            [
                (sample::TOGGLE, 1, 1, sample::BY_PORT),
                (sample::MARK, 7, 0, 1),
                (sample::TOGGLE, 2, 0, sample::BY_PORT),
                (sample::MARK, 9, 0, 0),
            ]
        );
        for ext in ["samples", "objects"] {
            let mut p = name.clone().into_os_string();
            p.push(format!(".{ext}"));
            let _ = std::fs::remove_file(p);
        }
    }
}
