//! Bounded per-owner instruction compilation for control and address profiles.
use crate::i4v_stack::{read_packet, write_packet};
use crate::*;
use natemu_abi::concurrency::{DEFER_COMPILE, POLL_COMPILE, TAKE_COMPILE};
use natemu_abi::translation as wire;
use natemu_abi::{address_concurrent as address, coherence, controls, ordering};
use std::{
    fs::File,
    path::PathBuf,
    process::{Child, Command, Stdio},
};

struct Job {
    child: Child,
    directory: PathBuf,
    request: wire::Packet,
    protocol: Protocol,
}
#[derive(Clone, Copy)]
enum Protocol {
    Controls,
    Address(address::Config),
    Ordering(ordering::Config),
    Coherence(coherence::Config),
}
impl Protocol {
    fn reply(self, request: &wire::Packet) -> Option<wire::Packet> {
        match self {
            Self::Controls => controls::reply(request),
            Self::Address(_) => address::reply(request),
            Self::Ordering(_) => ordering::reply(request),
            Self::Coherence(_) => coherence::reply(request),
        }
    }
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
        if self.protocol.reply(&self.request) != Some(reply) {
            return Err("control reply changed request or emitted body".into());
        }
        write_packet(&self.directory.join("reply-delivered.bin"), &reply)?;
        Ok(reply)
    }
}
pub struct Compiler {
    protocol: Protocol,
    owner: u64,
    incarnation: u64,
    concurrent: bool,
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
            protocol: Protocol::Controls,
            owner: 0,
            incarnation: 1,
            concurrent: false,
            producer,
            worker,
            output,
            foreground: None,
            pending: None,
            requests: Vec::new(),
        }
    }
    pub fn new_owner(
        producer: PathBuf,
        worker: PathBuf,
        output: PathBuf,
        owner: u64,
        incarnation: u64,
    ) -> Self {
        let mut compiler = Self::new(producer, worker, output);
        compiler.owner = owner;
        compiler.incarnation = incarnation;
        compiler.concurrent = true;
        compiler
    }
    pub fn new_address(
        producer: PathBuf,
        worker: PathBuf,
        output: PathBuf,
        owner: u64,
        config: address::Config,
    ) -> Self {
        let mut compiler = Self::new_owner(producer, worker, output, owner, 1);
        compiler.protocol = Protocol::Address(config);
        compiler
    }
    pub fn is_address(&self) -> bool {
        matches!(self.protocol, Protocol::Address(_))
    }
    pub fn new_ordering(
        producer: PathBuf,
        worker: PathBuf,
        output: PathBuf,
        owner: u64,
        config: ordering::Config,
    ) -> Self {
        let mut compiler = Self::new_owner(producer, worker, output, owner, 1);
        compiler.protocol = Protocol::Ordering(config);
        compiler
    }
    pub fn is_ordering(&self) -> bool {
        matches!(self.protocol, Protocol::Ordering(_))
    }
    pub fn new_coherence(
        producer: PathBuf,
        worker: PathBuf,
        output: PathBuf,
        owner: u64,
        config: coherence::Config,
    ) -> Self {
        let mut compiler = Self::new_owner(producer, worker, output, owner, 1);
        compiler.protocol = Protocol::Coherence(config);
        compiler
    }
    pub fn is_coherence(&self) -> bool {
        matches!(self.protocol, Protocol::Coherence(_))
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
            protocol: self.protocol,
        })
    }
    pub fn exchange(&mut self, mut packet: wire::Packet) -> Result<wire::Packet> {
        let operation = packet[0];
        let miss = match self.protocol {
            Protocol::Controls => controls::MISS,
            Protocol::Address(_) => address::MISS,
            Protocol::Ordering(_) => ordering::MISS,
            Protocol::Coherence(_) => coherence::MISS,
        };
        packet[0] = miss;
        if packet[3] != self.owner
            || packet[4] != self.incarnation
            || (!self.concurrent && packet[5] != 1)
            || self.protocol.reply(&packet).is_none()
            || matches!(self.protocol, Protocol::Address(config) if address::decode(&packet).map(|(actual, _)| actual) != Some(config))
            || matches!(self.protocol, Protocol::Ordering(config) if ordering::decode(&packet) != Some(config))
            || matches!(self.protocol, Protocol::Coherence(config) if coherence::decode(&packet) != Some(config))
        {
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
            operation if operation == miss => {
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
    #[test]
    fn coherence_worker_launch_is_bound_to_configured_owner_and_profile() {
        let config = coherence::Config::new(4, 0, 1).unwrap();
        for kind in 0..2 {
            let good = coherence::request(37, config, 2, kind).unwrap();
            for index in [1,3,4,6,7,8,9,10,11,12,13,14,15] {
                let mut bad = good;
                bad[index] ^= 1;
                let mut compiler = Compiler::new_coherence(
                    "unused".into(), "unused".into(), "unused".into(), 2, config);
                assert!(compiler.exchange(bad).is_err());
                assert!(compiler.requests.is_empty());
            }
            for other in [coherence::Config::new(3,0,1).unwrap(),
                          coherence::Config::new(4,3,1).unwrap(),
                          coherence::Config::new(4,0,2).unwrap()] {
                let bad = coherence::request(37,other,2,kind).unwrap();
                let mut compiler = Compiler::new_coherence(
                    "unused".into(), "unused".into(), "unused".into(), 2, config);
                assert!(compiler.exchange(bad).is_err());
                assert!(compiler.requests.is_empty());
            }
        }
    }
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
            protocol: Protocol::Controls,
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

    #[test]
    fn configured_owner_and_incarnation_are_not_taken_from_worker_packet() {
        let mut compiler =
            Compiler::new_owner("unused".into(), "unused".into(), "unused".into(), 2, 7);
        let packet =
            controls::request_owner(37, 2, 7, 3, 8, 2, controls::RIP + 512, &[0x89, 0x03], 4)
                .unwrap();
        compiler.pending = Some(job(packet));
        for (owner, incarnation) in [(1, 7), (2, 6)] {
            let mut wrong = controls::request_owner(
                37,
                owner,
                incarnation,
                3,
                8,
                2,
                controls::RIP + owner * 256,
                &[0x89, 0x03],
                4,
            )
            .unwrap();
            wrong[0] = TAKE_COMPILE;
            assert!(compiler.exchange(wrong).is_err());
            assert!(compiler.pending.is_some());
            assert!(compiler.foreground.is_none());
        }
        let mut take = packet;
        take[0] = TAKE_COMPILE;
        assert_eq!(compiler.exchange(take).unwrap(), wire::command(wire::BUSY));
        assert!(compiler.pending.is_none());
        assert_eq!(compiler.foreground.as_ref().unwrap().request, packet);
    }

    #[test]
    fn address_worker_cannot_choose_a_different_configured_actor_or_profile() {
        let config = address::Config::new(4, 3, 2).unwrap();
        let packet = address::request(
            37,
            config,
            1,
            1,
            2,
            false,
            0,
            config.fetch(1, false, 0).unwrap().gpa,
        )
        .unwrap();
        let mut compiler =
            Compiler::new_address("unused".into(), "unused".into(), "unused".into(), 1, config);
        let mut pending = job(packet);
        pending.protocol = Protocol::Address(config);
        compiler.pending = Some(pending);
        for other in [
            address::Config::new(3, 3, 2).unwrap(),
            address::Config::new(4, 4, 2).unwrap(),
            address::Config::new(4, 3, 3).unwrap(),
        ] {
            let mut wrong = address::request(
                37,
                other,
                1,
                1,
                2,
                false,
                0,
                other.fetch(1, false, 0).unwrap().gpa,
            )
            .unwrap();
            assert!(address::reply(&wrong).is_some());
            wrong[0] = TAKE_COMPILE;
            assert!(compiler.exchange(wrong).is_err());
            assert!(compiler.pending.is_some());
            assert!(compiler.requests.is_empty());
        }
        let mut take = packet;
        take[0] = TAKE_COMPILE;
        assert_eq!(compiler.exchange(take).unwrap(), wire::command(wire::BUSY));
        let mut wrong = packet;
        wrong[0] = POLL_COMPILE;
        wrong[2] += 1;
        assert!(compiler.exchange(wrong).is_err());
        assert_eq!(compiler.foreground.as_ref().unwrap().request, packet);
    }
    #[test]
    fn ordering_worker_cannot_change_config_or_owner_before_launch() {
        let config = ordering::Config::new(4, 9, 2, 37).unwrap();
        let mut compiler =
            Compiler::new_ordering("unused".into(), "unused".into(), "unused".into(), 1, config);
        for other in [
            ordering::Config::new(4, 8, 2, 37).unwrap(),
            ordering::Config::new(4, 9, 3, 37).unwrap(),
            ordering::Config::new(4, 9, 2, 38).unwrap(),
            ordering::Config::new(3, 9, 2, 37).unwrap(),
        ] {
            assert!(
                compiler
                    .exchange(ordering::request(37, other, 1).unwrap())
                    .is_err()
            );
            assert!(compiler.requests.is_empty());
        }
        assert!(
            compiler
                .exchange(ordering::request(37, config, 0).unwrap())
                .is_err()
        );
        let packet = ordering::request(37, config, 1).unwrap();
        let mut pending = job(packet);
        pending.protocol = Protocol::Ordering(config);
        compiler.pending = Some(pending);
        let mut take = packet;
        take[0] = TAKE_COMPILE;
        assert_eq!(compiler.exchange(take).unwrap(), wire::command(wire::BUSY));
        let mut wrong = packet;
        wrong[0] = POLL_COMPILE;
        wrong[2] += 1;
        assert!(compiler.exchange(wrong).is_err());
        assert_eq!(compiler.foreground.as_ref().unwrap().request, packet);
    }
}
