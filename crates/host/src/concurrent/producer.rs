use super::*;
use natemu_abi::translation as wire;
use std::path::PathBuf;

pub struct Compiler {
    worker: PathBuf,
    producer: PathBuf,
    output: PathBuf,
    pending: Option<translation::Producer>,
    foreground: Option<translation::Producer>,
    pub requests: Vec<wire::Packet>,
}
impl Compiler {
    pub fn new(producer: PathBuf, output: PathBuf) -> Result<Self> {
        fs::create_dir(&output).map_err(|e| e.to_string())?;
        Ok(Self {
            worker: PathBuf::from("tools/fex_worker.py"),
            producer,
            output,
            pending: None,
            foreground: None,
            requests: Vec::new(),
        })
    }
    pub fn exchange(&mut self, mut packet: wire::Packet) -> Result<wire::Packet> {
        let operation = packet[0];
        if operation == POLL_COMPILE {
            if self
                .foreground
                .as_mut()
                .ok_or("no foreground compilation")?
                .ready()?
            {
                return self.foreground.take().unwrap().finish();
            }
            return Ok(wire::command(wire::BUSY));
        }
        if operation == TAKE_COMPILE {
            if self.foreground.is_some() {
                return Err("foreground compiler slot full".into());
            }
            self.foreground = Some(self.pending.take().ok_or("no deferred compilation")?);
            return Ok(wire::command(wire::BUSY));
        }
        if !matches!(operation, wire::MISS | DEFER_COMPILE) {
            return Err("unsupported I4 compiler service".into());
        }
        if operation == DEFER_COMPILE && self.pending.is_some() {
            return Err("deferred compiler slot full".into());
        }
        if operation == wire::MISS && self.foreground.is_some() {
            return Err("foreground compiler slot full".into());
        }
        packet[0] = wire::MISS;
        let directory = self
            .output
            .join(format!("request-{:03}", self.requests.len()));
        let job = translation::start_worker(&self.worker, &self.producer, &directory, packet)?;
        self.requests.push(packet);
        if operation == DEFER_COMPILE {
            self.pending = Some(job);
            Ok(wire::command(wire::BUSY))
        } else {
            self.foreground = Some(job);
            Ok(wire::command(wire::BUSY))
        }
    }
}
