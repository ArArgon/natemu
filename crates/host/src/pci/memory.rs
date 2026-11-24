//! Mapped device memory (docs/restructure/mapped-device-memory-plan.md): a region of host memory a device shares with
//! the guest, which the VM maps at its own IPA (`vm::mapping_plan`) and the guest reads and writes natively. One POSIX
//! shared-memory object, so another process (a display front end) can map the same pages without a copy per frame.
use std::os::fd::{AsFd, AsRawFd, BorrowedFd, FromRawFd, OwnedFd};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};

/// The smallest region (the resident alias's block size) and the largest (the PCI memory window).
pub const MIN_BYTES: u64 = 2 << 20;
pub const MAX_BYTES: u64 = natemu_abi::execution::MAX_MAPPED_BYTES;
const _: () = assert!(
    MAX_BYTES == (super::platform::WINDOW.1 - super::platform::WINDOW.0) as u64
        && MIN_BYTES == natemu_abi::execution::cache::BLOCK as u64
);

/// A region of device memory: one shared-memory object of `len` bytes (a power of two from [`MIN_BYTES`] to
/// [`MAX_BYTES`]), mapped read-write at a 2 MiB-aligned address of this process. Its name is unlinked before [`new`]
/// returns, so only its descriptors name it and nothing of it outlives the process.
///
/// [`new`]: DeviceMemory::new
pub struct DeviceMemory {
    base: *mut u8,
    len: u64,
    /// A read-only descriptor of the object ([`DeviceMemory::fd`]).
    fd: OwnedFd,
    /// The name the object had until [`DeviceMemory::new`] unlinked it.
    #[cfg(test)]
    name: std::ffi::CString,
}
// SAFETY: the mapping is the object's own, unmapped only on drop; every access through it is a volatile word access.
unsafe impl Send for DeviceMemory {}
unsafe impl Sync for DeviceMemory {}

fn os_error(what: &str) -> String {
    format!("device memory: {what}: {}", std::io::Error::last_os_error())
}

