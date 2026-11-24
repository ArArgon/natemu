// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.
//
// The set 2 to set 1 translation table (`TRANSLATION`) and its use of the F0 break prefix:
//
// Copyright (c) 2014 Tycho Nightingale <tycho.nightingale@pluribusnetworks.com>
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
// SPDX-License-Identifier: MIT AND BSD-2-Clause
//
// Adapted from OpenVMM vm/devices/chipset/src/i8042/mod.rs and spec.rs at 5d6ab1cfa66c0c9435b374e194aa6ad6c5f9db42
// (MIT; licence text in third_party/openvmm/LICENSE), expanded by hand into plain Rust: the `bitfield_struct` registers
// (OutputPort, KeyboardStatus, CommandFlag) and the `open_enum` ControllerCommand are u8 constants in `spec`;
// `chipset_device`'s port interface is natemu's `Device` (one byte per access: each port is its own one-port range, so
// the registry splits wider accesses); `LineInterrupt` is `Irqs::line`; the reset callback stores `pm::RESET` in the
// registry's power request. Removed: `inspect`, save/restore, `ChangeDeviceState`, the resolver, `tracing`, the poll
// device and its waker, and the async keyboard `InputSource`: host input comes through [`Ps2Input`], from the calling
// thread. The mouse is bhyve's (`ps2/mouse.rs`; upstream's is a stub that only acknowledges). The XLAT table is FreeBSD
// bhyve's usr.sbin/bhyve/amd64/atkbdc.c at 4d65a7c6951cea0333f1a0c1b32c38489cdfa6c5 (`atkbdc_kbd_read`; BSD-2-Clause,
// the notice above, also in third_party/freebsd-bhyve/LICENSE). Changed from upstream: the keyboard sends set 2 and the
// controller translates it to set 1 while the command byte's XLAT bit is set (upstream's keyboard sends set 1 and its
// controller never translates); the command byte's keyboard-disable bit holds keyboard output back as its mouse-disable
// bit holds the mouse's (upstream checks only the mouse's); IRQ 1 is also raised for the controller's own replies, as a
// PC's 8042 and QEMU's `pc` (the behaviour reference) do (upstream raises it for keyboard bytes only); a command byte
// write re-evaluates both interrupt lines at once.

//! The 8042 keyboard controller with a PS/2 keyboard and mouse (platform-devices plan, "PS/2"): data at port 60H, status
//! (reads) and commands (writes) at 64H, IRQ 1 for keyboard bytes and the controller's replies, IRQ 12 for mouse bytes,
//! both ISA edge (a 0 -> 1 change of the level the controller drives: high while the output buffer holds a byte of that
//! source and the command byte enables its interrupt). Described in the DSDT as `\_SB.KBD_` (PNP0303) and `\_SB.MOU_`
//! (PNP0F13) and by the FADT's IAPC_BOOT_ARCH 8042 bit ([`crate::boot::acpi::Board::ps2`]); installed only with
//! `Boot::ps2` (the `natemu` CLI's `machine.ps2`), and port 64H is then the 8042's, not `pm`'s reset-only register
//! (`clock::install`).
//!
//! **Controller.** One output buffer: a byte from the controller (a command's reply), the keyboard or the mouse; while
//! it is empty the mouse's queue, then the keyboard's, refill it (the mouse first, as upstream; a port whose command-byte
//! disable bit is set is skipped). A read of 60H empties it and refills it from the queues. Commands (64H): read and write
//! the command byte (20H, 60H; it starts as 47H: both interrupts, the system flag, XLAT), the controller self-test (AAH:
//! 55H), the interface tests (ABH, A9H: 00H), the keyboard and mouse interface disables and enables (ADH, AEH, A7H,
//! A8H), the input port (C0H), the output port (D0H, D1H; D1H with the reset bit 0 clear resets), writes to the output
//! buffer as if from the keyboard or mouse (D2H, D3H), the next 60H byte to the mouse (D4H), the controller's RAM (21H-3FH,
//! 61H-7FH) and the pulse commands F0H-FFH (an even one pulses the reset line: FEH is the PC's reset). A reset ends the
//! run with `Stop::Power(pm::RESET)`, as `pm`'s port 64H does without the 8042. A byte the guest writes to 60H goes to
//! the keyboard (or, after D4H, the mouse, or to a command waiting for its argument) and enables that interface.
//!
//! **Output buffer full.** A device's replies wait in its queue behind the unread byte; a controller command's reply
//! goes into the output buffer over it, and the unread byte is lost (upstream's `write_output_byte`). No access is
//! refused and no command stops the vCPU.
//!
//! **Host input** ([`Ps2Input`], cloneable and `Send`): `keyboard` queues set 2 bytes, `mouse` movement with the buttons
//! held; each takes the controller's lock, queues, refills the output buffer if it is empty and drives IRQ 1 or 12 from
//! the calling thread, as the UART's receive side does (`uart::feed`), so a key reaches a vCPU halted in HLT without
//! any guest access. Nothing produces input yet (the display front end will). A producer holds an `Irqs::busy` guard
//! while input can still arrive, as the UART feeder does, or a run whose vCPUs are all halted ends before the key.
use crate::devices::{At, Device, Devices};
use crate::irq::Irqs;
use crate::pm;
use std::sync::atomic::{AtomicU8, Ordering::SeqCst};
use std::sync::{Arc, Mutex};

