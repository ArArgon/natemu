//! One asynchronous compilation per source snapshot, followed by three chunks.
use super::*;
use natemu_abi::stack;
use std::{
    fs::{File, OpenOptions},
    io::Write,
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
struct Ready {
    directory: PathBuf,
    request: wire::Packet,
    replies: [wire::Packet; 3],
    next: usize,
}
fn packet_file(path: &std::path::Path, packet: &wire::Packet) -> Result<()> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|e| e.to_string())?;
    for word in packet {
        file.write_all(&word.to_le_bytes())
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}
impl Job {
    fn finish(mut self) -> Result<Ready> {
        if !self.child.wait().map_err(|e| e.to_string())?.success() {
            return Err("concurrent stack worker failed".into());
        }
        let mut replies = [[0; 16]; 3];
        for (chunk, reply) in replies.iter_mut().enumerate() {
            let path = self.directory.join(format!("reply-{chunk}.bin"));
            let raw = read_input(path.to_str().ok_or("stack reply path")?, 128)?;
            if raw.len() != 128 {
                return Err("concurrent stack reply size differs".into());
            }
            for i in 0..16 {
                reply[i] = u64::from_le_bytes(raw[i * 8..i * 8 + 8].try_into().unwrap());
            }
            let mut expected = self.request;
            expected[15] = chunk as u64;
            if stack::reply_context(&expected) != Some(*reply) {
                return Err("concurrent stack reply changed request/body".into());
            }
        }
        Ok(Ready {
            directory: self.directory.clone(),
            request: self.request,
            replies,
            next: 0,
        })
    }
}
pub struct Compiler {
    producer: PathBuf,
    output: PathBuf,
    rip: u64,
    pending: Option<Job>,
    foreground: Option<Job>,
    ready: Option<Ready>,
    pub requests: Vec<wire::Packet>,
}
impl Compiler {
    pub fn new(producer: PathBuf, output: PathBuf, rip: u64) -> Self {
        Self {
            producer,
            output,
            rip,
            pending: None,
            foreground: None,
            ready: None,
            requests: Vec::new(),
        }
    }
    fn start(&mut self, request: wire::Packet) -> Result<Job> {
        let directory = self
            .output
            .join(format!("request-{:03}", self.requests.len()));
        fs::create_dir(&directory).map_err(|e| e.to_string())?;
        packet_file(&directory.join("request.bin"), &request)?;
        let child = Command::new("python3")
            .arg("-B")
            .arg("tools/i4v_stack_worker.py")
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
    fn deliver(&mut self, request: wire::Packet) -> Result<wire::Packet> {
        let ready = self
            .ready
            .as_mut()
            .ok_or("no completed stack compilation")?;
        let mut expected = ready.request;
        expected[15] = ready.next as u64;
        if request != expected || ready.next >= 3 {
            return Err("stack chunks changed identity or order".into());
        }
        let reply = ready.replies[ready.next];
        packet_file(
            &ready.directory.join(format!("request-{}.bin", ready.next)),
            &request,
        )?;
        packet_file(
            &ready
                .directory
                .join(format!("reply-delivered-{}.bin", ready.next)),
            &reply,
        )?;
        ready.next += 1;
        if ready.next == 3 {
            self.ready = None;
        }
        Ok(reply)
    }
    pub fn exchange(&mut self, mut packet: wire::Packet) -> Result<wire::Packet> {
        let operation = packet[0];
        packet[0] = stack::MISS;
        if packet[7] != self.rip || stack::reply_context(&packet).is_none() {
            return Err("invalid configured concurrent stack request".into());
        }
        if operation == POLL_COMPILE {
            let job = self
                .foreground
                .as_mut()
                .ok_or("no stack foreground compilation")?;
            if packet != job.request {
                return Err("stack poll changed request identity".into());
            }
            if job.child.try_wait().map_err(|e| e.to_string())?.is_none() {
                return Ok(wire::command(wire::BUSY));
            }
            self.ready = Some(self.foreground.take().unwrap().finish()?);
            return self.deliver(packet);
        }
        if operation == TAKE_COMPILE {
            if self.foreground.is_some() || self.ready.is_some() {
                return Err("stack foreground slot full".into());
            }
            let pending = self.pending.take().ok_or("no deferred stack compilation")?;
            if packet != pending.request {
                return Err("taking stack compilation changed request identity".into());
            }
            self.foreground = Some(pending);
            return Ok(wire::command(wire::BUSY));
        }
        if operation == stack::MISS && packet[15] != 0 {
            return self.deliver(packet);
        }
        if !matches!(operation, stack::MISS | DEFER_COMPILE) || packet[15] != 0 {
            return Err("unsupported concurrent stack service".into());
        }
        if operation == DEFER_COMPILE {
            if self.pending.is_some() {
                return Err("stack deferred slot full".into());
            }
            self.pending = Some(self.start(packet)?);
        } else {
            if self.foreground.is_some() || self.ready.is_some() {
                return Err("stack foreground slot full".into());
            }
            self.foreground = Some(self.start(packet)?);
        }
        Ok(wire::command(wire::BUSY))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn chunk_identity_and_order_fail_without_consuming_ready_output() {
        let directory = std::env::temp_dir().join(format!(
            "natemu-stack-chunks-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir(&directory).unwrap();
        let request = stack::request_context(73, 0, 1, 2, 1, 3, alias::source_rip(2), 1, 0);
        let replies = core::array::from_fn(|i| {
            let mut p = request;
            p[15] = i as u64;
            stack::reply_context(&p).unwrap()
        });
        let mut compiler = Compiler::new(PathBuf::new(), directory.clone(), request[7]);
        compiler.ready = Some(Ready {
            directory: directory.clone(),
            request,
            replies,
            next: 0,
        });
        for field in [2, 3, 4, 5, 6, 7, 13, 15] {
            let mut bad = request;
            bad[field] ^= 1;
            assert!(compiler.deliver(bad).is_err());
            assert_eq!(compiler.ready.as_ref().unwrap().next, 0);
        }
        for i in 0..3 {
            let mut next = request;
            next[15] = i as u64;
            assert_eq!(compiler.deliver(next).unwrap(), replies[i]);
            let retained = fs::read(directory.join(format!("request-{i}.bin"))).unwrap();
            assert_eq!(
                retained,
                next.into_iter()
                    .flat_map(u64::to_le_bytes)
                    .collect::<Vec<_>>()
            );
        }
        assert!(compiler.ready.is_none());
        assert!(compiler.deliver(request).is_err());
        fs::remove_dir_all(directory).unwrap();
    }
}