impl DeviceMemory {
    /// A zeroed region of `len` bytes.
    pub fn new(len: u64) -> crate::Result<Arc<Self>> {
        if !len.is_power_of_two() || !(MIN_BYTES..=MAX_BYTES).contains(&len) {
            return Err(format!(
                "device memory of {len:#x} bytes: a power of two from {MIN_BYTES:#x} to {MAX_BYTES:#x}"
            ));
        }
        let name = Self::unique_name();
        // SAFETY: a NUL-terminated name; the mode is the variadic argument O_CREAT takes.
        let rw = unsafe {
            libc::shm_open(
                name.as_ptr(),
                libc::O_RDWR | libc::O_CREAT | libc::O_EXCL,
                0o600 as libc::c_uint,
            )
        };
        if rw < 0 {
            return Err(os_error("shm_open"));
        }
        // SAFETY: `rw` is a descriptor this call just opened.
        let rw = unsafe { OwnedFd::from_raw_fd(rw) };
        // The object's one size change (macOS allows no other), then the read-only descriptor, then the name goes.
        let opened = if unsafe { libc::ftruncate(rw.as_raw_fd(), len as libc::off_t) } != 0 {
            Err(os_error("ftruncate"))
        } else {
            match unsafe { libc::shm_open(name.as_ptr(), libc::O_RDONLY) } {
                fd if fd < 0 => Err(os_error("shm_open (read-only)")),
                // SAFETY: as `rw`.
                fd => Ok(unsafe { OwnedFd::from_raw_fd(fd) }),
            }
        };
        unsafe { libc::shm_unlink(name.as_ptr()) };
        let fd = opened?;
        let base = map_aligned(rw.as_fd(), len)?;
        Ok(Arc::new(Self {
            base,
            len,
            fd,
            #[cfg(test)]
            name,
        }))
    }
    /// `/natemu.<pid>.d<n>`: unique in this process (the fexc channels' names have no `d`), within macOS's 31 bytes.
    fn unique_name() -> std::ffi::CString {
        static COUNT: AtomicU64 = AtomicU64::new(0);
        let name = format!("/natemu.{}.d{}", std::process::id(), COUNT.fetch_add(1, Relaxed));
        assert!(name.len() <= 31);
        std::ffi::CString::new(name).unwrap()
    }
    pub fn len(&self) -> u64 {
        self.len
    }
    /// The read-write mapping's first byte (2 MiB-aligned): what the VM maps.
    pub fn as_ptr(&self) -> *mut u8 {
        self.base
    }
    /// The object's read-only descriptor, the one to hand to another process: a `PROT_WRITE` `MAP_SHARED` mapping of it
    /// fails (EPERM on macOS, where POSIX names EACCES), so no client can write device memory.
    pub fn fd(&self) -> BorrowedFd<'_> {
        self.fd.as_fd()
    }
    /// A second, read-only mapping of the object in this process (unmapped when the view drops).
    pub fn view(&self) -> crate::Result<View> {
        // SAFETY: a fresh mapping of a descriptor this value owns, of the object's whole length.
        let base = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                self.len as usize,
                libc::PROT_READ,
                libc::MAP_SHARED,
                self.fd.as_raw_fd(),
                0,
            )
        };
        if base == libc::MAP_FAILED {
            return Err(os_error("mmap (view)"));
        }
        Ok(View {
            base: base.cast(),
            len: self.len,
        })
    }
    /// Copies the words at byte `offset` (8-aligned) out, each one load of 8 bytes.
    pub fn read_words(&self, offset: u64, words: &mut [u64]) {
        read_words(self.base, self.len, offset, words)
    }
    /// Stores `words` at byte `offset` (8-aligned), each one store of 8 bytes: single-copy atomic per word, so a vCPU
    /// reading concurrently sees every word old or new, never torn.
    pub fn write_words(&self, offset: u64, words: &[u64]) {
        let at = word_range(self.len, offset, words.len());
        for (i, &w) in words.iter().enumerate() {
            // SAFETY: inside the mapping (`word_range`), 8-aligned (the base is 2 MiB-aligned).
            unsafe { self.base.cast::<u64>().add(at + i).write_volatile(w) };
        }
    }
}
impl Drop for DeviceMemory {
    fn drop(&mut self) {
        unsafe { libc::munmap(self.base.cast(), self.len as usize) };
    }
}

/// A read-only mapping of a [`DeviceMemory`] ([`DeviceMemory::view`]).
pub struct View {
    base: *mut u8,
    len: u64,
}
// SAFETY: as DeviceMemory's; the view is only read.
unsafe impl Send for View {}
unsafe impl Sync for View {}
impl View {
    /// As [`DeviceMemory::read_words`], through this mapping.
    pub fn read_words(&self, offset: u64, words: &mut [u64]) {
        read_words(self.base, self.len, offset, words)
    }
}
impl Drop for View {
    fn drop(&mut self) {
        unsafe { libc::munmap(self.base.cast(), self.len as usize) };
    }
}

/// The index of the word at byte `offset` of a mapping of `len` bytes, checked to start `n` words inside it.
fn word_range(len: u64, offset: u64, n: usize) -> usize {
    assert!(
        offset % 8 == 0 && offset.checked_add(8 * n as u64).is_some_and(|end| end <= len),
        "{n} words at {offset:#x} of {len:#x} bytes"
    );
    (offset / 8) as usize
}
fn read_words(base: *mut u8, len: u64, offset: u64, words: &mut [u64]) {
    let at = word_range(len, offset, words.len());
    for (i, w) in words.iter_mut().enumerate() {
        // SAFETY: as in `write_words`.
        *w = unsafe { base.cast::<u64>().add(at + i).read_volatile() };
    }
}

