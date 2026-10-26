//! One process group and durable cleanup journal per already-created request.
//! Drop never replaces the error that caused the owner to unwind.
use std::{
    fs::{self, File},
    io::{self, Write},
    os::unix::process::{CommandExt, ExitStatusExt},
    path::{Path, PathBuf},
    process::{Child, Command, ExitStatus, Stdio},
    sync::OnceLock,
    thread,
    time::{Duration, Instant},
};
unsafe extern "C" {
    fn kill(pid: i32, signal: i32) -> i32;
    fn waitid(kind: u32, id: u32, info: *mut DarwinSiginfo, options: i32) -> i32;
}

// Darwin sys/signal.h and sys/wait.h: WEXITED|WNOHANG|WNOWAIT.
#[repr(C)]
struct DarwinSiginfo {
    signo: i32,
    errno: i32,
    code: i32,
    pid: i32,
    uid: u32,
    status: i32,
    remainder: [u64; 10],
}
// One VM per host process; a fresh-control VM must use a fresh process.
static CANCEL_DEADLINE: OnceLock<Instant> = OnceLock::new();
/// Call at the first VM cancellation, before joining any owner threads.
pub(crate) fn begin_cancellation() {
    CANCEL_DEADLINE.get_or_init(|| Instant::now() + Duration::from_secs(6));
}

