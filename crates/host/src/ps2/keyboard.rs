// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.
//
// SPDX-License-Identifier: MIT
//
// Adapted from OpenVMM vm/devices/chipset/src/i8042/ps2keyboard.rs at 5d6ab1cfa66c0c9435b374e194aa6ad6c5f9db42 (licence
// text in third_party/openvmm/LICENSE), expanded by hand into plain Rust: the `open_enum` Ps2KeyboardCommand is a set of
// u8 constants, and `inspect`, save/restore, `tracing` and the async `InputSource` (`input_core`, `futures`) are removed.
// Host input arrives as set 2 bytes through `key` (`super::Ps2Input::keyboard`) instead of the poll of an input source.
// Changed: the keyboard is a set 2 keyboard (the controller translates to set 1 under XLAT, `super`), so its ID is AB 83
// (upstream's keyboard sends set 1 itself and answers the translated AB 41); F0 00 answers the current set (2); a
// scanning flag: F5 stops scanning (host keys are dropped), F4 and FF start it (upstream ignores scanning); F6 keeps it.

//! A PS/2 keyboard (IBM's keyboard command set): reset (FA AA), identify (FA AB 83), LEDs, typematic rate, scan code
//! set (2 only), enable, disable, defaults, echo and resend, every command and argument byte acknowledged with FA, an
//! unknown command answered FE (resend). Its output queue holds at most [`KEYBOARD_BUFFER_SIZE`] bytes; a host key that
//! does not fit marks the overrun with 00 (set 2's overrun code, FF once translated) in the last byte, as upstream does
//! for its own replies.
use std::collections::VecDeque;

/// PS/2 keyboard definitions.
mod spec {
    pub const TURN_ON_OFF_LE_DS: u8 = 0xED;
    pub const ECHO: u8 = 0xEE;
    pub const SET_SCAN_CODE_SET: u8 = 0xF0;
    pub const IDENTIFY_KEYBOARD: u8 = 0xF2;
    pub const STANDARD_ENABLE: u8 = 0xF6;
    pub const RESEND: u8 = 0xFE;
    pub const ENABLE: u8 = 0xF4;
    pub const STANDARD_DISABLE: u8 = 0xF5;
    pub const SET_REPETITION_RATE: u8 = 0xF3;
    pub const SET3_COMMAND_F7: u8 = 0xF7;
    pub const SET3_COMMAND_F8: u8 = 0xF8;
    pub const SET3_COMMAND_F9: u8 = 0xF9;
    pub const SET3_COMMAND_FA: u8 = 0xFA;
    pub const SET3_COMMAND_FB: u8 = 0xFB;
    pub const SET3_COMMAND_FC: u8 = 0xFC;
    pub const SET3_COMMAND_FD: u8 = 0xFD;
    pub const RESET: u8 = 0xFF;

    pub const ACKNOWLEDGE_COMMAND: u8 = 0xFA;
}
use spec::*;

/// The keyboard's ID (IBM MF2, set 2): what IDENTIFY answers after its acknowledgement.
pub(super) const ID: [u8; 2] = [0xAB, 0x83];
/// The only scan code set.
const SCAN_CODE_SET: u8 = 2;
/// Set 2's overrun code.
const OVERRUN: u8 = 0;

struct KeyboardState {
    previous_command: Option<u8>,
    last_output_byte_read: u8,
    /// Kept as upstream does; nothing shows the LEDs yet.
    #[allow(dead_code)]
    led_state: u8,
    output_buffer: VecDeque<u8>,
}

impl KeyboardState {
    fn new() -> Self {
        Self {
            previous_command: None,
            last_output_byte_read: 0,
            led_state: 0,
            output_buffer: VecDeque::new(),
        }
    }
}

pub(super) struct Ps2Keyboard {
    state: KeyboardState,
    /// Scanning: host keys are queued (natemu's; see the header).
    scanning: bool,
}

pub(super) const KEYBOARD_BUFFER_SIZE: usize = 21;

impl Ps2Keyboard {
    pub(super) fn new() -> Self {
        Self {
            state: KeyboardState::new(),
            scanning: true,
        }
    }