/// Maps `len` bytes of the object `fd` read-write and shared at a 2 MiB-aligned address: reserves twice the length,
/// maps into its aligned part and gives the rest back.
fn map_aligned(fd: BorrowedFd<'_>, len: u64) -> crate::Result<*mut u8> {
    let len = len as usize;
    // SAFETY: a fresh reservation, nothing in it yet.
    let reserved = unsafe {
        libc::mmap(std::ptr::null_mut(), 2 * len, libc::PROT_NONE, libc::MAP_PRIVATE | libc::MAP_ANON, -1, 0)
    };
    if reserved == libc::MAP_FAILED {
        return Err(os_error("mmap (reserve)"));
    }
    let start = reserved as usize;
    let aligned = start.next_multiple_of(MIN_BYTES as usize);
    // SAFETY: `[aligned, aligned + len)` lies inside the reservation (len >= 2 MiB, so 2 * len covers the alignment).
    let base = unsafe {
        libc::mmap(
            aligned as *mut libc::c_void,
            len,
            libc::PROT_READ | libc::PROT_WRITE,
            libc::MAP_SHARED | libc::MAP_FIXED,
            fd.as_raw_fd(),
            0,
        )
    };
    if base == libc::MAP_FAILED {
        let e = os_error("mmap");
        unsafe { libc::munmap(reserved, 2 * len) };
        return Err(e);
    }
    unsafe {
        if aligned > start {
            libc::munmap(reserved, aligned - start);
        }
        libc::munmap((aligned + len) as *mut libc::c_void, start + 2 * len - (aligned + len));
    }
    Ok(base.cast())
}

#[cfg(test)]
mod tests {
    use super::*;

    const CHILD: &str = "NATEMU_DEVICE_MEMORY_CHILD";
    /// The child's success status: a test that ran and passed (a filter that matched nothing would exit 0).
    const CHILD_PASSED: i32 = 42;

    #[test]
    fn device_memory_is_2mib_aligned_power_of_two() {
        for len in [MIN_BYTES, 4 << 20, MAX_BYTES] {
            let m = DeviceMemory::new(len).unwrap();
            assert_eq!((m.len(), m.as_ptr() as u64 % MIN_BYTES), (len, 0));
            let mut w = [1; 2];
            m.read_words(len - 16, &mut w);
            assert_eq!(w, [0, 0], "zeroed");
        }
        for len in [0, 1 << 20, (2 << 20) + 4096, 3 << 20, 2 * MAX_BYTES] {
            assert!(DeviceMemory::new(len).is_err(), "{len:#x}");
        }
    }

    /// A word written through the main mapping reads back through `view()`, and through a `PROT_READ` mapping of `fd()`
    /// in another process: this test binary, re-executed to run [`view_child`] alone, the descriptor at a fixed number.
    #[test]
    fn view_sees_writes() {
        let m = DeviceMemory::new(MIN_BYTES).unwrap();
        const AT: u64 = 0x1_2340;
        let word = 0x0123_4567_89ab_cdef;
        m.write_words(AT, &[word]);
        let mut w = [0];
        m.view().unwrap().read_words(AT, &mut w);
        assert_eq!(w[0], word);
        let raw = m.fd().as_raw_fd();
        let target = if raw == 3 { 4 } else { 3 };
        let exe = std::ffi::CString::new(std::env::current_exe().unwrap().into_os_string().into_encoded_bytes())
            .unwrap();
        let args: Vec<std::ffi::CString> = [
            exe.to_str().unwrap(),
            "--exact",
            "pci::memory::tests::view_child",
            "--ignored",
            "--test-threads=1",
            "-q",
        ]
        .iter()
        .map(|a| std::ffi::CString::new(*a).unwrap())
        .collect();
        let env: Vec<std::ffi::CString> = std::env::vars()
            .filter(|(k, _)| k != CHILD)
            .map(|(k, v)| format!("{k}={v}"))
            .chain([format!("{CHILD}={target},{AT},{word}")])
            .map(|e| std::ffi::CString::new(e).unwrap())
            .collect();
        let mut argv: Vec<*mut libc::c_char> = args.iter().map(|a| a.as_ptr().cast_mut()).collect();
        argv.push(std::ptr::null_mut());
        let mut envp: Vec<*mut libc::c_char> = env.iter().map(|e| e.as_ptr().cast_mut()).collect();
        envp.push(std::ptr::null_mut());
        let mut pid = 0;
        let status = unsafe {
            let mut actions: libc::posix_spawn_file_actions_t = std::ptr::null_mut();
            assert_eq!(libc::posix_spawn_file_actions_init(&mut actions), 0);
            assert_eq!(libc::posix_spawn_file_actions_adddup2(&mut actions, raw, target), 0);
            let spawned =
                libc::posix_spawn(&mut pid, exe.as_ptr(), &actions, std::ptr::null(), argv.as_ptr(), envp.as_ptr());
            libc::posix_spawn_file_actions_destroy(&mut actions);
            assert_eq!(spawned, 0, "posix_spawn");
            let mut status = 0;
            assert_eq!(libc::waitpid(pid, &mut status, 0), pid);
            status
        };
        assert!(
            libc::WIFEXITED(status) && libc::WEXITSTATUS(status) == CHILD_PASSED,
            "child status {status:#x}"
        );
    }
    /// [`view_sees_writes`]'s other process: maps the descriptor it was given read-only and checks the word, then exits
    /// with [`CHILD_PASSED`]. Without the environment flag (any ordinary run of ignored tests) it does nothing.
    #[test]
    #[ignore = "run by view_sees_writes in a child process"]
    fn view_child() {
        let Ok(spec) = std::env::var(CHILD) else { return };
        let [fd, at, word]: [u64; 3] = spec
            .split(',')
            .map(|s| s.parse().unwrap())
            .collect::<Vec<_>>()
            .try_into()
            .unwrap();
        let base = unsafe {
            libc::mmap(std::ptr::null_mut(), MIN_BYTES as usize, libc::PROT_READ, libc::MAP_SHARED, fd as i32, 0)
        };
        assert_ne!(base, libc::MAP_FAILED, "{}", std::io::Error::last_os_error());
        let mut w = [0];
        read_words(base.cast(), MIN_BYTES, at, &mut w);
        assert_eq!(w[0], word);
        std::process::exit(CHILD_PASSED);
    }

