// SPDX-License-Identifier: BSD-2-Clause
//
// Copyright (c) 2015 Tycho Nightingale <tycho.nightingale@pluribusnetworks.com>
// Copyright (c) 2015 Nahanni Systems Inc.
// All rights reserved.
//
// Redistribution and use in source and binary forms, with or without
// modification, are permitted provided that the following conditions
// are met:
// 1. Redistributions of source code must retain the above copyright
//    notice, this list of conditions and the following disclaimer.
// 2. Redistributions in binary form must reproduce the above copyright
//    notice, this list of conditions and the following disclaimer in the
//    documentation and/or other materials provided with the distribution.
//
// THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND
// ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
// IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
// ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE
// FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
// DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
// OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
// HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
// LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
// OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
// SUCH DAMAGE.
//
// Adapted from FreeBSD bhyve usr.sbin/bhyve/amd64/ps2mouse.c at 4d65a7c6951cea0333f1a0c1b32c38489cdfa6c5 (the notice
// above; also in third_party/freebsd-bhyve/LICENSE), translated by hand from C into plain Rust: the FIFO is a VecDeque of
// the same size, the softc's mutex is the controller's (`super`), and snapshots, the console pointer registration and the
// debug prints are removed. Host movement arrives as deltas through `movement` (`super::Ps2Input::mouse`), not
// absolute VNC coordinates. Changed, after the PS/2 mouse's command set (IBM's PS/2 mouse technical reference; QEMU's
// `pc` as the stand-in for a real PC): reset and set-defaults leave data reporting disabled (upstream enables it) with
// resolution code 2 (4 counts/mm; upstream stores 4); set-scaling sets or clears the status byte's 2:1 bit (upstream only
// acknowledges); the status byte carries the buttons in its own order (left bit 2, middle 1, right 0); get-ID answers 03
// after the IntelliMouse sample-rate sequence 200, 100, 80, whose movement packets then carry a fourth (wheel) byte;
// wrap mode (EE, left with EC or reset); resend (FE); enable and disable reset the movement counters (upstream keeps
// them); deltas beyond the 9-bit range send its extremes, -256 and 255, with the overflow bit (upstream: 255 either way);
// an unknown command is answered FE (upstream acknowledges it).

//! A PS/2 mouse: reset (FA AA 00), set defaults, enable and disable data reporting, sample rate and resolution (each
//! with its argument byte), scaling 1:1 and 2:1, status request (three bytes), stream and remote mode, read data, get
//! ID (00, or 03 once the IntelliMouse sequence was set), wrap mode and resend. Movement packets are three bytes (four
//! with ID 03): buttons, the 9-bit X and Y deltas' signs and overflow bits, then the low bytes. 2:1 scaling is reported
//! but not applied to the counts (upstream's).
use std::collections::VecDeque;

/* mouse device commands */
const PS2MC_RESET_DEV: u8 = 0xff;
const PS2MC_RESEND: u8 = 0xfe;
const PS2MC_SET_DEFAULTS: u8 = 0xf6;
const PS2MC_DISABLE: u8 = 0xf5;
const PS2MC_ENABLE: u8 = 0xf4;
const PS2MC_SET_SAMPLING_RATE: u8 = 0xf3;
const PS2MC_SEND_DEV_ID: u8 = 0xf2;
const PS2MC_SET_REMOTE_MODE: u8 = 0xf0;
const PS2MC_SET_WRAP_MODE: u8 = 0xee;
const PS2MC_RESET_WRAP_MODE: u8 = 0xec;
const PS2MC_SEND_DEV_DATA: u8 = 0xeb;
const PS2MC_SET_STREAM_MODE: u8 = 0xea;
const PS2MC_SEND_DEV_STATUS: u8 = 0xe9;
const PS2MC_SET_RESOLUTION: u8 = 0xe8;
const PS2MC_SET_SCALING2: u8 = 0xe7;
const PS2MC_SET_SCALING1: u8 = 0xe6;

const PS2MC_BAT_SUCCESS: u8 = 0xaa;
const PS2MC_ACK: u8 = 0xfa;

/* mouse device id */
const PS2MOUSE_DEV_ID: u8 = 0x0;
/// The IntelliMouse's ID, and the sample rates that select it, in order.
const INTELLIMOUSE_ID: u8 = 0x3;
const INTELLIMOUSE_RATES: [u8; 3] = [200, 100, 80];

