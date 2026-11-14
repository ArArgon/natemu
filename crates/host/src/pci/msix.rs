// Copyright © 2019 Intel Corporation
//
// SPDX-License-Identifier: Apache-2.0 OR BSD-3-Clause
//
// Adapted from cloud-hypervisor pci/src/msix.rs at f84dae558c8b9518e9d9bf8c0f1b64cd4dc8e546: interrupts go out through
// a `Deliver` callback instead of an interrupt source group, no snapshot/migration, trigger() added (from
// virtio-devices/src/transport/pci_device.rs). Divergences from upstream's behaviour: MsixCap::new leaves Enable
// clear (PCI 3.0 6.8.2.3); set_msg_ctl delivers pending vectors when MSI-X becomes enabled and unmasked, not when
// the function mask clears (PCI 3.0 6.8.2.3, 6.8.2.9); an optional bus-master gate (`set_gate`) holds every message,
// sent or released, while the function's Bus Master bit is clear (PCI 3.0 6.2.2: no memory write, and an MSI is one):
// trigger() then sets the pending bit as for a masked vector, and `release_pending` sends what became deliverable when
// it sets. Additions: `clear_pending` (a device reset drops pending vectors, as QEMU's `msix_unuse_all_vectors` on a
// virtio reset). Upstream items natemu does not use are kept, hence the allow below.
#![allow(dead_code)]

use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering::SeqCst},
};
use std::{io, result};

use super::configuration::{PciCapability, PciCapabilityId};
use log::{debug, error};
use serde::{Deserialize, Serialize};
use thiserror::Error;

const MAX_MSIX_VECTORS_PER_DEVICE: u16 = 2048;
const MSIX_TABLE_ENTRIES_MODULO: u64 = 16;
const MSIX_PBA_ENTRIES_MODULO: u64 = 8;
const BITS_PER_PBA_ENTRY: usize = 64;
const FUNCTION_MASK_BIT: u8 = 14;
const MSIX_ENABLE_BIT: u8 = 15;
const FUNCTION_MASK_MASK: u16 = 1u16 << FUNCTION_MASK_BIT;
const MSIX_ENABLE_MASK: u16 = 1u16 << MSIX_ENABLE_BIT;
pub const MSIX_TABLE_ENTRY_SIZE: usize = 16;

/// Sends one MSI: the entry's 64-bit message address (upper << 32 | lower) and 32-bit message data. Replaces
/// upstream's interrupt source group (a Linux eventfd).
pub type Deliver = Arc<dyn Fn(u64, u32) + Send + Sync>;