mod keyboard;
mod mouse;
use keyboard::Ps2Keyboard;
use mouse::Ps2Mouse;
pub use mouse::{LEFT, MIDDLE, RIGHT};

/// The data port (output and input buffers).
pub const DATA_PORT: u16 = 0x60;
/// The status register (reads) and the command port (writes).
pub const COMMAND_PORT: u16 = 0x64;
/// ISA IRQs: the keyboard's, the mouse's.
pub const KEYBOARD_IRQ: usize = 1;
pub const MOUSE_IRQ: usize = 12;
/// 64H's offset in the device (60H's is 0).
const COMMAND_OFFSET: u64 = 4;

/// Intel 8042 definitions (upstream's `spec.rs`).
mod spec {
    // OutputPort.
    pub const OUTPUT_RESET: u8 = 1 << 0;
    pub const OUTPUT_A20_GATE: u8 = 1 << 1;
    pub const OUTPUT_AUX_CLOCK: u8 = 1 << 2;
    pub const OUTPUT_KEYBOARD_OUTPUT_BUFFERED: u8 = 1 << 4;
    pub const OUTPUT_MOUSE_OUTPUT_BUFFERED: u8 = 1 << 5;
    pub const OUTPUT_CLOCK: u8 = 1 << 6;

    // ControllerCommand.
    pub const READ_COMMAND_BYTE: u8 = 0x20;
    pub const WRITE_COMMAND_BYTE: u8 = 0x60;
    pub const UNKNOWN_A1: u8 = 0xA1; // Used in AMI BIOS boot
    pub const PWD_CHECK: u8 = 0xA4;
    pub const DISABLE_AUX_INTERFACE: u8 = 0xA7;
    pub const ENABLE_AUX_INTERFACE: u8 = 0xA8;
    pub const CHECK_AUX_INTERFACE: u8 = 0xA9;
    pub const SELF_TEST: u8 = 0xAA;
    pub const CHECK_INTERFACE: u8 = 0xAB;
    pub const DISABLE_KEYBOARD: u8 = 0xAD;
    pub const ENABLE_KEYBOARD: u8 = 0xAE;
    pub const READ_INPUT_PORT: u8 = 0xC0;
    pub const READ_OUT_INPUT_PORT_LO: u8 = 0xC1;
    pub const READ_OUT_INPUT_PORT_HI: u8 = 0xC2;
    pub const READ_OUTPUT_PORT: u8 = 0xD0;
    pub const WRITE_OUTPUT_PORT: u8 = 0xD1;
    pub const WRITE_OUTPUT_BUFFER: u8 = 0xD2;
    pub const WRITE_AUX_OUTPUT_BUFFER: u8 = 0xD3;
    pub const WRITE_AUX_DEVICE: u8 = 0xD4;
    pub const PULSE_OUTPUT_F0: u8 = 0xF0;
    pub const PULSE_OUTPUT_FF: u8 = 0xFF;

    // KeyboardStatus.
    pub const STATUS_OUTPUT_BUFFER_FULL: u8 = 1 << 0;
    pub const STATUS_KEYBOARD_SELF_TEST: u8 = 1 << 2;
    pub const STATUS_INPUT_BUFFER_FOR_CONTROLLER: u8 = 1 << 3;
    pub const STATUS_KEYBOARD_UNLOCKED: u8 = 1 << 4;
    pub const STATUS_OUTPUT_BUFFER_FOR_MOUSE: u8 = 1 << 5;

    // CommandFlag.
    pub const ALLOW_KEYBOARD_INTERRUPTS: u8 = 1 << 0;
    pub const ALLOW_MOUSE_INTERRUPTS: u8 = 1 << 1;
    pub const KEYBOARD_SELF_TEST: u8 = 1 << 2;
    pub const DISABLE_KEYBOARD_FLAG: u8 = 1 << 4;
    pub const DISABLE_MOUSE_FLAG: u8 = 1 << 5;
    pub const ENABLE_SCAN_CODE: u8 = 1 << 6;
}
use spec::*;

