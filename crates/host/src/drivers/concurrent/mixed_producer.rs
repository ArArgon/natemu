//! Bounded per-owner compilation, including an independently retained stale job.
use super::*;
use crate::translation::{read_packet, write_packet};
use crate::translation::process_owner::OwnedChild;
use natemu_abi::families::mixed32;
use std::path::PathBuf;

struct Job {
    child: OwnedChild,
    directory: PathBuf,
    request: wire::Packet,
}
impl Job {
    fn finish(mut self) -> Result<wire::Packet> {
        if !self.child.wait().map_err(|e| e.to_string())?.success() {
            return Err("mixed worker failed".into());
        }
        let reply = read_packet(&self.directory.join("reply.bin"))?;
        if mixed32::reply(&self.request) != Some(reply) {
            return Err("mixed reply changed request or emitted body".into());
        }
        write_packet(&self.directory.join("reply-delivered.bin"), &reply)?;
        self.child.cleanup()?;
        Ok(reply)
    }
}
pub struct Compiler {
    producer: PathBuf,
    output: PathBuf,
    owner: u64,
    mode: u64,
    foreground: Option<Job>,
    pending: Option<Job>,
    pub requests: Vec<wire::Packet>,
}
impl Compiler {
    pub fn new(producer: PathBuf, output: PathBuf, owner: u64, mode: u64) -> Self {
        Self {
            producer,
            output,
            owner,
            mode,
            foreground: None,
            pending: None,
            requests: Vec::new(),
        }
    }
    fn start(&mut self, request: wire::Packet) -> Result<Job> {
        let directory = self
            .output
            .join(format!("request-{:03}", self.requests.len()));
        fs::create_dir(&directory).map_err(|e| e.to_string())?;
        write_packet(&directory.join("request.bin"), &request)?;
        let child = OwnedChild::spawn(
            std::path::Path::new("tools/i4v_mixed32_worker.py"),
            &self.producer,
            &directory,
        )?;
        self.requests.push(request);
        Ok(Job {
            child,
            directory,
            request,
        })
    }
    pub fn exchange(&mut self, mut packet: wire::Packet) -> Result<wire::Packet> {
        let operation = packet[0];
        packet[0] = mixed32::MISS;
        if packet[3] != self.owner || packet[8] != self.mode || mixed32::reply(&packet).is_none() {
            return Err("mixed request changes configured owner/mode/profile".into());
        }
        match operation {
            POLL_COMPILE => {
                let job = self.foreground.as_mut().ok_or("no mixed foreground job")?;
                if packet != job.request {
                    return Err("mixed poll changed identity".into());
                }
                if job.child.try_wait().map_err(|e| e.to_string())?.is_none() {
                    return Ok(wire::command(wire::BUSY));
                }
                self.foreground.take().unwrap().finish()
            }
            TAKE_COMPILE => {
                if self.foreground.is_some() {
                    return Err("mixed foreground slot full".into());
                }
                if packet
                    != self
                        .pending
                        .as_ref()
                        .ok_or("no deferred mixed job")?
                        .request
                {
                    return Err("mixed take changed identity".into());
                }
                self.foreground = self.pending.take();
                Ok(wire::command(wire::BUSY))
            }
            DEFER_COMPILE => {
                if self.pending.is_some() {
                    return Err("mixed deferred slot full".into());
                }
                self.pending = Some(self.start(packet)?);
                Ok(wire::command(wire::BUSY))
            }
            mixed32::MISS => {
                if self.foreground.is_some() {
                    return Err("mixed foreground slot full".into());
                }
                self.foreground = Some(self.start(packet)?);
                Ok(wire::command(wire::BUSY))
            }
            _ => Err("unsupported mixed compiler operation".into()),
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn configured_owner_and_mode_are_checked_before_worker_launch() {
        let mut c = Compiler::new("unused".into(), "unused".into(), 1, 0);
        for (owner, mode) in [(0, 0), (1, 1)] {
            let p = mixed32::request(37, owner, 1, 1, mode + 1, mode, 1).unwrap();
            assert!(c.exchange(p).is_err());
            assert!(c.requests.is_empty());
        }
        let mut p = mixed32::request(37, 1, 1, 1, 1, 0, 1).unwrap();
        p[0] = POLL_COMPILE;
        assert!(c.exchange(p).is_err());
        assert!(c.requests.is_empty());
    }
}
