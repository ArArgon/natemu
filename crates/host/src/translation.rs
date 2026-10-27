//! Producer transport: hands a resident compile request to an out-of-process
//! worker, owns that process, and binds its reply to the original request.
pub(crate) mod process_owner;
use super::*;
use natemu_abi::translation as wire;
use process_owner::OwnedChild;
use std::{
    fs::File,
    io::Write,
    path::{Path, PathBuf},
};

/// Writes `packet` as 128 little-endian bytes to a new file at `path`.
pub(crate) fn write_packet(path: &Path, packet: &[u64; 16]) -> Result<()> {
    let mut file = File::create_new(path).map_err(|e| e.to_string())?;
    for word in packet {
        file.write_all(&word.to_le_bytes())
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Reads a 128-byte packet file written by a worker.
pub(crate) fn read_packet(path: &Path) -> Result<[u64; 16]> {
    let raw = read_input(path.to_str().ok_or("non-UTF8 packet path")?, 128)?;
    if raw.len() != 128 {
        return Err("packet file length differs".into());
    }
    Ok(core::array::from_fn(|i| {
        u64::from_le_bytes(raw[i * 8..i * 8 + 8].try_into().unwrap())
    }))
}

/// Runs one worker to completion on `request` in `output/request-NNN` and
/// returns that directory with the worker's outputs.
pub(crate) fn run_worker(
    worker: &str,
    producer: &str,
    output: &Path,
    request: &[u64; 16],
    number: usize,
) -> Result<PathBuf> {
    fs::create_dir_all(output).map_err(|e| e.to_string())?;
    let directory = output.join(format!("request-{number:03}"));
    fs::create_dir(&directory).map_err(|e| e.to_string())?;
    write_packet(&directory.join("request.bin"), request)?;
    let mut child = OwnedChild::spawn(Path::new(worker), Path::new(producer), &directory)?;
    let status = child.wait().map_err(|e| e.to_string())?;
    if !status.success() {
        return Err(format!("worker failed: {}", directory.display()));
    }
    child.cleanup()?;
    Ok(directory)
}

pub(super) struct Producer {
    pub(crate) child: OwnedChild,
    pub(crate) directory: PathBuf,
    request: wire::Packet,
    guest_rip: u64,
    bound_at_rip: bool,
}
impl Producer {
    pub(super) fn ready(&mut self) -> Result<bool> {
        self.child
            .try_wait()
            .map(|s| s.is_some())
            .map_err(|e| e.to_string())
    }
    pub(super) fn finish(mut self) -> Result<wire::Packet> {
        if !self.child.wait().map_err(|e| e.to_string())?.success() {
            return Err(format!("FEX worker failed: {}", self.directory.display()));
        }
        let raw = read_input(
            self.directory
                .join("reply.bin")
                .to_str()
                .ok_or("non-UTF8 reply path")?,
            128,
        )?;
        let reply = if self.bound_at_rip {
            bind_reply_at(&self.request, &raw, self.guest_rip)
        } else {
            bind_reply(&self.request, &raw)
        }?;
        self.child.cleanup()?;
        Ok(reply)
    }
}

fn bind_reply(request: &wire::Packet, raw: &[u8]) -> Result<wire::Packet> {
    bind_reply_using(request, raw, wire::SOURCE_VA, false)
}

fn bind_reply_at(request: &wire::Packet, raw: &[u8], guest_rip: u64) -> Result<wire::Packet> {
    bind_reply_using(request, raw, guest_rip, true)
}

fn bind_reply_using(
    request: &wire::Packet,
    raw: &[u8],
    guest_rip: u64,
    bound_at_rip: bool,
) -> Result<wire::Packet> {
    if raw.len() != 128 {
        return Err("wrong reply packet size".into());
    }
    let mut packet = [0; 16];
    for (i, word) in packet.iter_mut().enumerate() {
        *word = u64::from_le_bytes(raw[i * 8..i * 8 + 8].try_into().unwrap());
    }
    let mut body = [0; 44];
    wire::get_bytes(&packet[7..13], &mut body);
    let expected = if bound_at_rip {
        wire::reply_at(request, &body, guest_rip)
    } else {
        wire::reply(request, &body)
    }
    .map_err(|_| "unsupported worker output")?;
    if packet != expected {
        return Err("worker reply differs from its original request identity/source".into());
    }
    Ok(packet)
}

pub(super) fn start_worker(
    worker: &Path,
    producer: &Path,
    directory: &Path,
    request: wire::Packet,
) -> Result<Producer> {
    let mut job = start_worker_at(worker, producer, directory, request, wire::SOURCE_VA)?;
    job.bound_at_rip = false;
    Ok(job)
}

pub(super) fn start_worker_at(
    worker: &Path,
    producer: &Path,
    directory: &Path,
    request: wire::Packet,
    guest_rip: u64,
) -> Result<Producer> {
    if request[0] != wire::MISS {
        return Err("compiler requested without a resident miss".into());
    }
    if request[12] != guest_rip {
        return Err("compiler request differs from configured guest RIP".into());
    }
    fs::create_dir(directory).map_err(|e| e.to_string())?;
    let mut file = File::create_new(directory.join("request.bin")).map_err(|e| e.to_string())?;
    for word in request {
        file.write_all(&word.to_le_bytes())
            .map_err(|e| e.to_string())?;
    }
    let child = OwnedChild::spawn(worker, producer, directory)?;
    Ok(Producer {
        child,
        directory: directory.to_path_buf(),
        request,
        guest_rip,
        bound_at_rip: true,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn bytes(p: wire::Packet) -> Vec<u8> {
        p.into_iter().flat_map(u64::to_le_bytes).collect()
    }
    #[test]
    fn old_worker_cannot_impersonate_replacement_request() {
        let mut state = wire::Lifecycle::new(9);
        state.dispatch().unwrap();
        let old = state.packet();
        let reply = wire::reply(&old, &FEX_BODY).unwrap();
        assert_eq!(bind_reply(&old, &bytes(reply)), Ok(reply));
        state.change_context().unwrap();
        state.dispatch().unwrap();
        let forged = wire::reply(&state.packet(), &FEX_BODY).unwrap();
        assert!(state.validate_reply(&forged).is_ok());
        assert!(bind_reply(&old, &bytes(forged)).is_err());
    }
    #[test]
    fn malformed_worker_output_rejects_before_transfer() {
        let mut state = wire::Lifecycle::new(3);
        state.dispatch().unwrap();
        let request = state.packet();
        let good = wire::reply(&request, &FEX_BODY).unwrap();
        assert!(bind_reply(&request, &bytes(good)[..127]).is_err());
        for index in [0, 1, 2, 3, 4, 5, 6, 7, 12, 13, 15] {
            let mut wrong = good;
            wrong[index] ^= 1 << 32;
            assert!(bind_reply(&request, &bytes(wrong)).is_err());
        }
    }
    #[test]
    fn address_reply_is_bound_to_configured_rip_and_not_legacy_profile() {
        let high = natemu_abi::shadow::HIGH_BASE + wire::SOURCE_VA;
        let mut state = wire::Lifecycle::new_at(17, high);
        state.dispatch().unwrap();
        let request = state.packet();
        let good = wire::reply_at(&request, &FEX_BODY, high).unwrap();
        assert_eq!(bind_reply_at(&request, &bytes(good), high), Ok(good));
        assert!(bind_reply_at(&request, &bytes(good), wire::SOURCE_VA).is_err());
        assert!(bind_reply(&request, &bytes(good)).is_err());
        for field in [13, 14, 15] {
            let mut bad = good;
            bad[field] ^= 1;
            assert!(bind_reply_at(&request, &bytes(bad), high).is_err());
        }
    }
}