pub(crate) struct OwnedChild {
    child: Child,
    registry: PathBuf,
    helper: PathBuf,
    token: String,
    cleaned: bool,
    observed_exit: bool,
    #[cfg(test)]
    scratch: Option<PathBuf>,
}
fn atomic(path: &Path, value: &str) -> io::Result<()> {
    let temp = path.with_extension("pending");
    let mut file = File::create_new(&temp)?;
    file.write_all(value.as_bytes())?;
    file.sync_all()?;
    fs::rename(temp, path)
}
impl OwnedChild {
    pub(crate) fn spawn(worker: &Path, producer: &Path, directory: &Path) -> crate::Result<Self> {
        Self::spawn_injected(worker, producer, directory, false, false)
    }
    pub(crate) fn spawn_held(
        worker: &Path,
        producer: &Path,
        directory: &Path,
        hold: bool,
    ) -> crate::Result<Self> {
        Self::spawn_injected(worker, producer, directory, hold, false)
    }
    pub(crate) fn spawn_injected(
        worker: &Path,
        producer: &Path,
        directory: &Path,
        hold_payload: bool,
        hold_container: bool,
    ) -> crate::Result<Self> {
        if CANCEL_DEADLINE.get().is_some() {
            return Err("VM cancelled before worker launch".into());
        }
        let directory = directory.canonicalize().map_err(|e| e.to_string())?;
        let worker = worker.canonicalize().map_err(|e| e.to_string())?;
        let helper = worker
            .parent()
            .ok_or("worker parent")?
            .join("i4v_producer/ownership.py");
        let registry = directory.join(".ownership");
        fs::create_dir(&registry).map_err(|e| e.to_string())?;
        let mut random = [0u8; 16];
        if unsafe { crate::getentropy(random.as_mut_ptr().cast(), random.len()) } != 0 {
            return Err("unable to allocate worker ownership identity".into());
        }
        let token: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
        atomic(
            &registry.join("job.json"),
            &format!(
                "{{\"schema\":\"natemu-owned-worker-v1\",\"token\":\"{token}\",\"host_pid\":{}}}\n",
                std::process::id()
            ),
        )
        .map_err(|e| e.to_string())?;
        if hold_payload {
            File::create_new(registry.join("hold-payload")).map_err(|e| e.to_string())?;
        }
        if hold_container {
            File::create_new(registry.join("hold-container")).map_err(|e| e.to_string())?;
        }
        let launched = Command::new("python3")
            .arg("-B")
            .arg(&helper)
            .arg("worker")
            .arg("--registry")
            .arg(&registry)
            .arg("--")
            .arg("python3")
            .arg("-B")
            .arg(worker)
            .arg("--request-dir")
            .arg(&directory)
            .arg("--producer")
            .arg(producer)
            .process_group(0)
            .stdin(Stdio::null())
            .stdout(File::create_new(directory.join("worker.stdout")).map_err(|e| e.to_string())?)
            .stderr(File::create_new(directory.join("worker.stderr")).map_err(|e| e.to_string())?)
            .spawn();
        match launched {
            Ok(child) => Ok(Self {
                child,
                registry,
                helper,
                token,
                cleaned: false,
                observed_exit: false,
                #[cfg(test)]
                scratch: None,
            }),
            Err(error) => {
                let _ = atomic(
                    &registry.join("process.json"),
                    &format!(
                        "{{\"schema\":\"natemu-owned-worker-v1\",\"token\":\"{token}\",\"spawned\":false,\"group_gone\":true,\"reaped\":false}}\n"
                    ),
                );
                Err(error.to_string())
            }
        }
    }
    /// Startup acknowledgement precedes the DEFER response for an injected
    /// worker. This waits outside native authority and never trusts journal PIDs.
    pub(crate) fn wait_registered(&mut self) -> crate::Result<()> {
        self.wait_registered_until(Instant::now() + Duration::from_secs(5))
    }
    fn wait_registered_until(&mut self, deadline: Instant) -> crate::Result<()> {
        let expected: Vec<u8> = (0..16)
            .map(|i| u8::from_str_radix(&self.token[i * 2..i * 2 + 2], 16).unwrap())
            .collect();
        loop {
            if CANCEL_DEADLINE.get().is_some() {
                return Err("VM cancelled before worker registration".into());
            }
            if self.cleaned || self.try_wait().map_err(|e| e.to_string())?.is_some() {
                return Err("owned worker exited before registration acknowledgement".into());
            }
            match fs::read(self.registry.join("worker-ready.bin")) {
                Ok(bytes) => {
                    if bytes.len() != 32
                        || &bytes[..8] != b"NATWRK01"
                        || bytes[8..24] != expected
                        || u64::from_le_bytes(bytes[24..32].try_into().unwrap())
                            != self.child.id() as u64
                    {
                        return Err("invalid owned worker registration acknowledgement".into());
                    }
                    return Ok(());
                }
                Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                Err(e) => return Err(e.to_string()),
            }
            if Instant::now() >= deadline {
                return Err("owned worker registration deadline exceeded".into());
            }
            thread::sleep(Duration::from_millis(10));
        }
    }
    /// A live owned worker has acknowledged a real, stopped container. The
    /// marker is atomically published after Docker create, before start.
    pub(crate) fn container_ready(&mut self) -> crate::Result<bool> {
        if self.cleaned || self.try_wait().map_err(|e| e.to_string())?.is_some() {
            return Err("owned worker completed before container hold".into());
        }
        let bytes = match fs::read(self.registry.join("container-ready.bin")) {
            Ok(bytes) => bytes,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(false),
            Err(e) => return Err(e.to_string()),
        };
        let expected: Vec<u8> = (0..16)
            .map(|i| u8::from_str_radix(&self.token[i * 2..i * 2 + 2], 16).unwrap())
            .collect();
        if bytes.len() != 64
            || &bytes[..8] != b"NATCTR01"
            || bytes[8..24] != expected
            || bytes[56..] != [0; 8]
            || !self.registry.join("hold-container").exists()
        {
            return Err("invalid owned container creation acknowledgement".into());
        }
        Ok(true)
    }
    /// The live Child lease, never a PID read from a journal, authorizes this
    /// injected signal. The group remains owned and unreaped for normal cleanup.
    pub(crate) fn inject_exit(&mut self) -> crate::Result<()> {
        if self.cleaned || self.try_wait().map_err(|e| e.to_string())?.is_some() {
            return Err("owned worker is no longer running at injection".into());
        }
        let pid = self.child.id();
        atomic(&self.registry.join("injection.json"),&format!(
            "{{\"kind\":\"worker-exit\",\"token\":\"{}\",\"pid\":{pid},\"pgid\":{pid},\"signal\":9}}\n",self.token))
            .map_err(|e|e.to_string())?;
        self.child.kill().map_err(|e| e.to_string())
    }
    #[cfg(test)]
    pub(crate) fn inert_for_test() -> Self {
        Self::with_source_for_test("pass\n")
    }
    #[cfg(test)]
    fn with_source_for_test(source: &str) -> Self {
        Self::with_source_hold_for_test(source, false)
    }
    #[cfg(test)]
    fn with_source_hold_for_test(source: &str, hold: bool) -> Self {
        let mut random = [0u8; 16];
        assert_eq!(
            unsafe { crate::getentropy(random.as_mut_ptr().cast(), 16) },
            0
        );
        let name: String = random.iter().map(|b| format!("{b:02x}")).collect();
        let root = std::env::temp_dir().join(format!("natemu-owned-test-{name}"));
        fs::create_dir(&root).unwrap();
        fs::create_dir(root.join("i4v_producer")).unwrap();
        fs::create_dir(root.join("request")).unwrap();
        fs::write(root.join("worker.py"), source).unwrap();
        fs::copy(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tools/i4v_producer/ownership.py"),
            root.join("i4v_producer/ownership.py"),
        )
        .unwrap();
        let mut child = Self::spawn_held(
            &root.join("worker.py"),
            Path::new("unused"),
            &root.join("request"),
            hold,
        )
        .unwrap();
        child.scratch = Some(root);
        child
    }
    pub(crate) fn try_wait(&mut self) -> io::Result<Option<ExitStatus>> {
        match fs::read(self.registry.join("result.bin")) {
            Ok(bytes) if bytes.len() == 4 => Ok(Some(ExitStatus::from_raw(i32::from_le_bytes(
                bytes.try_into().unwrap(),
            )))),
            Ok(_) => Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "invalid owned worker completion",
            )),
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                let mut info = DarwinSiginfo {
                    signo: 0,
                    errno: 0,
                    code: 0,
                    pid: 0,
                    uid: 0,
                    status: 0,
                    remainder: [0; 10],
                };
                if unsafe { waitid(1, self.child.id(), &mut info, 0x4 | 0x1 | 0x20) } != 0 {
                    return Err(io::Error::last_os_error());
                }
                if info.pid == 0 {
                    return Ok(None);
                }
                if info.pid != self.child.id() as i32 || !matches!(info.code, 1..=3) {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "unexpected owned-child exit observation",
                    ));
                }
                let raw = if info.code == 1 {
                    (info.status & 255) << 8
                } else {
                    info.status | if info.code == 3 { 128 } else { 0 }
                };
                if !self.observed_exit {
                    self.observed_exit = true;
                    let _ = atomic(
                        &self.registry.join("worker-exit.json"),
                        &format!(
                            "{{\"schema\":\"natemu-owned-worker-v1\",\"token\":\"{}\",\"pid\":{},\"code\":{},\"status\":{},\"raw_status\":{raw},\"reaped\":false}}\n",
                            self.token, info.pid, info.code, info.status
                        ),
                    );
                }
                Ok(Some(ExitStatus::from_raw(raw)))
            }
            Err(e) => Err(e),
        }
    }
    pub(crate) fn wait(&mut self) -> io::Result<ExitStatus> {
        let until = Instant::now() + Duration::from_secs(120);
        loop {
            if CANCEL_DEADLINE.get().is_some() {
                return Err(io::Error::new(
                    io::ErrorKind::Interrupted,
                    "VM cancelled while awaiting owned worker",
                ));
            }
            if let Some(status) = self.try_wait()? {
                return Ok(status);
            }
            if Instant::now() >= until {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "owned worker completion deadline",
                ));
            }
            thread::sleep(Duration::from_millis(10));
        }
    }
    pub(crate) fn cleanup(&mut self) -> crate::Result<()> {
        if self.cleaned {
            return Ok(());
        }
        self.cleaned = true;
        let _ = File::create_new(self.registry.join("cancelled"));
        let pgid = self.child.id() as i32;
        // Signal while Child still owns the leader; grandchildren inherit this
        // group, including Docker clients. Container daemon work is separate.
        let signalled = unsafe { kill(-pgid, 9) } == 0;
        let deadline = CANCEL_DEADLINE
            .get()
            .copied()
            .unwrap_or_else(|| Instant::now() + Duration::from_secs(3));
        let until = (Instant::now() + Duration::from_secs(1)).min(deadline);
        let reaped = loop {
            match self.child.try_wait() {
                Ok(Some(_)) => break true,
                Ok(None) if Instant::now() < until => thread::sleep(Duration::from_millis(10)),
                _ => break false,
            }
        };
        let group_gone = loop {
            if unsafe { kill(-pgid, 0) } != 0 {
                break io::Error::last_os_error().raw_os_error() == Some(3);
            }
            if Instant::now() >= until {
                break false;
            }
            thread::sleep(Duration::from_millis(10));
        };
        atomic(&self.registry.join("process.json"),&format!(
            "{{\"schema\":\"natemu-owned-worker-v1\",\"token\":\"{}\",\"pid\":{pgid},\"pgid\":{pgid},\"signalled\":{signalled},\"group_gone\":{group_gone},\"reaped\":{reaped}}}\n",self.token)).map_err(|e|e.to_string())?;
        if Instant::now() >= deadline {
            return Err("owned cleanup deadline exhausted".into());
        }
        let remaining = (deadline - Instant::now()).as_secs_f64().min(2.0);
        let mut cleanup = Command::new("python3")
            .arg("-B")
            .arg(&self.helper)
            .arg("cleanup")
            .arg("--budget")
            .arg(format!("{remaining:.6}"))
            .arg("--registry")
            .arg(&self.registry)
            .process_group(0)
            .stdin(Stdio::null())
            .stdout(
                File::create_new(self.registry.join("cleanup.stdout"))
                    .map_err(|e| e.to_string())?,
            )
            .stderr(
                File::create_new(self.registry.join("cleanup.stderr"))
                    .map_err(|e| e.to_string())?,
            )
            .spawn()
            .map_err(|e| e.to_string())?;
        let work_deadline = deadline.min(Instant::now() + Duration::from_secs_f64(remaining));
        let success = loop {
            match cleanup.try_wait() {
                Ok(Some(status)) => break status.success(),
                Ok(None) if Instant::now() < work_deadline => {
                    thread::sleep(Duration::from_millis(10))
                }
                _ => {
                    let helper_pid = cleanup.id();
                    let signalled = unsafe { kill(-(helper_pid as i32), 9) } == 0;
                    // SIGKILL is not a bounded wait primitive. Preserve an
                    // explicit incomplete reap if the shared budget expires.
                    let reaped = loop {
                        match cleanup.try_wait() {
                            Ok(Some(_)) => break true,
                            Ok(None) if Instant::now() < deadline => {
                                thread::sleep(Duration::from_millis(5))
                            }
                            _ => break false,
                        }
                    };
                    let _ = atomic(
                        &self.registry.join("cleanup-helper.json"),
                        &format!(
                            "{{\"schema\":\"natemu-owned-worker-v1\",\"token\":\"{}\",\"pid\":{helper_pid},\"pgid\":{helper_pid},\"signalled\":{signalled},\"reaped\":{reaped},\"timed_out\":true}}\n",
                            self.token
                        ),
                    );
                    break false;
                }
            }
        };
        if reaped && group_gone && success {
            Ok(())
        } else {
            Err(format!(
                "owned worker cleanup incomplete: {}",
                self.registry.display()
            ))
        }
    }
}
impl Drop for OwnedChild {
    fn drop(&mut self) {
        let _ = self.cleanup();
        #[cfg(test)]
        if let Some(path) = &self.scratch {
            let _ = fs::remove_dir_all(path);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn abrupt_wrapper_exit_is_observed_without_reaping_group_lease() {
        let mut child = OwnedChild::with_source_for_test("import os;os._exit(23)\n");
        let until = Instant::now() + Duration::from_secs(2);
        let status = loop {
            if let Some(status) = child.try_wait().unwrap() {
                break status;
            }
            assert!(
                Instant::now() < until,
                "abrupt exit was treated as pending work"
            );
            thread::sleep(Duration::from_millis(10));
        };
        assert_eq!(status.code(), Some(23));
        assert!(!child.registry.join("result.bin").exists());
        assert!(child.registry.join("worker-exit.json").exists());
        // WNOWAIT leaves the same real exit available for the owning reap.
        assert_eq!(child.try_wait().unwrap().unwrap().code(), Some(23));
        child.cleanup().unwrap();
    }
    #[test]
    fn killed_wrapper_reports_its_signal_before_cleanup() {
        let mut child = OwnedChild::with_source_for_test("import time;time.sleep(60)\n");
        assert_eq!(unsafe { kill(child.child.id() as i32, 9) }, 0);
        let until = Instant::now() + Duration::from_secs(2);
        loop {
            if let Some(status) = child.try_wait().unwrap() {
                assert_eq!(status.signal(), Some(9));
                break;
            }
            assert!(Instant::now() < until);
            thread::sleep(Duration::from_millis(10));
        }
        child.cleanup().unwrap();
    }
    #[test]
    fn injected_exit_uses_held_live_child_and_preserves_reap_lease() {
        let mut child = OwnedChild::with_source_hold_for_test(
            "raise AssertionError('payload must remain held')\n",
            true,
        );
        assert!(child.registry.join("hold-payload").exists());
        child.inject_exit().unwrap();
        let until = Instant::now() + Duration::from_secs(2);
        loop {
            if let Some(status) = child.try_wait().unwrap() {
                assert_eq!(status.signal(), Some(9));
                break;
            }
            assert!(Instant::now() < until);
            thread::sleep(Duration::from_millis(10));
        }
        let injection = fs::read_to_string(child.registry.join("injection.json")).unwrap();
        assert!(injection.contains(&format!("\"pid\":{}", child.child.id())));
        assert!(injection.contains(&child.token));
        assert!(!child.registry.join("result.bin").exists());
        child.cleanup().unwrap();
    }
    #[test]
    fn completion_is_not_mislabelled_as_injected_worker_exit() {
        let mut child = OwnedChild::inert_for_test();
        assert!(child.wait().unwrap().success());
        assert!(child.inject_exit().is_err());
        assert!(!child.registry.join("injection.json").exists());
        child.cleanup().unwrap();
    }
    #[test]
    fn held_worker_registration_precedes_failure_injection() {
        let mut child = OwnedChild::with_source_hold_for_test("pass\n", true);
        child.wait_registered().unwrap();
        assert!(child.registry.join("worker.json").exists());
        assert!(!child.registry.join("result.bin").exists());
        child.inject_exit().unwrap();
        child.cleanup().unwrap();
    }
    #[test]
    fn worker_registration_rejects_missing_identity_and_abrupt_exit() {
        let mut child = OwnedChild::with_source_hold_for_test("pass\n", true);
        child.wait_registered().unwrap();
        fs::remove_file(child.registry.join("worker-ready.bin")).unwrap();
        let start = Instant::now();
        assert!(
            child
                .wait_registered_until(start + Duration::from_millis(20))
                .is_err()
        );
        assert!(start.elapsed() < Duration::from_secs(1));
        child.inject_exit().unwrap();
        assert!(child.wait_registered().is_err());
        child.cleanup().unwrap();
    }
    #[test]
    fn worker_registration_rejects_foreign_token_and_pid() {
        let mut child = OwnedChild::with_source_hold_for_test("pass\n", true);
        child.wait_registered().unwrap();
        let path = child.registry.join("worker-ready.bin");
        let mut marker = fs::read(&path).unwrap();
        marker[8] ^= 1;
        fs::write(&path, &marker).unwrap();
        assert!(child.wait_registered().is_err());
        marker[8] ^= 1;
        marker[24] ^= 1;
        fs::write(&path, &marker).unwrap();
        assert!(child.wait_registered().is_err());
        child.cleanup().unwrap();
    }
    #[test]
    fn container_acknowledgement_binds_live_job_and_atomic_marker() {
        let mut child = OwnedChild::with_source_hold_for_test("pass\n", true);
        assert!(!child.container_ready().unwrap());
        fs::write(child.registry.join("hold-container"), b"").unwrap();
        let mut marker = b"NATCTR01".to_vec();
        marker.extend(
            (0..16).map(|i| u8::from_str_radix(&child.token[i * 2..i * 2 + 2], 16).unwrap()),
        );
        marker.extend([0xaa; 32]);
        marker.extend([0; 8]);
        let path = child.registry.join("container-ready.bin");
        fs::write(&path, &marker).unwrap();
        assert!(child.container_ready().unwrap());
        marker[8] ^= 1;
        fs::write(&path, &marker).unwrap();
        assert!(child.container_ready().is_err());
        marker[8] ^= 1;
        marker[63] = 1;
        fs::write(&path, &marker).unwrap();
        assert!(child.container_ready().is_err());
        marker[63] = 0;
        fs::write(&path, &marker[..63]).unwrap();
        assert!(child.container_ready().is_err());
        fs::write(&path, &marker).unwrap();
        fs::remove_file(child.registry.join("hold-container")).unwrap();
        assert!(child.container_ready().is_err());
        child.cleanup().unwrap();
    }
    #[test]
    fn stalled_cleanup_helper_is_killed_and_reaped_with_a_bound() {
        let mut child = OwnedChild::inert_for_test();
        assert!(child.wait().unwrap().success());
        fs::write(&child.helper, "import time;time.sleep(60)\n").unwrap();
        let start = Instant::now();
        assert!(child.cleanup().is_err());
        assert!(start.elapsed() < Duration::from_secs(4));
        let result = fs::read_to_string(child.registry.join("cleanup-helper.json")).unwrap();
        assert!(result.contains("\"timed_out\":true"));
        assert!(result.contains("\"reaped\":true"));
    }
}