/// Set 2 to set 1, indexed by the set 2 byte (bhyve's `translation`; a break code's F0 prefix sets bit 7 of the next).
const TRANSLATION: [u8; 256] = [
    0xff, 0x43, 0x41, 0x3f, 0x3d, 0x3b, 0x3c, 0x58, 0x64, 0x44, 0x42, 0x40, 0x3e, 0x0f, 0x29, 0x59, //
    0x65, 0x38, 0x2a, 0x70, 0x1d, 0x10, 0x02, 0x5a, 0x66, 0x71, 0x2c, 0x1f, 0x1e, 0x11, 0x03, 0x5b, //
    0x67, 0x2e, 0x2d, 0x20, 0x12, 0x05, 0x04, 0x5c, 0x68, 0x39, 0x2f, 0x21, 0x14, 0x13, 0x06, 0x5d, //
    0x69, 0x31, 0x30, 0x23, 0x22, 0x15, 0x07, 0x5e, 0x6a, 0x72, 0x32, 0x24, 0x16, 0x08, 0x09, 0x5f, //
    0x6b, 0x33, 0x25, 0x17, 0x18, 0x0b, 0x0a, 0x60, 0x6c, 0x34, 0x35, 0x26, 0x27, 0x19, 0x0c, 0x61, //
    0x6d, 0x73, 0x28, 0x74, 0x1a, 0x0d, 0x62, 0x6e, 0x3a, 0x36, 0x1c, 0x1b, 0x75, 0x2b, 0x63, 0x76, //
    0x55, 0x56, 0x77, 0x78, 0x79, 0x7a, 0x0e, 0x7b, 0x7c, 0x4f, 0x7d, 0x4b, 0x47, 0x7e, 0x7f, 0x6f, //
    0x52, 0x53, 0x50, 0x4c, 0x4d, 0x48, 0x01, 0x45, 0x57, 0x4e, 0x51, 0x4a, 0x37, 0x49, 0x46, 0x54, //
    0x80, 0x81, 0x82, 0x41, 0x54, 0x85, 0x86, 0x87, 0x88, 0x89, 0x8a, 0x8b, 0x8c, 0x8d, 0x8e, 0x8f, //
    0x90, 0x91, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9a, 0x9b, 0x9c, 0x9d, 0x9e, 0x9f, //
    0xa0, 0xa1, 0xa2, 0xa3, 0xa4, 0xa5, 0xa6, 0xa7, 0xa8, 0xa9, 0xaa, 0xab, 0xac, 0xad, 0xae, 0xaf, //
    0xb0, 0xb1, 0xb2, 0xb3, 0xb4, 0xb5, 0xb6, 0xb7, 0xb8, 0xb9, 0xba, 0xbb, 0xbc, 0xbd, 0xbe, 0xbf, //
    0xc0, 0xc1, 0xc2, 0xc3, 0xc4, 0xc5, 0xc6, 0xc7, 0xc8, 0xc9, 0xca, 0xcb, 0xcc, 0xcd, 0xce, 0xcf, //
    0xd0, 0xd1, 0xd2, 0xd3, 0xd4, 0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda, 0xdb, 0xdc, 0xdd, 0xde, 0xdf, //
    0xe0, 0xe1, 0xe2, 0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xe9, 0xea, 0xeb, 0xec, 0xed, 0xee, 0xef, //
    0xf0, 0xf1, 0xf2, 0xf3, 0xf4, 0xf5, 0xf6, 0xf7, 0xf8, 0xf9, 0xfa, 0xfb, 0xfc, 0xfd, 0xfe, 0xff, //
];
/// Set 2's break prefix.
const BREAK: u8 = 0xf0;

/// An Intel 8042 keyboard/mouse controller.
pub(crate) struct I8042 {
    // Runtime glue
    power: Arc<AtomicU8>,
    irqs: Irqs,

    // Sub-emulators
    keyboard: Ps2Keyboard,
    mouse: Ps2Mouse,

    // Volatile state
    state: I8042State,
}

struct I8042State {
    command_flag: u8,
    data_port_target: DataPortTarget,
    output_buffer: u8,
    output_buffer_state: OutputBufferState,
    a20_gate: bool,
    memory: [u8; 32],
    /// XLAT: a break prefix taken from the keyboard, for the next byte (natemu's: it may arrive after a refill).
    release: bool,
}

#[derive(Copy, Clone, PartialEq, Eq)]
enum OutputBufferState {
    Empty,
    Controller,
    Keyboard,
    Mouse,
}

#[derive(Copy, Clone)]
enum DataPortTarget {
    Keyboard,
    Mouse,
    Controller(u8),
}

impl I8042State {
    fn new() -> Self {
        Self {
            command_flag: ALLOW_KEYBOARD_INTERRUPTS | ALLOW_MOUSE_INTERRUPTS | KEYBOARD_SELF_TEST | ENABLE_SCAN_CODE,
            memory: [0; 32],
            data_port_target: DataPortTarget::Keyboard,
            output_buffer: 0,
            output_buffer_state: OutputBufferState::Empty,
            a20_gate: true,
            release: false,
        }
    }
}

impl I8042 {
    /// A controller with an attached PS/2 keyboard and mouse; a guest reset request stores `pm::RESET` in `power`.
    fn new(power: Arc<AtomicU8>, irqs: Irqs) -> Self {
        I8042 {
            power,
            irqs,
            state: I8042State::new(),
            keyboard: Ps2Keyboard::new(),
            mouse: Ps2Mouse::new(),
        }
    }

    fn trigger_reset(&self) {
        self.power.store(pm::RESET, SeqCst);
    }
}

impl Device for I8042 {
    fn read(&mut self, _vcpu: usize, offset: u64, size: u64) -> Option<u64> {
        if size != 1 {
            return None;
        }
        let data = match offset {
            // Read a piece of data from the output buffer
            0 => self.read_output_byte(),
            // Read the current keyboard status.
            COMMAND_OFFSET => {
                let mut status = STATUS_KEYBOARD_SELF_TEST | STATUS_KEYBOARD_UNLOCKED;
                if self.state.output_buffer_state != OutputBufferState::Empty {
                    status |= STATUS_OUTPUT_BUFFER_FULL;
                }
                if matches!(self.state.data_port_target, DataPortTarget::Controller(_)) {
                    status |= STATUS_INPUT_BUFFER_FOR_CONTROLLER;
                }
                if self.state.output_buffer_state == OutputBufferState::Mouse {
                    status |= STATUS_OUTPUT_BUFFER_FOR_MOUSE;
                }
                status
            }
            _ => return None,
        };
        // Populate the next output byte if appropriate.
        self.check_devices_for_output();
        Some(data.into())
    }