/* mouse data bits */
const PS2M_DATA_Y_OFLOW: u8 = 0x80;
const PS2M_DATA_X_OFLOW: u8 = 0x40;
const PS2M_DATA_Y_SIGN: u8 = 0x20;
const PS2M_DATA_X_SIGN: u8 = 0x10;
const PS2M_DATA_AONE: u8 = 0x08;
const PS2M_DATA_BUTTONS: u8 = 0x07;

/* mouse status bits */
const PS2M_STS_REMOTE_MODE: u8 = 0x40;
const PS2M_STS_ENABLE_DEV: u8 = 0x20;
const PS2M_STS_SCALING_21: u8 = 0x10;

/// Movement packets' buttons (the packet's order): left, right, middle.
pub const LEFT: u8 = 1;
pub const RIGHT: u8 = 2;
pub const MIDDLE: u8 = 4;

const PS2MOUSE_FIFOSZ: usize = 16;

pub(super) struct Ps2Mouse {
    /// Mode bits (remote, enabled, 2:1 scaling); the buttons are kept apart in `buttons`.
    status: u8,
    resolution: u8,
    sampling_rate: u8,
    fifo: VecDeque<u8>,
    /// Current command for the next byte.
    curcmd: u8,
    delta_x: i32,
    delta_y: i32,
    /// The buttons held, in the packet's order.
    buttons: u8,
    wrap: bool,
    id: u8,
    /// The last three sample rates set, oldest first.
    rates: [u8; 3],
    /// The last byte sent (resend).
    last: u8,
}

impl Ps2Mouse {
    pub(super) fn new() -> Self {
        let mut sc = Self {
            status: 0,
            resolution: 0,
            sampling_rate: 0,
            fifo: VecDeque::with_capacity(PS2MOUSE_FIFOSZ),
            curcmd: 0,
            delta_x: 0,
            delta_y: 0,
            buttons: 0,
            wrap: false,
            id: PS2MOUSE_DEV_ID,
            rates: [0; 3],
            last: 0,
        };
        sc.reset();
        sc
    }

    fn fifo_put(&mut self, val: u8) {
        if self.fifo.len() < PS2MOUSE_FIFOSZ {
            self.fifo.push_back(val);
        }
    }

    fn movement_reset(&mut self) {
        self.delta_x = 0;
        self.delta_y = 0;
    }

    /// One movement packet into the FIFO (if it fits), the deltas then cleared.
    fn movement_get(&mut self) {
        let mut val0 = PS2M_DATA_AONE | self.buttons & PS2M_DATA_BUTTONS;
        let mut axis = |delta: i32, sign: u8, oflow: u8| -> u8 {
            if delta < 0 {
                val0 |= sign;
            }
            if !(-256..=255).contains(&delta) {
                // The 9-bit extremes, -256 and 255 (upstream sends 255 either way).
                val0 |= oflow;
                if delta < 0 { 0 } else { 255 }
            } else {
                delta as u8
            }
        };
        let val1 = axis(self.delta_x, PS2M_DATA_X_SIGN, PS2M_DATA_X_OFLOW);
        let val2 = axis(self.delta_y, PS2M_DATA_Y_SIGN, PS2M_DATA_Y_OFLOW);
        self.movement_reset();
        let packet = [val0, val1, val2, 0];
        let n = if self.id == INTELLIMOUSE_ID { 4 } else { 3 };
        if self.fifo.len() + n <= PS2MOUSE_FIFOSZ {
            packet[..n].iter().for_each(|&b| self.fifo_put(b));
        }
    }

    /// Set Defaults: what a reset leaves, without the self-test reply, the ID or the wrap mode.
    fn defaults(&mut self) {
        self.fifo.clear();
        self.movement_reset();
        self.status = 0;
        self.resolution = 2;
        self.sampling_rate = 100;
    }

    fn reset(&mut self) {
        self.defaults();
        self.curcmd = 0;
        self.wrap = false;
        self.id = PS2MOUSE_DEV_ID;
        self.rates = [0; 3];
    }

    pub(super) fn output(&mut self) -> Option<u8> {
        let value = self.fifo.pop_front()?;
        self.last = value;
        Some(value)
    }

    /// The status request's first byte: the mode bits, then the buttons as left (bit 2), middle (1), right (0).
    fn status_byte(&self) -> u8 {
        let b = |mask: u8, bit: u8| if self.buttons & mask != 0 { bit } else { 0 };
        self.status | b(LEFT, 4) | b(MIDDLE, 2) | b(RIGHT, 1)
    }

