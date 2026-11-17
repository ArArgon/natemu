// Copyright 2025 The Cloud Hypervisor Authors. All rights reserved.
//
// SPDX-License-Identifier: Apache-2.0
//
// natemu: the zlib and zstd decoders were removed (no flate2/zstd dependency). The trait stays so
// the vendored call sites are unchanged; every decode fails, which callers turn into EIO.

use std::result;

use thiserror::Error;

#[derive(Debug, Error)]
pub enum Error {
    #[error("Compressed clusters are not supported")]
    Unsupported,
}

pub(super) type Result<T> = result::Result<T, Error>;

/// Decoder for compressed clusters.
pub trait Decoder: Send + Sync {
    fn decode(&self, input: &[u8], output: &mut [u8]) -> Result<usize>;
}

#[derive(Default)]
pub(super) struct UnsupportedDecoder {}

impl Decoder for UnsupportedDecoder {
    fn decode(&self, _input: &[u8], _output: &mut [u8]) -> Result<usize> {
        Err(Error::Unsupported)
    }
}