    fn write(&mut self, _vcpu: usize, offset: u64, size: u64, value: u64) -> bool {
        if size != 1 {
            return false;
        }
        let data = value as u8;
        match offset {
            0 => {
                match std::mem::replace(&mut self.state.data_port_target, DataPortTarget::Keyboard) {
                    DataPortTarget::Keyboard => {
                        self.state.command_flag &= !DISABLE_KEYBOARD_FLAG;
                        self.keyboard.input(data);
                    }
                    DataPortTarget::Mouse => {
                        self.state.command_flag &= !DISABLE_MOUSE_FLAG;
                        self.mouse.input(data);
                    }
                    DataPortTarget::Controller(command) => {
                        self.handle_command(command, Some(data));
                    }
                }
            }
            COMMAND_OFFSET => {
                // Next data port write defaults to the keyboard.
                self.state.data_port_target = DataPortTarget::Keyboard;
                if self.handle_command(data, None).is_none() {
                    self.state.data_port_target = DataPortTarget::Controller(data);
                }
            }
            _ => return false,
        }
        // Check to see if the keyboard or mouse
        // have more input for the controller.
        self.check_devices_for_output();
        true
    }
}

impl I8042 {
    /// The interrupt lines' levels: IRQ 1 while the output buffer holds a keyboard or controller byte, IRQ 12 while it
    /// holds a mouse byte, each if the command byte enables it (`Irqs::line` passes only changes on).
    fn sync_interrupts(&mut self) {
        let state = self.state.output_buffer_state;
        self.irqs.line(
            KEYBOARD_IRQ,
            self.state.command_flag & ALLOW_KEYBOARD_INTERRUPTS != 0
                && matches!(state, OutputBufferState::Keyboard | OutputBufferState::Controller),
        );
        self.irqs.line(
            MOUSE_IRQ,
            self.state.command_flag & ALLOW_MOUSE_INTERRUPTS != 0 && state == OutputBufferState::Mouse,
        );
    }

    fn write_output_byte(&mut self, state: OutputBufferState, data: u8) {
        self.state.output_buffer = data;
        self.state.output_buffer_state = state;
        self.sync_interrupts();
    }

    fn read_output_byte(&mut self) -> u8 {
        self.state.output_buffer_state = OutputBufferState::Empty;
        self.sync_interrupts();
        self.state.output_buffer
    }

    /// Returns `None` if the command needs to wait for more data.
    fn handle_command(&mut self, command: u8, command_data: Option<u8>) -> Option<()> {
        match command {
            READ_COMMAND_BYTE => {
                self.write_output_byte(OutputBufferState::Controller, self.state.command_flag);
            }
            WRITE_COMMAND_BYTE => {
                self.state.command_flag = command_data?;
            }
            DISABLE_AUX_INTERFACE => {
                self.state.command_flag |= DISABLE_MOUSE_FLAG;
            }
            ENABLE_AUX_INTERFACE => {
                self.state.command_flag &= !DISABLE_MOUSE_FLAG;
            }
            CHECK_AUX_INTERFACE => {
                self.write_output_byte(OutputBufferState::Controller, 0);
            }
            SELF_TEST => {
                self.write_output_byte(OutputBufferState::Controller, 0x55);
            }
            CHECK_INTERFACE => {
                self.write_output_byte(OutputBufferState::Controller, 0);
            }
            DISABLE_KEYBOARD => {
                self.state.command_flag |= DISABLE_KEYBOARD_FLAG;
            }
            ENABLE_KEYBOARD => {
                self.state.command_flag &= !DISABLE_KEYBOARD_FLAG;
            }
            READ_INPUT_PORT => {
                // Specify that the keyboard is not locked
                self.write_output_byte(OutputBufferState::Controller, 0x80);
            }
            READ_OUT_INPUT_PORT_LO | READ_OUT_INPUT_PORT_HI => {
                //
                // Over multiple releases of Hyper-V these have never
                // been implemented and are clearly not needed.
                // Silently ignore these two commands.
                //
            }
            READ_OUTPUT_PORT => {
                let mut output_port = OUTPUT_RESET | OUTPUT_AUX_CLOCK | OUTPUT_CLOCK;
                if self.state.a20_gate {
                    output_port |= OUTPUT_A20_GATE;
                }
                if self.state.output_buffer_state == OutputBufferState::Keyboard {
                    output_port |= OUTPUT_KEYBOARD_OUTPUT_BUFFERED;
                }
                if self.state.output_buffer_state == OutputBufferState::Mouse {
                    output_port |= OUTPUT_MOUSE_OUTPUT_BUFFERED;
                }
                self.write_output_byte(OutputBufferState::Controller, output_port);
            }
            WRITE_OUTPUT_PORT => {
                let output_port = command_data?;
                // The A20 gate is kept for D0H only: this machine has no A20 masking.
                self.state.a20_gate = output_port & OUTPUT_A20_GATE != 0;
                if output_port & OUTPUT_RESET == 0 {
                    // Initiated reset via WRITE_OUTPUT_PORT command.
                    self.trigger_reset();
                }
            }
            WRITE_OUTPUT_BUFFER => {
                // Write the data to the output buffer, making it look as
                // though the keyboard put it there. This means that there
                // will be an interrupt (IRQ1) requested.
                self.write_output_byte(OutputBufferState::Keyboard, command_data?);
            }
            WRITE_AUX_OUTPUT_BUFFER => {
                // Write the data to the output buffer, making it look as
                // though the mouse put it there.
                self.write_output_byte(OutputBufferState::Mouse, command_data?);
            }
            WRITE_AUX_DEVICE => {
                // The next byte written to port 60 will go to the mouse instead of the keyboard
                self.state.data_port_target = DataPortTarget::Mouse;
            }
            PULSE_OUTPUT_F0..=PULSE_OUTPUT_FF => {
                if (command & 1) == 0 {
                    // If we get this command, the program wants to restart the
                    // machine if bit 0 of the command is clear.
                    self.trigger_reset();
                } else {
                    // This command (along with commands 0xF0 through 0xFE) strobes the
                    // four output bits on the keyboard controller. Except for 0xFE, all
                    // of these commands are NOPs. This one in particular is used by the
                    // BIOS and various other applications just to check to make sure the
                    // keyboard controller is still responding.
                }
            }
            0x21..=0x3f => {
                // There are 31 spare bytes of data storage in the keyboard controller
                // that can be addressed and written to by commands 0x61 - 0x7F.
                // When these registers are read using commands 0x21 - 0x3f, they
                // apparently simulate data bytes coming from the keyboard.
                // NetOp (a remote control program) uses this functionality.
                self.write_output_byte(OutputBufferState::Keyboard, self.state.memory[command as usize & 0x1f]);
            }
            0x61..=0x7f => {
                self.state.memory[command as usize & 0x1f] = command_data?;
            }
            UNKNOWN_A1 => {
                self.write_output_byte(OutputBufferState::Controller, 0);
            }
            PWD_CHECK => {
                self.write_output_byte(OutputBufferState::Controller, 0xf1);
            }
            _ => {
                // Unsupported keyboard command.
            }
        }
        Some(())
    }

