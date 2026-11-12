// libslirp (the user-mode network backend, virtio/slirp.rs): Homebrew's `libslirp`.
fn main() {
    let dir = std::process::Command::new("pkg-config")
        .args(["--variable=libdir", "slirp"])
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_else(|| "/opt/homebrew/opt/libslirp/lib".into());
    println!("cargo:rustc-link-search=native={dir}");
    println!("cargo:rustc-link-lib=dylib=slirp");
    println!("cargo:rerun-if-changed=build.rs");
}