    /// No writable shared mapping of `fd()`: macOS refuses it with EPERM (POSIX's EACCES).
    #[test]
    fn fd_is_read_only() {
        let m = DeviceMemory::new(MIN_BYTES).unwrap();
        for prot in [libc::PROT_WRITE, libc::PROT_READ | libc::PROT_WRITE] {
            let p = unsafe {
                libc::mmap(std::ptr::null_mut(), MIN_BYTES as usize, prot, libc::MAP_SHARED, m.fd().as_raw_fd(), 0)
            };
            let e = std::io::Error::last_os_error();
            assert_eq!(p, libc::MAP_FAILED);
            assert_eq!(e.raw_os_error(), Some(libc::EPERM), "{e}");
        }
    }

    #[test]
    fn object_has_no_name() {
        let m = DeviceMemory::new(MIN_BYTES).unwrap();
        let name = &m.name;
        assert!(name.to_str().unwrap().starts_with(&format!("/natemu.{}.d", std::process::id())));
        let fd = unsafe { libc::shm_open(name.as_ptr(), libc::O_RDONLY) };
        let e = std::io::Error::last_os_error();
        assert!(fd < 0, "{name:?} still opens");
        assert_eq!(e.raw_os_error(), Some(libc::ENOENT), "{e}");
    }

    /// Host stores are single-copy atomic per word: a reader never sees a word that is neither pattern.
    #[test]
    fn host_writes_are_word_atomic() {
        let m = DeviceMemory::new(MIN_BYTES).unwrap();
        const WORDS: usize = 512;
        let (a, b) = ([0x5555_5555_5555_5555u64; WORDS], [0xaaaa_aaaa_aaaa_aaaau64; WORDS]);
        m.write_words(0, &a);
        let done = std::sync::atomic::AtomicBool::new(false);
        std::thread::scope(|t| {
            t.spawn(|| {
                for k in 0..20_000 {
                    m.write_words(0, if k % 2 == 0 { &b } else { &a });
                }
                done.store(true, Relaxed);
            });
            let mut w = [0; WORDS];
            while !done.load(Relaxed) {
                m.read_words(0, &mut w);
                assert!(w.iter().all(|&x| x == a[0] || x == b[0]), "a torn word");
            }
        });
    }
}
