//! One actual compilation, followed by four identity-bound resident deliveries.
use super::*;
use crate::i4v_stack::{read_packet, write_packet};
use natemu_abi::fence as f;
use std::{
    fs::File,
    path::PathBuf,
    process::{Child, Command, Stdio},
};

pub struct Compiler {
    config: f::Config,
    owner: u64,
    producer: PathBuf,
    worker: PathBuf,
    output: PathBuf,
    child: Option<Child>,
    original: Option<wire::Packet>,
    replies: Option<[wire::Packet; 4]>,
    pub requests: Vec<wire::Packet>,
}
impl Drop for Compiler {
    fn drop(&mut self) {
        if let Some(child) = &mut self.child {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}
impl Compiler {
    pub fn new(
        producer: PathBuf,
        worker: PathBuf,
        output: PathBuf,
        owner: u64,
        config: f::Config,
    ) -> Self {
        Self {
            config,
            owner,
            producer,
            worker,
            output,
            child: None,
            original: None,
            replies: None,
            requests: Vec::new(),
        }
    }
    fn start(&mut self, p: wire::Packet) -> Result<wire::Packet> {
        if self.original.is_some() || p[15] != 0 {
            return Err("fence compile must start at chunk zero exactly once".into());
        }
        let directory = self.output.join("request-000");
        fs::create_dir(&directory).map_err(|e| e.to_string())?;
        write_packet(&directory.join("request.bin"), &p)?;
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
        self.child = Some(child);
        self.original = Some(p);
        self.requests.push(p);
        Ok(wire::command(wire::BUSY))
    }
    fn deliver(&mut self, p: wire::Packet) -> Result<wire::Packet> {
        let index = p[15] as usize;
        let original = self.original.ok_or("fence delivery without compilation")?;
        if f::request(original[2], self.config, self.owner, index as u64) != Some(p)
            || self.requests.len() != if index == 0 { 1 } else { index }
        {
            return Err("fence chunk changes identity or sequence".into());
        }
        let packet = self.replies.as_ref().ok_or("fence body not ready")?[index];
        let directory = self.output.join("request-000");
        if index != 0 {
            write_packet(&directory.join(format!("request-chunk-{index}.bin")), &p)?;
            self.requests.push(p);
        }
        write_packet(
            &directory.join(format!("reply-delivered-{index}.bin")),
            &packet,
        )?;
        Ok(packet)
    }
    pub fn exchange(&mut self, mut p: wire::Packet) -> Result<wire::Packet> {
        let operation = p[0];
        p[0] = f::MISS;
        if f::decode(&p) != Some(self.config) || p[3] != self.owner {
            return Err("fence request changes configured owner/profile".into());
        }
        if operation == f::MISS {
            if self.original.is_none() {
                return self.start(p);
            }
            if p[15] == 0 {
                return Err("duplicate fence compilation".into());
            }
            return self.deliver(p);
        }
        if operation != POLL_COMPILE || self.original != Some(p) {
            return Err("fence poll changed compilation identity".into());
        }
        let child = self.child.as_mut().ok_or("no pending fence compiler")?;
        let Some(status) = child.try_wait().map_err(|e| e.to_string())? else {
            return Ok(wire::command(wire::BUSY));
        };
        if !status.success() {
            return Err("fence worker failed".into());
        }
        self.child.take();
        let mut replies = [[0; 16]; 4];
        for (i, packet) in replies.iter_mut().enumerate() {
            *packet = read_packet(&self.output.join(format!("request-000/reply-{i}.bin")))?;
            let request = f::request(p[2], self.config, self.owner, i as u64).unwrap();
            if f::reply(&request) != Some(*packet) {
                return Err("fence body chunk differs from compilation contract".into());
            }
        }
        self.replies = Some(replies);
        self.deliver(p)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn owner_profile_and_chunk_order_reject_before_launch() {
        let c = f::Config::new(4, 2).unwrap();
        let mut worker = Compiler::new("unused".into(), "unused".into(), "unused".into(), 1, c);
        for p in [
            f::request(37, c, 0, 0).unwrap(),
            f::request(37, f::Config::new(3, 2).unwrap(), 1, 0).unwrap(),
            f::request(37, c, 1, 1).unwrap(),
        ] {
            assert!(worker.exchange(p).is_err());
            assert!(worker.requests.is_empty());
        }
        let p = f::request(37, c, 1, 0).unwrap();
        worker.original = Some(p);
        worker.requests.push(p);
        worker.replies = Some(core::array::from_fn(|i| {
            f::reply(&f::request(37, c, 1, i as u64).unwrap()).unwrap()
        }));
        for (vm, chunk) in [(38, 1), (37, 2), (37, 0)] {
            assert!(
                worker
                    .exchange(f::request(vm, c, 1, chunk).unwrap())
                    .is_err()
            );
        }
        assert_eq!(worker.requests.len(), 1);
    }
}
