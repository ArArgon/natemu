//! Finite allocated-map profile. Locations are producer output, not manual maps.
use crate::{translation as wire, *};
pub const MODE: u64 = 4;
pub const FP_MODE: u64 = 5;
pub const BEGIN: u64 = 7;
pub const CHUNK: u64 = 8;
pub const COMMIT: u64 = 9;
pub const ACK: u64 = 19;
pub const EXPORT_BYTES: usize = 1168;
pub const STAGING: u64 = PRIVATE + 0x22000;
pub const ACTIVE: u64 = PRIVATE + 0x23000;
pub const SPILLS: u64 = PRIVATE + 0x24000;
pub const ALIAS: [u8; 17] = {
    let mut s = wire::XOR;
    s[9] = 0;
    s
};
pub const FP_ALIAS: [u8; 17] = [
    0x0f, 0x58, 0xc1, 0x48, 0x01, 0xd1, 0x48, 0x8b, 0, 0x0f, 0x94, 0xc2, 0xf3, 0x0f, 0x7f, 7, 0,
];
// Exact native computation admitted in this finite producer profile. Exported
// locations still come from the allocator and are consumed at each fault site.
// Automatically exported allocation maps from the same pinned producer. This
// finite admission profile rejects every map alteration, including in-range but
// uninitialized/wrong roots. Runtime recovery still consumes the delivered maps.
pub const SITES: &[u8; 608] = include_bytes!("pressure_sites.bin");
pub const BODY: &[u8; 308] = include_bytes!("pressure_body.arm64");
pub const GPR_ORDER: [usize; 16] = [0, 3, 1, 2, 6, 7, 5, 4, 8, 9, 10, 11, 12, 13, 14, 15];
pub const ENTRY: [usize; 16] = [4, 6, 7, 5, 10, 11, 9, 8, 12, 13, 14, 15, 16, 17, 19, 29];
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Site {
    pub offset: u64,
    pub guest_offset: u64,
    pub write: u64,
    pub roots: [u64; 35],
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Export {
    pub body_len: u64,
    pub source_len: u64,
    pub spill_bytes: u64,
    pub source: [u8; 24],
    pub body: [u8; 512],
    pub sites: [Site; 2],
}
impl Export {
    pub fn validate(&self, source: &[u8; 17]) -> Result<(), u64> {
        let fp = *source == FP_ALIAS;
        if !fp && *source != ALIAS {
            return Err(wire::BAD_REPLY);
        }
        if self.body_len != 308
            || self.source_len != if fp { 16 } else { 17 }
            || self.spill_bytes != 768
            || self.source[..17] != source[..]
            || self.source[17..].iter().any(|&b| b != 0)
            || self.body[308..].iter().any(|&b| b != 0)
        {
            return Err(wire::BAD_REPLY);
        }
        for (i, &b) in self.body[..308].iter().enumerate() {
            // ADDPS instead of PXOR changes only the native vector instruction.
            let expected = if fp && (4..8).contains(&i) {
                [0x10, 0xd6, 0x31, 0x4e][i - 4]
            } else {
                BODY[i]
            };
            if b != expected {
                return Err(wire::BAD_REPLY);
            }
        }
        for (i, s) in self.sites.iter().enumerate() {
            if s.offset != [148, 296][i]
                || s.guest_offset != [7 - u64::from(fp), 13 - u64::from(fp)][i]
                || s.write != i as u64
            {
                return Err(wire::BAD_REPLY);
            }
            for (role, &loc) in s.roots.iter().enumerate() {
                let at = i * 304 + 24 + role * 8;
                let expected = u64::from_le_bytes(SITES[at..at + 8].try_into().unwrap());
                if loc != expected {
                    return Err(wire::BAD_REPLY);
                }
                let kind = loc & 255;
                let value = loc >> 8;
                let valid = if (16..32).contains(&role) {
                    kind == 2 && value < 32
                } else {
                    (kind == 1 && value < 31)
                        || (kind == 3 && value % 8 == 0 && value + 8 <= self.spill_bytes)
                };
                if !valid {
                    return Err(wire::BAD_REPLY);
                }
            }
        }
        Ok(())
    }
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Stream {
    pub identity: [u64; 4],
    pub received: u64,
    pub bytes: [u8; EXPORT_BYTES],
}
impl Stream {
    pub const ZERO: Self = Self {
        identity: [0; 4],
        received: 0,
        bytes: [0; EXPORT_BYTES],
    };
    pub fn ingest(&mut self, s: &wire::Lifecycle, p: &wire::Packet) -> Result<bool, u64> {
        let identity = [s.pending, s.source_generation, s.context_generation, s.vm];
        if p[1] != VERSION as u64 {
            return Err(wire::BAD_REPLY);
        }
        if s.pending == 0 || p[2..6] != identity {
            return Err(wire::STALE);
        }
        match p[0] {
            BEGIN => {
                if p[6] != EXPORT_BYTES as u64 || p[7..].iter().any(|&x| x != 0) {
                    return Err(wire::BAD_REPLY);
                }
                self.identity = identity;
                self.received = 0;
                self.bytes.fill(0);
                Ok(false)
            }
            CHUNK => {
                if self.identity != identity {
                    return Err(wire::STALE);
                }
                let at = self.received as usize;
                if p[6] != self.received || at >= EXPORT_BYTES {
                    return Err(wire::BAD_REPLY);
                }
                let count = 72.min(EXPORT_BYTES - at);
                let mut bytes = [0; 72];
                wire::get_bytes(&p[7..], &mut bytes);
                if bytes[count..].iter().any(|&b| b != 0) {
                    return Err(wire::BAD_REPLY);
                }
                self.bytes[at..at + count].copy_from_slice(&bytes[..count]);
                self.received += count as u64;
                Ok(false)
            }
            COMMIT => {
                if self.identity != identity {
                    return Err(wire::STALE);
                }
                if self.received != EXPORT_BYTES as u64 || p[6..].iter().any(|&x| x != 0) {
                    return Err(wire::BAD_REPLY);
                }
                Ok(true)
            }
            _ => Err(wire::BAD_REPLY),
        }
    }
}
const _: () = assert!(core::mem::size_of::<Export>() == EXPORT_BYTES);

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn malformed_and_stale_stream_cannot_publish() {
        let mut state = wire::Lifecycle::new(7);
        state.variant = 2;
        state.dispatch().unwrap();
        let mut stream = Stream::ZERO;
        let request = state.packet();
        let packet = |command| {
            let mut p = wire::command(command);
            p[2..6].copy_from_slice(&request[2..6]);
            p
        };
        assert_eq!(stream.ingest(&state, &packet(COMMIT)), Err(wire::STALE));
        let mut begin = packet(BEGIN);
        begin[6] = EXPORT_BYTES as u64;
        assert_eq!(stream.ingest(&state, &begin), Ok(false));
        assert_eq!(stream.ingest(&state, &packet(COMMIT)), Err(wire::BAD_REPLY));
        let mut chunk = packet(CHUNK);
        chunk[6] = 72;
        assert_eq!(stream.ingest(&state, &chunk), Err(wire::BAD_REPLY));
        state.change_context().unwrap();
        state.dispatch().unwrap();
        assert_eq!(stream.ingest(&state, &begin), Err(wire::STALE));
        assert_eq!(stream.received, 0);
        // Last chunk has 16 data bytes and 56 padding bytes. A rejected final
        // packet must leave even the accepted prefix unchanged.
        state = wire::Lifecycle::new(7);
        state.variant = 2;
        state.dispatch().unwrap();
        assert_eq!(stream.ingest(&state, &begin), Ok(false));
        for offset in (0..1152).step_by(72) {
            let mut p = packet(CHUNK);
            p[6] = offset;
            p[7] = offset;
            assert_eq!(stream.ingest(&state, &p), Ok(false));
        }
        let saved = stream;
        let mut last = packet(CHUNK);
        last[6] = 1152;
        last[9] = 1;
        assert_eq!(stream.ingest(&state, &last), Err(wire::BAD_REPLY));
        assert_eq!(stream.identity, saved.identity);
        assert_eq!(stream.received, saved.received);
        assert_eq!(stream.bytes, saved.bytes);
        last[9] = 0;
        assert_eq!(stream.ingest(&state, &last), Ok(false));
        assert_eq!(stream.ingest(&state, &packet(COMMIT)), Ok(true));
    }
    #[test]
    fn altered_allocations_reject() {
        let mut e = Export {
            body_len: 308,
            source_len: 17,
            spill_bytes: 768,
            source: [0; 24],
            body: [0; 512],
            sites: unsafe { core::ptr::read_unaligned(SITES.as_ptr().cast::<[Site; 2]>()) },
        };
        e.source[..17].copy_from_slice(&ALIAS);
        e.body[..308].copy_from_slice(BODY);
        assert_eq!(e.validate(&ALIAS), Ok(()));
        for (role, loc) in [(0, 3 | (8 << 8)), (0, 3 | (384 << 8)), (1, 1)] {
            let mut bad = e;
            bad.sites[0].roots[role] = loc;
            assert_eq!(bad.validate(&ALIAS), Err(wire::BAD_REPLY));
        }
    }
}