#[derive(Error, Debug)]
pub enum Error {
    /// Failed enabling the interrupt route.
    #[error("Failed enabling the interrupt route")]
    EnableInterruptRoute(#[source] io::Error),
    /// Failed updating the interrupt route.
    #[error("Failed updating the interrupt route")]
    UpdateInterruptRoute(#[source] io::Error),
}

#[derive(Debug, Clone, Serialize, Deserialize, Eq, PartialEq)]
pub struct MsixTableEntry {
    pub msg_addr_lo: u32,
    pub msg_addr_hi: u32,
    pub msg_data: u32,
    pub vector_ctl: u32,
}

impl MsixTableEntry {
    pub fn masked(&self) -> bool {
        self.vector_ctl & 0x1 == 0x1
    }
}

impl Default for MsixTableEntry {
    fn default() -> Self {
        MsixTableEntry {
            msg_addr_lo: 0,
            msg_addr_hi: 0,
            msg_data: 0,
            vector_ctl: 0x1,
        }
    }
}

pub struct MsixConfig {
    pub table_entries: Vec<MsixTableEntry>,
    pub pba_entries: Vec<u64>,
    pub devid: u32,
    deliver: Deliver,
    masked: bool,
    enabled: bool,
    /// natemu: the function's Bus Master bit (None: always set, as upstream).
    gate: Option<Arc<AtomicBool>>,
}

impl MsixConfig {
    pub fn new(msix_vectors: u16, deliver: Deliver, devid: u32) -> result::Result<Self, Error> {
        assert!(msix_vectors <= MAX_MSIX_VECTORS_PER_DEVICE);

        let mut table_entries: Vec<MsixTableEntry> = Vec::new();
        table_entries.resize_with(msix_vectors as usize, Default::default);
        let mut pba_entries: Vec<u64> = Vec::new();
        let num_pba_entries: usize = ((msix_vectors as usize) / BITS_PER_PBA_ENTRY) + 1;
        pba_entries.resize_with(num_pba_entries, Default::default);

        Ok(MsixConfig {
            table_entries,
            pba_entries,
            devid,
            deliver,
            masked: true,
            enabled: false,
            gate: None,
        })
    }

    pub fn masked(&self) -> bool {
        self.masked
    }

    pub fn enabled(&self) -> bool {
        self.enabled
    }

    pub fn set_msg_ctl(&mut self, reg: u16) {
        let was_active = self.enabled && !self.masked;

        self.masked = ((reg >> FUNCTION_MASK_BIT) & 1u16) == 1u16;
        self.enabled = ((reg >> MSIX_ENABLE_BIT) & 1u16) == 1u16;

        // natemu: keyed on MSI-X becoming enabled and unmasked (upstream: on the function mask clearing), so that
        // nothing is sent while MSI-X is disabled and enabling with the mask clear releases pending vectors
        // (PCI 3.0 6.8.2.3, 6.8.2.9). When that happens it is important to go through the entire PBA to check if
        // there was any pending MSI-X message to inject, given that the vector is not masked.
        if !was_active {
            self.release_pending();
        }
    }

    /// natemu: drop every pending vector (a device reset).
    pub fn clear_pending(&mut self) {
        self.pba_entries.fill(0);
    }

    /// natemu: messages wait while `gate` is clear (the function's Bus Master bit).
    pub fn set_gate(&mut self, gate: Arc<AtomicBool>) {
        self.gate = Some(gate);
    }

    /// natemu: the function may send a message now: bus mastering on (PCI 3.0 6.2.2).
    fn may_send(&self) -> bool {
        self.gate.as_ref().is_none_or(|g| g.load(SeqCst))
    }

    /// Send every pending vector whose entry is unmasked, if MSI-X is enabled, the function unmasked and the gate set
    /// (upstream's loop from set_msg_ctl; natemu also calls it when bus mastering turns on).
    pub fn release_pending(&mut self) {
        if !self.enabled || self.masked || !self.may_send() {
            return;
        }
        for (index, entry) in self.table_entries.clone().iter().enumerate() {
            if !entry.masked() && self.get_pba_bit(index as u16) == 1 {
                self.inject_msix_and_clear_pba(index);
            }
        }
    }

    pub fn read_table(&self, offset: u64, data: &mut [u8]) {
        if data.len() != 4 && data.len() != 8 {
            error!(
                "invalid MSI-X table read size: {} (allowed: 4 or 8)",
                data.len()
            );
            data.fill(0xff);
            return;
        }

        let index: usize = (offset / MSIX_TABLE_ENTRIES_MODULO) as usize;
        let modulo_offset = offset % MSIX_TABLE_ENTRIES_MODULO;

        if index >= self.table_entries.len() {
            debug!("Invalid MSI-X table entry index {index}");
            data.copy_from_slice(&[0xff; 8][..data.len()]);
            return;
        }

        match data.len() {
            4 => {
                let value = match modulo_offset {
                    0x0 => self.table_entries[index].msg_addr_lo,
                    0x4 => self.table_entries[index].msg_addr_hi,
                    0x8 => self.table_entries[index].msg_data,
                    0xc => self.table_entries[index].vector_ctl,
                    _ => {
                        debug!("invalid offset");
                        0
                    }
                };

                debug!("MSI_R TABLE offset 0x{offset:x} data 0x{value:x}");
                data.copy_from_slice(&value.to_le_bytes());
            }
            8 => {
                let value = match modulo_offset {
                    0x0 => {
                        (u64::from(self.table_entries[index].msg_addr_hi) << 32)
                            | u64::from(self.table_entries[index].msg_addr_lo)
                    }
                    0x8 => {
                        (u64::from(self.table_entries[index].vector_ctl) << 32)
                            | u64::from(self.table_entries[index].msg_data)
                    }
                    _ => {
                        debug!("invalid offset");
                        0
                    }
                };

                debug!("MSI_R TABLE offset 0x{offset:x} data 0x{value:x}");
                data.copy_from_slice(&value.to_le_bytes());
            }
            _ => {
                debug!("invalid data length");
            }
        }
    }

    pub fn write_table(&mut self, offset: u64, data: &[u8]) {
        if data.len() != 4 && data.len() != 8 {
            error!(
                "invalid MSI-X table write size: {} (allowed: 4 or 8)",
                data.len()
            );
            return;
        }

        let index: usize = (offset / MSIX_TABLE_ENTRIES_MODULO) as usize;
        let modulo_offset = offset % MSIX_TABLE_ENTRIES_MODULO;

        if index >= self.table_entries.len() {
            debug!("Invalid MSI-X table entry index {index}");
            return;
        }

        // Store the value of the entry before modification
        let old_entry = self.table_entries[index].clone();

        match data.len() {
            4 => {
                let value = u32::from_le_bytes(data.try_into().expect("data should be 4 bytes"));
                match modulo_offset {
                    0x0 => self.table_entries[index].msg_addr_lo = value,
                    0x4 => self.table_entries[index].msg_addr_hi = value,
                    0x8 => self.table_entries[index].msg_data = value,
                    0xc => {
                        self.table_entries[index].vector_ctl = value;
                    }
                    _ => debug!("invalid offset"),
                }

                debug!("MSI_W TABLE offset 0x{offset:x} data 0x{value:x}");
            }
            8 => {
                let value = u64::from_le_bytes(data.try_into().expect("data should be 8 bytes"));
                match modulo_offset {
                    0x0 => {
                        self.table_entries[index].msg_addr_lo = (value & 0xffff_ffffu64) as u32;
                        self.table_entries[index].msg_addr_hi = (value >> 32) as u32;
                    }
                    0x8 => {
                        self.table_entries[index].msg_data = (value & 0xffff_ffffu64) as u32;
                        self.table_entries[index].vector_ctl = (value >> 32) as u32;
                    }
                    _ => debug!("invalid offset"),
                }

                debug!("MSI_W TABLE offset 0x{offset:x} data 0x{value:x}");
            }
            _ => debug!("invalid data length"),
        }

        let table_entry = &self.table_entries[index];

        // Optimisation to avoid excessive updates
        if &old_entry == table_entry {
            return;
        }

        // After the MSI-X table entry has been updated, it is necessary to
        // check if the vector control masking bit has changed. In case the
        // bit has been flipped from 1 to 0, we need to inject a MSI message
        // if the corresponding pending bit from the PBA is set. Once the MSI
        // has been injected, the pending bit in the PBA needs to be cleared.
        // All of this is valid only if MSI-X has not been masked for the whole
        // device.

        // Check if bit has been flipped
        if !self.masked()
            && self.enabled()
            && self.may_send()
            && old_entry.masked()
            && !table_entry.masked()
            && self.get_pba_bit(index as u16) == 1
        {
            self.inject_msix_and_clear_pba(index);
        }
    }

    pub fn read_pba(&mut self, offset: u64, data: &mut [u8]) {
        if data.len() != 4 && data.len() != 8 {
            error!(
                "invalid MSI-X PBA read size: {} (allowed: 4 or 8)",
                data.len()
            );
            data.fill(0xff);
            return;
        }

        let index: usize = (offset / MSIX_PBA_ENTRIES_MODULO) as usize;
        let modulo_offset = offset % MSIX_PBA_ENTRIES_MODULO;

        if index >= self.pba_entries.len() {
            debug!("Invalid MSI-X PBA entry index {index}");
            data.copy_from_slice(&[0xff; 8][..data.len()]);
            return;
        }

        match data.len() {
            4 => {
                let value: u32 = match modulo_offset {
                    0x0 => (self.pba_entries[index] & 0xffff_ffffu64) as u32,
                    0x4 => (self.pba_entries[index] >> 32) as u32,
                    _ => {
                        debug!("invalid offset");
                        0
                    }
                };

                debug!("MSI_R PBA offset 0x{offset:x} data 0x{value:x}");
                data.copy_from_slice(&value.to_le_bytes());
            }
            8 => {
                let value: u64 = match modulo_offset {
                    0x0 => self.pba_entries[index],
                    _ => {
                        debug!("invalid offset");
                        0
                    }
                };

                debug!("MSI_R PBA offset 0x{offset:x} data 0x{value:x}");
                data.copy_from_slice(&value.to_le_bytes());
            }
            _ => {
                debug!("invalid data length");
            }
        }
    }

    pub fn write_pba(&mut self, _offset: u64, _data: &[u8]) {
        error!("Pending Bit Array is read only");
    }

    pub fn set_pba_bit(&mut self, vector: u16, reset: bool) {
        assert!(vector < MAX_MSIX_VECTORS_PER_DEVICE);

        let index: usize = (vector as usize) / BITS_PER_PBA_ENTRY;
        let shift: usize = (vector as usize) % BITS_PER_PBA_ENTRY;
        let mut mask = 1u64 << shift;

        if reset {
            mask = !mask;
            self.pba_entries[index] &= mask;
        } else {
            self.pba_entries[index] |= mask;
        }
    }

    fn get_pba_bit(&self, vector: u16) -> u8 {
        assert!(vector < MAX_MSIX_VECTORS_PER_DEVICE);

        let index: usize = (vector as usize) / BITS_PER_PBA_ENTRY;
        let shift: usize = (vector as usize) % BITS_PER_PBA_ENTRY;

        ((self.pba_entries[index] >> shift) & 0x0000_0001u64) as u8
    }

    fn inject_msix_and_clear_pba(&mut self, vector: usize) {
        // Inject the MSI message
        let entry = &self.table_entries[vector];
        (self.deliver)(
            (u64::from(entry.msg_addr_hi) << 32) | u64::from(entry.msg_addr_lo),
            entry.msg_data,
        );
        debug!("MSI-X injected on vector control flip");

        // Clear the bit from PBA
        self.set_pba_bit(vector as u16, true);
    }

    /// A device interrupt on `vector` (natemu addition; upstream has this in the virtio transport's
    /// `VirtioInterruptMsix::trigger`). Delivers the entry's message, or sets its pending bit when the entry or the
    /// function is masked or the gate is clear (the release paths above then deliver it once). Does nothing while
    /// MSI-X is disabled or for a vector beyond the table.
    pub fn trigger(&mut self, vector: u16) {
        if !self.enabled || usize::from(vector) >= self.table_entries.len() {
            return;
        }
        // In case the vector control register associated with the entry has its first bit set, this means the
        // vector is masked and the device should not inject the interrupt. Instead, the Pending Bit Array table
        // is updated to reflect there is a pending interrupt for this specific vector. natemu: likewise while the
        // gate (Bus Master) is clear.
        if self.masked || self.table_entries[usize::from(vector)].masked() || !self.may_send() {
            self.set_pba_bit(vector, false);
            return;
        }
        let entry = &self.table_entries[usize::from(vector)];
        (self.deliver)(
            (u64::from(entry.msg_addr_hi) << 32) | u64::from(entry.msg_addr_lo),
            entry.msg_data,
        );
    }
}

#[repr(C, packed)]
#[derive(Clone, Copy, Default, Serialize, Deserialize)]
pub struct MsixCap {
    // Message Control Register
    //   10-0:  MSI-X Table size
    //   13-11: Reserved
    //   14:    Mask. Mask all MSI-X when set.
    //   15:    Enable. Enable all MSI-X when set.
    pub msg_ctl: u16,
    // Table. Contains the offset and the BAR indicator (BIR)
    //   2-0:  Table BAR indicator (BIR). Can be 0 to 5.
    //   31-3: Table offset in the BAR pointed by the BIR.
    pub table: u32,
    // Pending Bit Array. Contains the offset and the BAR indicator (BIR)
    //   2-0:  PBA BAR indicator (BIR). Can be 0 to 5.
    //   31-3: PBA offset in the BAR pointed by the BIR.
    pub pba: u32,
}

impl PciCapability for MsixCap {
    fn bytes(&self) -> &[u8] {
        // upstream: ByteValued::as_slice. The packed repr(C) struct is plain integers with no padding, and
        // the host is little-endian like the guest, so its bytes are the capability's layout.
        // SAFETY: the pointer and length come from `self`, and any bytes may be read as u8.
        unsafe {
            std::slice::from_raw_parts(
                (self as *const Self).cast::<u8>(),
                std::mem::size_of::<Self>(),
            )
        }
    }

    fn id(&self) -> PciCapabilityId {
        PciCapabilityId::MsiX
    }
}

impl MsixCap {
    pub fn new(
        table_pci_bar: u8,
        table_size: u16,
        table_off: u32,
        pba_pci_bar: u8,
        pba_off: u32,
    ) -> Self {
        assert!(table_size < MAX_MSIX_VECTORS_PER_DEVICE);

        // Set the table size. natemu: Enable and Function Mask reset to 0 (PCI 3.0 6.8.2.3); upstream sets Enable.
        let msg_ctl: u16 = table_size - 1;

        MsixCap {
            msg_ctl,
            table: (table_off & 0xffff_fff8u32) | u32::from(table_pci_bar & 0x7u8),
            pba: (pba_off & 0xffff_fff8u32) | u32::from(pba_pci_bar & 0x7u8),
        }
    }

    pub fn set_msg_ctl(&mut self, data: u16) {
        self.msg_ctl = (self.msg_ctl & !(FUNCTION_MASK_MASK | MSIX_ENABLE_MASK))
            | (data & (FUNCTION_MASK_MASK | MSIX_ENABLE_MASK));
    }

    pub fn masked(&self) -> bool {
        (self.msg_ctl >> FUNCTION_MASK_BIT) & 0x1 == 0x1
    }

    pub fn enabled(&self) -> bool {
        (self.msg_ctl >> MSIX_ENABLE_BIT) & 0x1 == 0x1
    }

    pub fn table_offset(&self) -> u32 {
        self.table & 0xffff_fff8
    }

    pub fn pba_offset(&self) -> u32 {
        self.pba & 0xffff_fff8
    }

    pub fn table_set_offset(&mut self, addr: u32) {
        self.table &= 0x7;
        self.table += addr;
    }

    pub fn pba_set_offset(&mut self, addr: u32) {
        self.pba &= 0x7;
        self.pba += addr;
    }

    pub fn table_bir(&self) -> u32 {
        self.table & 0x7
    }

    pub fn pba_bir(&self) -> u32 {
        self.pba & 0x7
    }

    pub fn table_size(&self) -> u16 {
        (self.msg_ctl & 0x7ff) + 1
    }

    pub fn table_range(&self) -> (u64, u64) {
        // The table takes 16 bytes per entry.
        let size = self.table_size() as u64 * 16;
        (self.table_offset() as u64, size)
    }

    pub fn pba_range(&self) -> (u64, u64) {
        // The table takes 1 bit per entry modulo 8 bytes.
        let size = ((self.table_size() as u64 / 64) + 1) * 8;
        (self.pba_offset() as u64, size)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_set_pba_bit() {
        let mut config = MsixConfig::new(
            MAX_MSIX_VECTORS_PER_DEVICE,
            Arc::new(|_, _| unreachable!()),
            0,
        )
        .unwrap();

        for vector in 0..MAX_MSIX_VECTORS_PER_DEVICE {
            let index = usize::from(vector) / BITS_PER_PBA_ENTRY;
            let mask = 1u64 << (usize::from(vector) % BITS_PER_PBA_ENTRY);

            config.pba_entries.fill(0);
            let mut expected = config.pba_entries.clone();
            expected[index] = mask;
            config.set_pba_bit(vector, false);
            assert_eq!(config.pba_entries, expected, "Setting vector {vector}");

            config.pba_entries.fill(u64::MAX);
            let mut expected = config.pba_entries.clone();
            expected[index] = !mask;
            config.set_pba_bit(vector, true);
            assert_eq!(config.pba_entries, expected, "Clearing vector {vector}");
        }
    }

    type Sent = Arc<std::sync::Mutex<Vec<(u64, u32)>>>;

    fn recording(vectors: u16) -> (MsixConfig, Sent) {
        let sent: Sent = Arc::default();
        let log = sent.clone();
        let config = MsixConfig::new(
            vectors,
            Arc::new(move |addr, data| log.lock().unwrap().push((addr, data))),
            0,
        )
        .unwrap();
        (config, sent)
    }

    fn write_entry(config: &mut MsixConfig, index: u64, addr: u64, data: u32, ctl: u32) {
        let base = index * 16;
        config.write_table(base, &addr.to_le_bytes());
        config.write_table(
            base + 8,
            &((u64::from(ctl) << 32) | u64::from(data)).to_le_bytes(),
        );
    }

    const ENABLE: u16 = MSIX_ENABLE_MASK;
    const ENABLE_MASKED: u16 = MSIX_ENABLE_MASK | FUNCTION_MASK_MASK;

    #[test]
    fn unmasked_entry_delivers_address_and_data() {
        let (mut config, sent) = recording(2);
        write_entry(&mut config, 1, 0xfee0_1000 | (1 << 32), 0x41, 0);
        config.set_msg_ctl(ENABLE);
        config.trigger(1);
        assert_eq!(*sent.lock().unwrap(), vec![(0x1_fee0_1000, 0x41)]);
        assert_eq!(config.get_pba_bit(1), 0);
    }

    #[test]
    fn masked_entry_sets_pending_and_delivers_once_on_unmask() {
        let (mut config, sent) = recording(2);
        write_entry(&mut config, 0, 0xfee0_0000, 0x40, 1);
        config.set_msg_ctl(ENABLE);
        config.trigger(0);
        config.trigger(0);
        assert!(sent.lock().unwrap().is_empty());
        assert_eq!(config.get_pba_bit(0), 1);

        write_entry(&mut config, 0, 0xfee0_0000, 0x40, 0);
        assert_eq!(*sent.lock().unwrap(), vec![(0xfee0_0000, 0x40)]);
        assert_eq!(config.get_pba_bit(0), 0);

        // Rewriting the same entry delivers nothing more.
        write_entry(&mut config, 0, 0xfee0_0000, 0x40, 0);
        assert_eq!(sent.lock().unwrap().len(), 1);
    }

    #[test]
    fn function_mask_holds_every_vector_then_delivers_each_pending_once() {
        let (mut config, sent) = recording(3);
        for v in 0..3 {
            write_entry(&mut config, v, 0xfee0_0000 + (v << 12), 0x50 + v as u32, 0);
        }
        // Vector 2 keeps its own mask bit set, so it must stay pending after the function unmask.
        write_entry(&mut config, 2, 0xfee0_2000, 0x52, 1);
        config.set_msg_ctl(ENABLE_MASKED);
        config.trigger(0);
        config.trigger(1);
        config.trigger(1);
        config.trigger(2);
        assert!(sent.lock().unwrap().is_empty());

        config.set_msg_ctl(ENABLE);
        let mut got = sent.lock().unwrap().clone();
        got.sort();
        assert_eq!(got, vec![(0xfee0_0000, 0x50), (0xfee0_1000, 0x51)]);
        assert_eq!((config.get_pba_bit(0), config.get_pba_bit(1)), (0, 0));
        assert_eq!(config.get_pba_bit(2), 1);

        config.set_msg_ctl(ENABLE);
        assert_eq!(sent.lock().unwrap().len(), 2);
    }

    #[test]
    fn disabled_delivers_nothing_and_sets_no_pending() {
        let (mut config, sent) = recording(2);
        write_entry(&mut config, 0, 0xfee0_0000, 0x40, 0);
        write_entry(&mut config, 1, 0xfee0_0000, 0x41, 1);
        // Never enabled, then enabled and disabled again.
        config.trigger(0);
        config.trigger(1);
        config.set_msg_ctl(ENABLE);
        config.set_msg_ctl(0);
        config.trigger(0);
        config.trigger(1);
        // Out of range while enabled.
        config.set_msg_ctl(ENABLE);
        config.trigger(2);
        config.trigger(u16::MAX);
        assert!(sent.lock().unwrap().is_empty());
        assert!(config.pba_entries.iter().all(|&w| w == 0));
    }

    #[test]
    fn rewriting_an_entry_while_masked_delivers_to_the_new_address() {
        // The affinity change sequence Linux uses: mask the entry, rewrite address and data, unmask.
        let (mut config, sent) = recording(1);
        write_entry(&mut config, 0, 0xfee0_0000, 0x40, 0);
        config.set_msg_ctl(ENABLE);
        config.write_table(12, &1u32.to_le_bytes());
        config.trigger(0);
        config.write_table(0, &0xfee0_3000u32.to_le_bytes());
        config.write_table(8, &0x60u32.to_le_bytes());
        assert!(sent.lock().unwrap().is_empty());
        config.write_table(12, &0u32.to_le_bytes());
        assert_eq!(*sent.lock().unwrap(), vec![(0xfee0_3000, 0x60)]);
        config.trigger(0);
        assert_eq!(sent.lock().unwrap()[1], (0xfee0_3000, 0x60));
    }

    #[test]
    fn fresh_msix_cap_has_enable_and_function_mask_clear() {
        // PCI 3.0 6.8.2.3: both bits reset to 0; the size field is N - 1.
        let msg_ctl = MsixCap::new(0, 5, 0x4000, 0, 0x5000).msg_ctl;
        assert_eq!(msg_ctl & (MSIX_ENABLE_MASK | FUNCTION_MASK_MASK), 0);
        assert_eq!(msg_ctl & 0x7ff, 4);
    }

    #[test]
    fn disabling_while_masked_then_unmasking_delivers_nothing_and_keeps_pending() {
        // PCI 3.0 6.8.2.9: no message while MSI-X is disabled.
        let (mut config, sent) = recording(1);
        write_entry(&mut config, 0, 0xfee0_0000, 0x40, 0);
        config.set_msg_ctl(ENABLE_MASKED);
        config.trigger(0);
        config.set_msg_ctl(FUNCTION_MASK_MASK);
        config.set_msg_ctl(0);
        assert!(sent.lock().unwrap().is_empty());
        assert_eq!(config.get_pba_bit(0), 1);
    }

    #[test]
    fn enabling_with_function_mask_clear_delivers_pending_once() {
        let (mut config, sent) = recording(2);
        write_entry(&mut config, 0, 0xfee0_0000, 0x40, 0);
        write_entry(&mut config, 1, 0xfee0_1000, 0x41, 1);
        config.set_msg_ctl(ENABLE_MASKED);
        config.trigger(0);
        config.trigger(1);
        // Disable with the function mask clear, then enable again with it still clear.
        config.set_msg_ctl(0);
        assert!(sent.lock().unwrap().is_empty());
        config.set_msg_ctl(ENABLE);
        // Vector 0 is delivered; vector 1 has its own mask bit and stays pending.
        assert_eq!(*sent.lock().unwrap(), vec![(0xfee0_0000, 0x40)]);
        assert_eq!((config.get_pba_bit(0), config.get_pba_bit(1)), (0, 1));
        config.set_msg_ctl(ENABLE);
        assert_eq!(sent.lock().unwrap().len(), 1);
    }
}