    /// One byte of a host key's set 2 sequence (upstream's `poll`, which keeps two places free for replies).
    pub(super) fn key(&mut self, code: u8) {
        if !self.scanning {
            return;
        }
        if self.state.output_buffer.len() < KEYBOARD_BUFFER_SIZE - 2 {
            self.state.output_buffer.push_back(code);
        } else if let Some(last) = self.state.output_buffer.back_mut() {
            *last = OVERRUN;
        }
    }

    pub(super) fn output(&mut self) -> Option<u8> {
        let value = self.state.output_buffer.pop_front()?;
        self.state.last_output_byte_read = value;
        Some(value)
    }

    fn push(&mut self, value: u8) {
        if self.state.output_buffer.len() <= KEYBOARD_BUFFER_SIZE {
            self.state.output_buffer.push_back(value);
        } else {
            // Indicate buffer overflow.
            *self.state.output_buffer.back_mut().unwrap() = OVERRUN;
        }
    }

    pub(super) fn input(&mut self, input: u8) {
        let (command, data) = if let Some(command) = self.state.previous_command.take() {
            (command, Some(input))
        } else {
            (input, None)
        };
        if self.command(command, data).is_none() {
            self.state.previous_command = Some(command);
        }
    }

    /// Returns `None` if the command needs to wait for its argument byte.
    fn command(&mut self, command: u8, data: Option<u8>) -> Option<()> {
        match command {
            TURN_ON_OFF_LE_DS => {
                self.push(ACKNOWLEDGE_COMMAND);
                let data = data?;
                if data == TURN_ON_OFF_LE_DS {
                    // Sometimes the BIOS doesn't receive the above ACK in
                    // time. I have tried lots of kludges to get around this
                    // problem, but the best I can come up with at this time
                    // is to detect when it resends the LED command the
                    // second time and just swallow the first one.
                    return None;
                } else {
                    self.state.led_state = data;
                }
            }
            ECHO => {
                self.push(command);
            }
            SET_SCAN_CODE_SET => {
                self.push(ACKNOWLEDGE_COMMAND);
                // Any set is acknowledged and set 2 kept (upstream's); 0 asks which set is in use.
                if data? == 0 {
                    self.push(SCAN_CODE_SET);
                }
            }
            IDENTIFY_KEYBOARD => {
                self.push(ACKNOWLEDGE_COMMAND);
                self.push(ID[0]);
                self.push(ID[1]);
            }
            STANDARD_ENABLE => {
                self.state = KeyboardState::new();
                self.push(ACKNOWLEDGE_COMMAND);
            }
            ENABLE => {
                self.state.output_buffer.clear();
                self.scanning = true;
                self.push(ACKNOWLEDGE_COMMAND);
            }
            STANDARD_DISABLE => {
                self.state = KeyboardState::new();
                self.scanning = false;
                self.push(ACKNOWLEDGE_COMMAND);
            }
            SET_REPETITION_RATE => {
                // We will use the Host to do auto-repeat keys, so we won't
                // honor the change to the X86 keyboard. Just acknowledge that
                // we received the data byte (without actually doing anything
                // with it).
                self.push(ACKNOWLEDGE_COMMAND);
                let _ = data?;
            }
            RESEND => {
                self.push(self.state.last_output_byte_read);
            }
            RESET => {
                self.state = KeyboardState::new();
                self.scanning = true;
                self.push(ACKNOWLEDGE_COMMAND);
                self.push(0xaa);
            }
            SET3_COMMAND_F7 | SET3_COMMAND_F8 | SET3_COMMAND_F9 | SET3_COMMAND_FA => {
                self.push(ACKNOWLEDGE_COMMAND);
            }
            SET3_COMMAND_FB | SET3_COMMAND_FC | SET3_COMMAND_FD => {
                // Acknowledge parameter byte.
                // Since set3 is not supported, no extra action required.
                self.push(ACKNOWLEDGE_COMMAND);
                let _ = data?;
            }
            _ => {
                // Invalid keyboard command.
                self.push(0xfe);
            }
        }
        Some(())
    }
}
