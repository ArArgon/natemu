//! Bounded per-owner compilation, including an independently retained stale job.
use crate::i4v_stack::{read_packet, write_packet};
use crate::*;
use natemu_abi::concurrency::{DEFER_COMPILE, POLL_COMPILE, TAKE_COMPILE};
use natemu_abi::controls;
use natemu_abi::translation as wire;
use std::{
    fs::File,
    path::PathBuf,
    process::{Child, Command, Stdio},
};

struct Job {
    child: Child,
    directory: PathBuf,
    request: wire::Packet,
}
impl Drop for Job {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
impl Job {
    fn finish(mut self) -> Result<wire::Packet> {
        if !self.child.wait().map_err(|e| e.to_string())?.success() {
            return Err("control worker failed".into());
        }
        let reply = read_packet(&self.directory.join("reply.bin"))?;
        if controls::reply(&self.request) != Some(reply) {
            return Err("control reply changed request or emitted body".into());
        }
        write_packet(&self.directory.join("reply-delivered.bin"), &reply)?;
        Ok(reply)
    }
}
pub struct Compiler {
    producer: PathBuf,
    worker: PathBuf,
    output: PathBuf,
    foreground: Option<Job>,
    pending: Option<Job>,
    pub requests: Vec<wire::Packet>,
}
impl Compiler {
    pub fn new(producer: PathBuf, worker: PathBuf, output: PathBuf) -> Self {
        Self {
            producer,
            worker,
            output,
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
        let child = Command::new("python3")
            .arg("-B")
            .arg(&self.worker)
            .arg("--request-dir")
            .arg(&directory)
            .arg("--producer")
            .arg(&self.producer)
            .stdin(Stdio::null())
            .stdout(File::create_new(directory.join("worker.stdout")).map_err(|e| e.to_string())?)
            .stderr(File::create_new(directory.join("worker.stderr")).map_err(|e| e.to_string())?)
            .spawn()
            .map_err(|e| e.to_string())?;
        self.requests.push(request);
        Ok(Job {
            child,
            directory,
            request,
        })
    }
    pub fn exchange(&mut self, mut packet: wire::Packet) -> Result<wire::Packet> {
        let operation = packet[0];
        packet[0] = controls::MISS;
        if controls::reply(&packet).is_none() {
            return Err("control request changes configured owner/mode/profile".into());
        }
        match operation {
            POLL_COMPILE => {
                let job = self
                    .foreground
                    .as_mut()
                    .ok_or("no control foreground job")?;
                if packet != job.request {
                    return Err("control poll changed identity".into());
                }
                if job.child.try_wait().map_err(|e| e.to_string())?.is_none() {
                    return Ok(wire::command(wire::BUSY));
                }
                self.foreground.take().unwrap().finish()
            }
            TAKE_COMPILE => {
                if self.foreground.is_some() {
                    return Err("control foreground slot full".into());
                }
                if packet
                    != self
                        .pending
                        .as_ref()
                        .ok_or("no deferred control job")?
                        .request
                {
                    return Err("control take changed identity".into());
                }
                self.foreground = self.pending.take();
                Ok(wire::command(wire::BUSY))
            }
            DEFER_COMPILE => {
                if self.pending.is_some() {
                    return Err("control deferred slot full".into());
                }
                self.pending = Some(self.start(packet)?);
                Ok(wire::command(wire::BUSY))
            }
            controls::MISS => {
                if self.foreground.is_some() {
                    return Err("control foreground slot full".into());
                }
                self.foreground = Some(self.start(packet)?);
                Ok(wire::command(wire::BUSY))
            }
            _ => Err("unsupported control compiler operation".into()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn compiler() -> Compiler {
        Compiler::new(
            "unused-producer".into(),
            "unused-worker".into(),
            "unused-output".into(),
        )
    }
    fn job(request: wire::Packet) -> Job {
        Job {
            child: Command::new("/usr/bin/true").spawn().unwrap(),
            directory: "unused-job".into(),
            request,
        }
    }
    #[test]
    fn deferred_take_and_poll_preserve_saved_context_before_consuming_job() {
        let request =
            controls::request(37, 1, 0, controls::RIP, &[0xb8, 0x11, 0, 1, 0], 2).unwrap();
        let mut compiler = compiler();
        compiler.pending = Some(job(request));
        for index in [2, 6, 7, 8, 12] {
            let mut changed = request;
            changed[0] = TAKE_COMPILE;
            changed[index] += 1;
            assert!(compiler.exchange(changed).is_err());
            assert!(compiler.pending.is_some());
            assert!(compiler.foreground.is_none());
        }
        let mut take = request;
        take[0] = TAKE_COMPILE;
        assert_eq!(compiler.exchange(take).unwrap(), wire::command(wire::BUSY));
        assert!(compiler.pending.is_none());
        let mut wrong_poll = request;
        wrong_poll[0] = POLL_COMPILE;
        wrong_poll[6] += 1;
        assert!(compiler.exchange(wrong_poll).is_err());
        assert!(compiler.foreground.is_some());
    }
    #[test]
    fn malformed_request_fails_before_worker_launch() {
        let request = controls::request(37, 1, 0, controls::RIP, &[0x89, 0x03], 1).unwrap();
        for index in [1, 3, 4, 5, 9, 10, 11, 13, 14, 15] {
            let mut bad = request;
            bad[index] ^= 1;
            let mut compiler = compiler();
            assert!(compiler.exchange(bad).is_err());
            assert!(compiler.requests.is_empty());
        }
    }
}