    /// A byte from the controller (the guest's D4 command, then 60H).
    pub(super) fn input(&mut self, val: u8) {
        self.fifo.clear();
        if self.curcmd != 0 {
            match self.curcmd {
                PS2MC_SET_SAMPLING_RATE => {
                    self.sampling_rate = val;
                    self.rates = [self.rates[1], self.rates[2], val];
                    if self.rates == INTELLIMOUSE_RATES {
                        self.id = INTELLIMOUSE_ID;
                    }
                }
                _ => self.resolution = val, // PS2MC_SET_RESOLUTION
            }
            self.fifo_put(PS2MC_ACK);
            self.curcmd = 0;
        } else if self.wrap && val != PS2MC_RESET_DEV && val != PS2MC_RESET_WRAP_MODE {
            self.fifo_put(val);
        } else {
            match val {
                PS2MC_RESET_DEV => {
                    self.reset();
                    self.fifo_put(PS2MC_ACK);
                    self.fifo_put(PS2MC_BAT_SUCCESS);
                    self.fifo_put(PS2MOUSE_DEV_ID);
                }
                PS2MC_RESEND => {
                    self.fifo_put(self.last);
                }
                PS2MC_SET_DEFAULTS => {
                    self.defaults();
                    self.fifo_put(PS2MC_ACK);
                }
                PS2MC_DISABLE => {
                    self.status &= !PS2M_STS_ENABLE_DEV;
                    self.movement_reset();
                    self.fifo_put(PS2MC_ACK);
                }
                PS2MC_ENABLE => {
                    self.status |= PS2M_STS_ENABLE_DEV;
                    self.movement_reset();
                    self.fifo_put(PS2MC_ACK);
                }
                PS2MC_SET_SAMPLING_RATE | PS2MC_SET_RESOLUTION => {
                    self.curcmd = val;
                    self.fifo_put(PS2MC_ACK);
                }
                PS2MC_SEND_DEV_ID => {
                    self.fifo_put(PS2MC_ACK);
                    self.fifo_put(self.id);
                }
                PS2MC_SET_REMOTE_MODE => {
                    self.status |= PS2M_STS_REMOTE_MODE;
                    self.fifo_put(PS2MC_ACK);
                }
                PS2MC_SET_WRAP_MODE => {
                    self.wrap = true;
                    self.fifo_put(PS2MC_ACK);
                }
                PS2MC_RESET_WRAP_MODE => {
                    self.wrap = false;
                    self.fifo_put(PS2MC_ACK);
                }
                PS2MC_SEND_DEV_DATA => {
                    self.fifo_put(PS2MC_ACK);
                    self.movement_get();
                }
                PS2MC_SET_STREAM_MODE => {
                    self.status &= !PS2M_STS_REMOTE_MODE;
                    self.fifo_put(PS2MC_ACK);
                }
                PS2MC_SEND_DEV_STATUS => {
                    self.fifo_put(PS2MC_ACK);
                    self.fifo_put(self.status_byte());
                    self.fifo_put(self.resolution);
                    self.fifo_put(self.sampling_rate);
                }
                PS2MC_SET_SCALING2 => {
                    self.status |= PS2M_STS_SCALING_21;
                    self.fifo_put(PS2MC_ACK);
                }
                PS2MC_SET_SCALING1 => {
                    self.status &= !PS2M_STS_SCALING_21;
                    self.fifo_put(PS2MC_ACK);
                }
                _ => self.fifo_put(PS2MC_RESEND),
            }
        }
    }

    /// Host movement (`dx` right, `dy` up, as the PS/2 mouse counts them) with the buttons now held (`LEFT`, `RIGHT`,
    /// `MIDDLE`): the deltas accumulate, and in stream mode with reporting enabled (and not in wrap mode) a packet is
    /// queued (upstream's `ps2mouse_event`). In remote mode the guest reads them with read-data.
    pub(super) fn movement(&mut self, dx: i16, dy: i16, buttons: u8) {
        self.delta_x += i32::from(dx);
        self.delta_y += i32::from(dy);
        self.buttons = buttons & PS2M_DATA_BUTTONS;
        if self.status & (PS2M_STS_ENABLE_DEV | PS2M_STS_REMOTE_MODE) == PS2M_STS_ENABLE_DEV && !self.wrap {
            self.movement_get();
        }
    }
}