    /// The keyboard's next byte, through XLAT when the command byte sets it (bhyve's `atkbdc_kbd_read`).
    fn keyboard_output(&mut self) -> Option<u8> {
        if self.state.command_flag & ENABLE_SCAN_CODE == 0 {
            return self.keyboard.output();
        }
        loop {
            let byte = self.keyboard.output()?;
            if byte == BREAK {
                self.state.release = true;
                continue;
            }
            let release = if std::mem::take(&mut self.state.release) { 0x80 } else { 0 };
            return Some(TRANSLATION[usize::from(byte)] | release);
        }
    }

    /// Loads the output buffer with the next device output byte.
    fn load_device_output(&mut self) -> bool {
        if self.state.output_buffer_state != OutputBufferState::Empty {
            return true;
        }

        if self.state.command_flag & DISABLE_MOUSE_FLAG == 0 {
            if let Some(byte) = self.mouse.output() {
                self.write_output_byte(OutputBufferState::Mouse, byte);
                return true;
            }
        }

        if self.state.command_flag & DISABLE_KEYBOARD_FLAG == 0 {
            if let Some(byte) = self.keyboard_output() {
                self.write_output_byte(OutputBufferState::Keyboard, byte);
                return true;
            }
        }

        false
    }

    /// Loads the output buffer with the next device output byte, then drives both lines (a command byte write may have
    /// changed an interrupt enable).
    fn check_devices_for_output(&mut self) {
        self.load_device_output();
        self.sync_interrupts();
    }
}

/// Host input for the 8042's keyboard and mouse, from any thread (see the module documentation).
#[derive(Clone)]
pub struct Ps2Input(Arc<Mutex<I8042>>);
impl Ps2Input {
    fn lock(&self) -> std::sync::MutexGuard<'_, I8042> {
        self.0.lock().unwrap_or_else(|e| e.into_inner())
    }
    /// Queues a key's set 2 bytes (a make code, or F0 and the code for its break; E0-prefixed keys with their prefix)
    /// while the keyboard scans; the controller translates them to set 1 under XLAT.
    pub fn keyboard(&self, set2: &[u8]) {
        let mut c = self.lock();
        set2.iter().for_each(|&b| c.keyboard.key(b));
        c.check_devices_for_output();
    }
    /// Mouse movement: `dx` to the right, `dy` up (the PS/2 mouse's directions), with the buttons now held ([`LEFT`],
    /// [`RIGHT`], [`MIDDLE`]); a packet is queued while the mouse reports in stream mode.
    pub fn mouse(&self, dx: i16, dy: i16, buttons: u8) {
        let mut c = self.lock();
        c.mouse.movement(dx, dy, buttons);
        c.check_devices_for_output();
    }
}

