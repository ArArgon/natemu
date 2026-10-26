//! Finite I2 service-stop wire protocol; all words are little-endian u64.
use crate::{DATA_VA, FEX_BODY, FEX_OR_BODY, PROFILE, VERSION};

pub const MODE: u64 = 2;
pub const SOURCE_VA: u64 = DATA_VA + 8192;
pub const XOR: [u8; 17] = [
    0x66, 0x0f, 0xef, 0xc1, 0x48, 0x01, 0xd1, 0x48, 0x8b, 0x03, 0x0f, 0x94, 0xc2, 0xf3, 0x0f, 0x7f,
    0x07,
];
pub const OR: [u8; 17] = {
    let mut source = XOR;
    source[2] = 0xeb;
    source
};
pub type Packet = [u64; 16];
pub const DISPATCH: u64 = 1;
pub const DELIVER: u64 = 2;
pub const SOURCE: u64 = 3;
pub const CONTEXT: u64 = 4;
pub const CANCEL: u64 = 5;
pub const SHUTDOWN: u64 = 6;
pub const READY: u64 = 10;
pub const MISS: u64 = 11;
pub const EXECUTED: u64 = 12;
pub const BUSY: u64 = 13;
pub const CHANGED: u64 = 14;
pub const CANCELLED: u64 = 15;
pub const STALE: u64 = 16;
pub const BAD_REPLY: u64 = 17;
pub const ADDRESS_PROFILE: u64 = 3;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(C)]
pub struct Lifecycle {
    pub vm: u64,
    pub guest_rip: u64,
    pub reply_profile: u64,
    pub source_generation: u64,
    pub context_generation: u64,
    pub serial: u64,
    pub pending: u64,
    pub variant: u64,
    pub cached: u64,
    pub executions: u64,
    pub requests: u64,
    pub rejections: u64,
    pub busy: u64,
    pub cancellations: u64,
    pub event: u64,
}
impl Lifecycle {
    pub const fn new(vm: u64) -> Self {
        Self {
            vm,
            guest_rip: SOURCE_VA,
            reply_profile: PROFILE as u64,
            source_generation: 1,
            context_generation: 1,
            serial: 0,
            pending: 0,
            variant: 0,
            cached: 0,
            executions: 0,
            requests: 0,
            rejections: 0,
            busy: 0,
            cancellations: 0,
            event: READY,
        }
    }
    pub const fn new_at(vm: u64, guest_rip: u64) -> Self {
        assert!(guest_rip == SOURCE_VA || guest_rip == crate::shadow::HIGH_BASE + SOURCE_VA);
        let mut result = Self::new(vm);
        result.guest_rip = guest_rip;
        result.reply_profile = ADDRESS_PROFILE;
        result
    }
    pub fn source(&self) -> [u8; 17] {
        match self.variant {
            0 => XOR,
            1 => OR,
            2 => crate::pressure::ALIAS,
            _ => crate::pressure::FP_ALIAS,
        }
    }
    pub fn packet(&self) -> Packet {
        let mut p = command(self.event);
        p[2] = self.pending;
        p[3] = self.source_generation;
        p[4] = self.context_generation;
        p[5] = self.vm;
        p[6] = if self.variant == 3 { 16 } else { 17 };
        put_bytes(&mut p[7..10], &self.source());
        p[10] = if self.variant >= 2 {
            2
        } else {
            self.reply_profile
        };
        p[11] = 0;
        p[12] = self.guest_rip;
        p[13] = self.requests;
        p[14] = self.executions;
        p[15] = self.serial;
        p
    }
    pub fn dispatch(&mut self) -> Result<u64, u64> {
        if self.pending != 0 {
            self.busy = self.busy.checked_add(1).ok_or(BAD_REPLY)?;
            self.event = BUSY;
            return Ok(BUSY);
        }
        if self.cached != 0 {
            self.event = EXECUTED;
            return Ok(EXECUTED);
        }
        self.serial = self.serial.checked_add(1).ok_or(BAD_REPLY)?;
        self.requests = self.requests.checked_add(1).ok_or(BAD_REPLY)?;
        self.pending = self.serial;
        self.event = MISS;
        Ok(MISS)
    }
    pub fn change_source(&mut self, source: [u8; 17]) -> Result<(), u64> {
        let kind = variant(&source).ok_or(BAD_REPLY)?;
        let next = self.source_generation.checked_add(1).ok_or(BAD_REPLY)?;
        self.source_generation = next;
        self.variant = kind;
        self.cached = 0;
        self.pending = 0;
        Ok(())
    }
    pub fn change_context(&mut self) -> Result<(), u64> {
        self.context_generation = self.context_generation.checked_add(1).ok_or(BAD_REPLY)?;
        self.cached = 0;
        self.pending = 0;
        Ok(())
    }
    pub fn cancel(&mut self, packet: &Packet) -> Result<(), u64> {
        if packet[2] == 0 || packet[2] != self.pending || packet[5] != self.vm {
            return Err(STALE);
        }
        self.cancellations = self.cancellations.checked_add(1).ok_or(BAD_REPLY)?;
        self.pending = 0;
        Ok(())
    }
    // Invalid/late deliveries never consume or mutate a currently occupied slot.
    pub fn validate_reply(&self, p: &Packet) -> Result<[u8; 44], u64> {
        if self.variant >= 2 || p[0] != DELIVER || p[1] != VERSION as u64 {
            return Err(BAD_REPLY);
        }
        if self.pending == 0
            || p[2] != self.pending
            || p[3] != self.source_generation
            || p[4] != self.context_generation
            || p[5] != self.vm
        {
            return Err(STALE);
        }
        if self.reply_profile == ADDRESS_PROFILE {
            if p[13] != self.guest_rip {
                return Err(STALE);
            }
            if p[14] != 64 || p[15] != ADDRESS_PROFILE {
                return Err(BAD_REPLY);
            }
        } else if self.reply_profile == PROFILE as u64 {
            let source = source_words(&p[13..16])?;
            if source != self.source() {
                return Err(STALE);
            }
        } else {
            return Err(BAD_REPLY);
        }
        if p[6] != 44 || p[12] >> 32 != 0 {
            return Err(BAD_REPLY);
        }
        let mut body = [0; 44];
        get_bytes(&p[7..13], &mut body);
        if body
            != if self.variant == 0 {
                FEX_BODY
            } else {
                FEX_OR_BODY
            }
        {
            return Err(BAD_REPLY);
        }
        Ok(body)
    }
}
pub fn command(kind: u64) -> Packet {
    let mut p = [0; 16];
    p[0] = kind;
    p[1] = VERSION as u64;
    p
}
pub fn variant(source: &[u8; 17]) -> Option<u64> {
    if *source == XOR {
        Some(0)
    } else if *source == OR {
        Some(1)
    } else {
        None
    }
}
pub fn put_bytes(words: &mut [u64], bytes: &[u8]) {
    words.fill(0);
    for (i, &b) in bytes.iter().enumerate() {
        words[i / 8] |= (b as u64) << ((i % 8) * 8);
    }
}
pub fn get_bytes(words: &[u64], bytes: &mut [u8]) {
    for (i, b) in bytes.iter_mut().enumerate() {
        *b = (words[i / 8] >> ((i % 8) * 8)) as u8;
    }
}
pub fn source_words(words: &[u64]) -> Result<[u8; 17], u64> {
    if words.len() != 3 || words[2] >> 8 != 0 {
        return Err(BAD_REPLY);
    }
    let mut source = [0; 17];
    get_bytes(words, &mut source);
    variant(&source).ok_or(BAD_REPLY)?;
    Ok(source)
}
pub fn reply(request: &Packet, body: &[u8]) -> Result<Packet, u64> {
    reply_for(request, body, SOURCE_VA, PROFILE as u64)
}
pub fn reply_at(request: &Packet, body: &[u8], guest_rip: u64) -> Result<Packet, u64> {
    reply_for(request, body, guest_rip, ADDRESS_PROFILE)
}
fn reply_for(request: &Packet, body: &[u8], guest_rip: u64, profile: u64) -> Result<Packet, u64> {
    if request[0] != MISS
        || request[1] != VERSION as u64
        || request[6] != 17
        || request[10] != profile
        || request[11] != 0
        || (guest_rip != SOURCE_VA && guest_rip != crate::shadow::HIGH_BASE + SOURCE_VA)
        || request[12] != guest_rip
        || body.len() != 44
    {
        return Err(BAD_REPLY);
    }
    let source = source_words(&request[7..10])?;
    if body
        != if source == XOR {
            &FEX_BODY
        } else {
            &FEX_OR_BODY
        }
    {
        return Err(BAD_REPLY);
    }
    let mut p = command(DELIVER);
    p[2..6].copy_from_slice(&request[2..6]);
    p[6] = 44;
    put_bytes(&mut p[7..13], body);
    if profile == ADDRESS_PROFILE {
        p[13..16].copy_from_slice(&[guest_rip, 64, ADDRESS_PROFILE]);
    } else {
        p[13..16].copy_from_slice(&request[7..10]);
    }
    Ok(p)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn high_rip_requires_explicit_request_binding() {
        let rip = crate::shadow::HIGH_BASE + SOURCE_VA;
        let mut s = Lifecycle::new_at(41, rip);
        s.dispatch().unwrap();
        let request = s.packet();
        assert_eq!(request[12], rip);
        assert_eq!(reply(&request, &FEX_BODY), Err(BAD_REPLY));
        assert!(
            s.validate_reply(&reply_at(&request, &FEX_BODY, rip).unwrap())
                .is_ok()
        );
        assert_eq!(reply_at(&request, &FEX_BODY, SOURCE_VA), Err(BAD_REPLY));
        let mut low_request = request;
        low_request[12] = SOURCE_VA;
        let wrong_rip = reply_at(&low_request, &FEX_BODY, SOURCE_VA).unwrap();
        assert_eq!(s.validate_reply(&wrong_rip), Err(STALE));
        assert_eq!(s.pending, 1);
    }
    #[test]
    fn stale_source_and_context_do_not_consume_replacement() {
        for source_change in [true, false] {
            let mut s = Lifecycle::new(42);
            assert_eq!(s.dispatch(), Ok(MISS));
            let old = reply(&s.packet(), &FEX_BODY).unwrap();
            if source_change {
                s.change_source(OR).unwrap()
            } else {
                s.change_context().unwrap()
            }
            assert_eq!(s.dispatch(), Ok(MISS));
            let newer = s.pending;
            assert_eq!(s.validate_reply(&old), Err(STALE));
            assert_eq!(s.pending, newer);
            let body = if source_change { FEX_OR_BODY } else { FEX_BODY };
            assert_eq!(
                s.validate_reply(&reply(&s.packet(), &body).unwrap()),
                Ok(body)
            );
        }
    }
    #[test]
    fn cancellation_capacity_and_late_reply() {
        let mut s = Lifecycle::new(7);
        s.dispatch().unwrap();
        let old = reply(&s.packet(), &FEX_BODY).unwrap();
        let id = s.pending;
        assert_eq!(s.dispatch(), Ok(BUSY));
        assert_eq!(s.pending, id);
        let mut c = command(CANCEL);
        c[2] = id;
        c[5] = s.vm;
        s.cancel(&c).unwrap();
        s.dispatch().unwrap();
        assert_eq!(s.validate_reply(&old), Err(STALE));
        assert_ne!(s.pending, id);
        assert!(
            s.validate_reply(&reply(&s.packet(), &FEX_BODY).unwrap())
                .is_ok()
        );
    }
    #[test]
    fn wire_padding_corruption_vm_and_wrap() {
        let mut s = Lifecycle::new(9);
        s.dispatch().unwrap();
        let p = reply(&s.packet(), &FEX_BODY).unwrap();
        for index in [0, 1, 2, 3, 4, 5, 6, 7, 12, 13, 15] {
            let mut bad = p;
            bad[index] ^= 1 << 32;
            assert!(s.validate_reply(&bad).is_err());
        }
        s.pending = 0;
        s.serial = u64::MAX;
        assert_eq!(s.dispatch(), Err(BAD_REPLY));
    }
}
