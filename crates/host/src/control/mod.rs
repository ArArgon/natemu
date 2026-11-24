//! The control API (`docs/restructure/control-api-plan.md`): a program on the Mac drives the VM through JSON-RPC 2.0
//! requests on a Unix socket. [`protocol`] is the wire format; [`ps2`] turns input events into the 8042's keyboard
//! and mouse bytes, with flow control so no input is lost.
pub mod protocol;
pub mod ps2;