/// Puts the 8042 on `devices` at ports 60H and 64H, its interrupts on `irqs` (ISA IRQ 1 and 12) and its reset on the
/// registry's power request; the host's input handle. Port 64H must be free: `clock::install` with `ps2` true.
pub(crate) fn install(devices: &mut Devices, irqs: &Irqs) -> crate::Result<Ps2Input> {
    let controller = Arc::new(Mutex::new(I8042::new(devices.power.clone(), irqs.clone())));
    devices.register_ports(DATA_PORT.into(), 1, controller.clone())?;
    devices.register_ports(
        COMMAND_PORT.into(),
        1,
        Arc::new(Mutex::new(At(controller.clone(), COMMAND_OFFSET))),
    )?;
    Ok(Ps2Input(controller))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pm;

    const OBF: u8 = 1;
    const AUX: u8 = 0x20;
    const XLAT: u8 = 0x40;

    /// The 8042 on a registry with the interrupt controllers (no vCPU): its ports through `Devices::port`, IRQ 1 and
    /// 12 through the chipset's input levels.
    struct Board {
        devices: Devices,
        irqs: Irqs,
        input: Ps2Input,
    }
    fn board() -> Board {
        let memory = Arc::new(crate::memory::Memory::with_size(4096).unwrap());
        let uart = Arc::new(Mutex::new(crate::uart::Uart::new()));
        let mut devices = Devices::new(1 << 30);
        let irqs = crate::irq::install(&mut devices, &memory, 0, &uart).unwrap();
        let input = install(&mut devices, &irqs).unwrap();
        Board { devices, irqs, input }
    }
    impl Board {
        fn status(&self) -> u8 {
            self.devices.port(0, COMMAND_PORT, 1, None).unwrap() as u8
        }
        fn command(&self, command: u8) {
            assert_eq!(self.devices.port(0, COMMAND_PORT, 1, Some(command.into())), Ok(0), "{command:#x} refused");
        }
        fn data(&self, data: u8) {
            assert_eq!(self.devices.port(0, DATA_PORT, 1, Some(data.into())), Ok(0), "{data:#x} refused");
        }
        /// A controller command with its argument byte.
        fn command_with(&self, command: u8, data: u8) {
            self.command(command);
            self.data(data);
        }
        /// Everything the output buffer delivers, read as a driver does: while the status says it is full.
        fn drain(&self) -> Vec<u8> {
            let mut out = Vec::new();
            while self.status() & OBF != 0 {
                assert!(out.len() < 64, "the output buffer never empties: {out:x?}");
                out.push(self.devices.port(0, DATA_PORT, 1, None).unwrap() as u8);
            }
            out
        }
        /// Every byte the mouse answers `bytes` with (each sent with D4).
        fn mouse(&self, bytes: &[u8]) -> Vec<u8> {
            let mut out = Vec::new();
            for &b in bytes {
                self.command_with(0xd4, b);
                assert!(self.status() & AUX != 0 || self.status() & OBF == 0, "a mouse reply without AUXB");
                out.extend(self.drain());
            }
            out
        }
        fn level(&self, irq: usize) -> bool {
            self.irqs.level(irq)
        }
    }

    /// Controller self-test AAH answers 55H; the interface tests (keyboard ABH, mouse A9H) answer 00H (no error);
    /// the command byte starts as 47H (both interrupts, system flag, XLAT), as upstream's.
    #[test]
    fn self_test_answers_55() {
        let b = board();
        assert_eq!(b.status() & OBF, 0, "empty after power-on");
        b.command(0xaa);
        assert_eq!(b.status() & (OBF | AUX), OBF);
        assert_eq!(b.drain(), [0x55]);
        b.command(0xab);
        assert_eq!(b.drain(), [0x00]);
        b.command(0xa9);
        assert_eq!(b.drain(), [0x00]);
        b.command(0x20);
        assert_eq!(b.drain(), [0x47]);
        b.command_with(0x60, 0x65);
        b.command(0x20);
        assert_eq!(b.drain(), [0x65], "the command byte as written");
        assert_eq!(b.devices.power(), 0);
    }

    /// Keyboard reset (FFH to 60H): acknowledge FAH, then the self-test pass AAH (both unchanged by XLAT).
    #[test]
    fn keyboard_reset_answers_fa_aa() {
        let b = board();
        b.data(0xff);
        assert_eq!(b.status() & (OBF | AUX), OBF);
        assert!(b.level(1), "IRQ 1 with the acknowledgement");
        assert_eq!(b.drain(), [0xfa, 0xaa]);
    }

    /// Identify (F2H): FA AB 83, an MF2 keyboard in set 2; through XLAT (the default command byte) the controller
    /// translates the 83H to 41H, as a PC's does (Linux then names it "AT Translated Set 2 keyboard").
    #[test]
    fn keyboard_id_is_ab_83() {
        let b = board();
        b.command_with(0x60, 0x47 & !XLAT);
        b.data(0xf2);
        assert_eq!(b.drain(), [0xfa, 0xab, 0x83]);
        b.command_with(0x60, 0x47);
        b.data(0xf2);
        assert_eq!(b.drain(), [0xfa, 0xab, 0x41]);
        // Echo, an argument command (LEDs), an unknown command (resend FEH).
        b.data(0xee);
        b.data(0xed);
        b.data(0x07);
        b.data(0x01);
        assert_eq!(b.drain(), [0xee, 0xfa, 0xfa, 0xfe]);
    }

    /// XLAT (command byte bit 6): a host key's set 2 make code 1CH (A) arrives as set 1's 1EH, its break F0 1C as 9EH;
    /// without XLAT the bytes arrive as sent.
    #[test]
    fn xlat_translates_set2_to_set1() {
        let b = board();
        b.input.keyboard(&[0x1c]);
        assert_eq!(b.drain(), [0x1e]);
        b.input.keyboard(&[0xf0, 0x1c, 0xe0, 0x75, 0xe0, 0xf0, 0x75]);
        assert_eq!(b.drain(), [0x9e, 0xe0, 0x48, 0xe0, 0xc8], "break; up arrow make and break");
        b.command_with(0x60, 0x47 & !XLAT);
        b.input.keyboard(&[0x1c]);
        assert_eq!(b.drain(), [0x1c]);
        b.input.keyboard(&[0xf0, 0x1c]);
        assert_eq!(b.drain(), [0xf0, 0x1c]);
    }

    /// Host input while the guest is idle: a key queued from another thread reaches the output buffer and raises
    /// IRQ 1 there and then, with no guest access in between (the vCPU halted in HLT is woken by the interrupt, not by
    /// a read it would never make). Not while the command byte's keyboard interrupt bit is clear: the byte still waits
    /// in the output buffer.
    #[test]
    fn host_key_raises_irq1_without_a_guest_read() {
        fn send<T: Send + Clone>(_: &T) {}
        let b = board();
        send(&b.input);
        assert!(!b.level(1));
        let input = b.input.clone();
        std::thread::spawn(move || input.keyboard(&[0x1c])).join().unwrap();
        assert!(b.level(1), "IRQ 1 raised by the host thread");
        assert!(!b.level(12));
        assert_eq!(b.drain(), [0x1e]);
        assert!(!b.level(1), "lowered by the read");
        // A second key: the line rises again (an edge per byte).
        let input = b.input.clone();
        std::thread::spawn(move || input.keyboard(&[0x32])).join().unwrap();
        assert!(b.level(1));
        assert_eq!(b.drain(), [0x30]);
        b.command_with(0x60, 0x46);
        let input = b.input.clone();
        std::thread::spawn(move || input.keyboard(&[0x1c])).join().unwrap();
        assert!(!b.level(1), "the keyboard interrupt is disabled");
        assert_eq!(b.drain(), [0x1e]);
    }

    /// Mouse reset (D4H FFH): FA, the self-test pass AA, the ID 00; every byte flagged as the mouse's (AUXB).
    #[test]
    fn mouse_reset_answers_fa_aa_00() {
        let b = board();
        assert_eq!(b.mouse(&[0xff]), [0xfa, 0xaa, 0x00]);
        assert_eq!(b.mouse(&[0xf2]), [0xfa, 0x00]);
        // Status request after reset: stream mode, reporting off, 1:1; resolution 2 (4 counts/mm), 100 samples/s.
        assert_eq!(b.mouse(&[0xe9]), [0xfa, 0x00, 0x02, 100]);
        assert_eq!(b.mouse(&[0xe8, 0x03, 0xf3, 40, 0xe7, 0xf0, 0xe9]), [0xfa, 0xfa, 0xfa, 0xfa, 0xfa, 0xfa, 0xfa, 0x50, 0x03, 40]);
        assert_eq!(b.mouse(&[0xf6, 0xe9]), [0xfa, 0xfa, 0x00, 0x02, 100], "set defaults");
        // Wrap mode echoes until EC; an unknown command is answered FE.
        assert_eq!(b.mouse(&[0xee, 0x12, 0xe9, 0xec, 0x12]), [0xfa, 0x12, 0xe9, 0xfa, 0xfe]);
    }

    /// The IntelliMouse sequence (sample rates 200, 100, 80) makes the ID 03, and packets then have a fourth (wheel)
    /// byte; a reset makes it 00 again. Another order does not.
    #[test]
    fn intellimouse_sequence_makes_id_3() {
        let b = board();
        assert_eq!(b.mouse(&[0xf3, 200, 0xf3, 80, 0xf3, 100, 0xf2]), [0xfa; 6].iter().copied().chain([0xfa, 0x00]).collect::<Vec<_>>());
        assert_eq!(b.mouse(&[0xf3, 200, 0xf3, 100, 0xf3, 80, 0xf2]), [0xfa; 6].iter().copied().chain([0xfa, 0x03]).collect::<Vec<_>>());
        assert_eq!(b.mouse(&[0xf4]), [0xfa]);
        b.input.mouse(1, 1, 0);
        assert_eq!(b.drain(), [0x08, 1, 1, 0]);
        assert_eq!(b.mouse(&[0xff, 0xf2]), [0xfa, 0xaa, 0x00, 0xfa, 0x00]);
    }

    /// Movement from the host with reporting on: a packet (buttons, signs, low bytes) in the output buffer and IRQ 12
    /// raised from the host thread, each byte flagged AUXB. Before reporting is enabled no packet is sent.
    #[test]
    fn mouse_packet_after_movement_raises_irq12() {
        let b = board();
        b.input.mouse(5, -3, mouse::LEFT);
        assert_eq!(b.status() & OBF, 0, "reporting is off after power-on");
        assert_eq!(b.mouse(&[0xf4]), [0xfa]);
        let input = b.input.clone();
        std::thread::spawn(move || input.mouse(5, -3, mouse::LEFT)).join().unwrap();
        assert!(b.level(12) && !b.level(1), "IRQ 12 from the host thread");
        assert_eq!(b.status() & (OBF | AUX), OBF | AUX);
        assert_eq!(b.drain(), [0x08 | 0x01 | 0x20, 5, 0xfd]);
        assert!(!b.level(12));
        // Beyond the 9-bit range: the overflow bits and the extremes.
        b.input.mouse(300, -300, mouse::RIGHT | mouse::MIDDLE);
        assert_eq!(b.drain(), [0x08 | 0x06 | 0x40 | 0x80 | 0x20, 0xff, 0x00]);
        // Remote mode: no packet until read-data (EBH).
        assert_eq!(b.mouse(&[0xf0]), [0xfa]);
        b.input.mouse(-2, 7, 0);
        assert_eq!(b.status() & OBF, 0);
        assert_eq!(b.mouse(&[0xeb]), [0xfa, 0x08 | 0x10, 0xfe, 7]);
    }

    /// The pulse-output command FEH (bit 0 clear: the reset line) makes the power request `pm::RESET`, the stop that
    /// port 64H gives without the 8042; so does every even pulse command. FFH pulses nothing.
    #[test]
    fn pulse_reset_requests_reset() {
        let b = board();
        b.command(0xff);
        assert_eq!(b.devices.power(), 0);
        b.command(0xfe);
        assert_eq!(b.devices.power(), pm::RESET);
        let b = board();
        b.command(0xf0);
        assert_eq!(b.devices.power(), pm::RESET);
    }

    /// Writing the output port (D1H) with bit 0 (the reset line) clear makes the power request `pm::RESET`; with it set
    /// nothing happens. D0H reads the output port with the reset line high.
    #[test]
    fn output_port_reset_bit_requests_reset() {
        let b = board();
        b.command(0xd0);
        assert_eq!(b.drain()[0] & 1, 1);
        b.command_with(0xd1, 0x03);
        assert_eq!(b.devices.power(), 0);
        b.command_with(0xd1, 0xfe);
        assert_eq!(b.devices.power(), pm::RESET);
    }

    /// Commands while the output buffer is full: never refused, the vCPU never stopped. A keyboard command (F2H to 60H)
    /// queues its reply in the keyboard behind the unread byte, which the guest reads first. A controller command
    /// (20H) puts its reply in the output buffer over the unread byte, which is lost: OpenVMM's controller
    /// (`write_output_byte`), whose behaviour this keeps.
    #[test]
    fn command_with_full_output_buffer() {
        let b = board();
        b.input.keyboard(&[0x1c]);
        assert_eq!(b.status() & OBF, OBF);
        b.data(0xf2);
        assert_eq!(b.drain(), [0x1e, 0xfa, 0xab, 0x41]);
        b.input.keyboard(&[0x1c]);
        b.command(0x20);
        assert_eq!(b.drain(), [0x47], "the controller's reply replaced the unread key");
        // A mouse command likewise waits behind an unread keyboard byte.
        b.input.keyboard(&[0x1c]);
        b.command_with(0xd4, 0xf2);
        assert_eq!(b.drain(), [0x1e, 0xfa, 0x00], "the unread keyboard byte, then the mouse's reply");
        assert_eq!(b.devices.power(), 0);
    }

    /// Without PS/2 (`clock::install` with `ps2` false and no 8042) port 64H is `pm`'s reset-only register: it reads 0,
    /// FEH requests the reset, 60H is unassigned. With `ps2` true `clock::install` leaves 64H to the 8042.
    #[test]
    fn disabled_ps2_keeps_reset_only_port() {
        let memory = Arc::new(crate::memory::Memory::with_size(4096).unwrap());
        let uart = Arc::new(Mutex::new(crate::uart::Uart::new()));
        let mut devices = Devices::new(1 << 30);
        let irqs = crate::irq::install(&mut devices, &memory, 0, &uart).unwrap();
        let board = crate::clock::install(&mut devices, &irqs, crate::clock::LostTicks::DEFAULT, &uart, false).unwrap();
        assert_eq!(devices.port(0, COMMAND_PORT, 1, None), Ok(0));
        assert_eq!(devices.port(0, DATA_PORT, 1, None), Ok(0xff));
        assert_eq!(devices.port(0, COMMAND_PORT, 1, Some(0x20)), Ok(0));
        assert_eq!(devices.port(0, DATA_PORT, 1, None), Ok(0xff), "no command byte");
        assert_eq!(devices.power(), 0);
        assert_eq!(devices.port(0, COMMAND_PORT, 1, Some(0xfe)), Ok(0));
        assert_eq!(devices.power(), pm::RESET);
        board.stop();
        let mut devices = Devices::new(1 << 30);
        let irqs = crate::irq::install(&mut devices, &memory, 0, &uart).unwrap();
        let board = crate::clock::install(&mut devices, &irqs, crate::clock::LostTicks::DEFAULT, &uart, true).unwrap();
        assert_eq!(devices.port(0, COMMAND_PORT, 1, None), Ok(0xff), "64H unassigned until the 8042 is installed");
        install(&mut devices, &irqs).unwrap();
        assert_eq!(devices.port(0, COMMAND_PORT, 1, None), Ok(0x14), "the 8042's status: system flag, unlocked");
        board.stop();
    }
}
