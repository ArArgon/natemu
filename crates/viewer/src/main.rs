//! `natemu-viewer SOCKET`: a window on a running natemu's display (docs/viewer.md). It is a client of natemu's
//! control socket (docs/control-api.md) with two connections: one maps the framebuffer and follows `display_changed`,
//! the other sends keyboard and mouse input, so a slow `send_input` never holds up the picture.
//!
//! The window redraws from the mapped framebuffer about 60 times a second (the Bochs display does not say what
//! changed). Keys go to the guest while the window has focus. A click captures the mouse (hidden, locked, its movement
//! sent as relative motion, as the guest's PS/2 mouse reports it); Control-Option (pressed and let go on its own) lets it go.
//!
//! The window code is macOS only (winit is built with its macOS back end alone); elsewhere the program only says so,
//! so a plain `cargo build` of the workspace works on any host.
#[cfg(target_os = "macos")]
mod app;
#[cfg(target_os = "macos")]
mod control;
#[cfg(target_os = "macos")]
mod keys;
#[cfg(target_os = "macos")]
mod picture;

#[cfg(target_os = "macos")]
fn main() {
    app::main()
}

#[cfg(not(target_os = "macos"))]
fn main() {
    eprintln!("natemu-viewer: runs on macOS only");
    std::process::exit(1);
}
