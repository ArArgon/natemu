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
    guest_rip: u64,
    address_profile: bool,
    stack: Option<stack_producer::Compiler>,
    mixed: Option<mixed_producer::Compiler>,
    controls: Option<crate::i4v_controls::producer::Compiler>,
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
            guest_rip: wire::SOURCE_VA,
            address_profile: false,
            stack: None,
            mixed: None,
            controls: None,
        })
    }
    pub fn new_alias(producer: PathBuf, output: PathBuf, guest_rip: u64) -> Result<Self> {
        let mut compiler = Self::new(producer, output)?;
        compiler.worker = PathBuf::from("tools/i4v_alias_worker.py");
        compiler.guest_rip = guest_rip;
        compiler.address_profile = true;
        Ok(compiler)
    }
    pub fn new_stack(producer: PathBuf, output: PathBuf, guest_rip: u64) -> Result<Self> {
        let mut compiler = Self::new(producer.clone(), output.clone())?;
        compiler.stack = Some(stack_producer::Compiler::new(producer, output, guest_rip));
        Ok(compiler)
    }
    pub fn new_mixed(producer: PathBuf, output: PathBuf, owner: u64, mode: u64) -> Result<Self> {
        let mut c = Self::new(producer.clone(), output.clone())?;
        c.mixed = Some(mixed_producer::Compiler::new(producer, output, owner, mode));
        Ok(c)
    }
    pub fn new_controls(
        producer: PathBuf,
        worker: PathBuf,
        output: PathBuf,
        owner: u64,
    ) -> Result<Self> {
        let mut c = Self::new(producer.clone(), output.clone())?;
        c.controls = Some(crate::i4v_controls::producer::Compiler::new_owner(
            producer, worker, output, owner, 1,
        ));
        Ok(c)
    }
    pub fn is_controls(&self) -> bool {
        self.controls.is_some()
    }
    pub fn is_mixed(&self) -> bool {
        self.mixed.is_some()
    }
    pub fn is_stack(&self) -> bool {
        self.stack.is_some()
    }
    pub fn exchange(&mut self, mut packet: wire::Packet) -> Result<wire::Packet> {
        if let Some(controls) = self.controls.as_mut() {
            let reply = controls.exchange(packet)?;
            self.requests.clone_from(&controls.requests);
            return Ok(reply);
        }
        if let Some(mixed) = self.mixed.as_mut() {
            let reply = mixed.exchange(packet)?;
            self.requests.clone_from(&mixed.requests);
            return Ok(reply);
        }
        if let Some(stack) = self.stack.as_mut() {
            let reply = stack.exchange(packet)?;
            self.requests.clone_from(&stack.requests);
            return Ok(reply);
        }
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
        let job = if self.address_profile {
            translation::start_worker_at(
                &self.worker,
                &self.producer,
                &directory,
                packet,
                self.guest_rip,
            )?
        } else {
            translation::start_worker(&self.worker, &self.producer, &directory, packet)?
        };
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
