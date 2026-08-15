#!/usr/bin/env python3
"""
runrates.py -- launch the guest_rates x86_64 QEMU guest, host-timestamp every
serial line, and parse the guest's GRSNAP counters into a JSON result.

Deliberately close to probes/qemu_baseline/runvm.py (which it does not modify):
same kernel, same accel settings, different initramfs and different parsing.

  runrates.py --task spawn2000 --out results/spawn.1.json
  runrates.py --task mem24 --plugin ./bin/plugin_rates.so --out results/mem.p1.json
  runrates.py --task none --monitor-jit --out results/boot.jit.1.json
"""
import argparse, json, os, re, socket, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "work")
BASE = os.path.join(HERE, "..", "qemu_baseline")
QEMU = "/opt/homebrew/bin/qemu-system-x86_64"

SNAP_RE = re.compile(r"^GRSNAP (\S+) (.*)$")


def build_cmd(a):
    kern = os.path.join(BASE, "work", "nb-x86_64", "boot", "vmlinuz-virt")
    initrd = os.path.join(WORK, "initramfs-x86_64.cpio")
    append = (f"console=ttyS0 natemu.task={a.task} panic=-1 rdinit=/init "
              f"loglevel={a.loglevel}").strip()
    cmd = [QEMU, "-machine", "q35", "-accel", f"tcg,thread={a.tcg_thread}",
           "-cpu", a.cpu, "-m", str(a.mem), "-smp", str(a.smp),
           "-kernel", kern, "-initrd", initrd, "-append", append,
           "-display", "none", "-serial", "stdio", "-no-reboot", "-nodefaults"]
    if a.plugin:
        cmd += ["-plugin", a.plugin, "-d", "plugin", "-D", a.plugin_out]
    if a.monitor_jit:
        cmd += ["-monitor", f"unix:{a.monitor_jit},server,nowait"]
    else:
        cmd += ["-monitor", "none"]
    return cmd


def jit_poller(sock_path, stop, out):
    """Poll `info jit` every interval until the run ends; keep the last reply."""
    while not stop.is_set():
        stop.wait(2.0)
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(sock_path)
            time.sleep(0.3)
            s.recv(65536)
            s.sendall(b"info jit\n")
            time.sleep(1.2)
            buf = b""
            try:
                while True:
                    s.settimeout(1.0)
                    d = s.recv(65536)
                    if not d:
                        break
                    buf += d
            except socket.timeout:
                pass
            if b"gen code size" in buf:
                out.append((time.monotonic(), buf.decode("utf-8", "replace")))
            s.close()
        except Exception:
            pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--task", default="none")
    p.add_argument("--smp", type=int, default=1)
    p.add_argument("--mem", type=int, default=3072)
    p.add_argument("--cpu", default="max")
    p.add_argument("--tcg-thread", default="single")
    p.add_argument("--loglevel", default="3")
    p.add_argument("--plugin", default=None)
    p.add_argument("--plugin-out", default=None)
    p.add_argument("--monitor-jit", default=None, help="unix socket path")
    p.add_argument("--timeout", type=float, default=5400)
    p.add_argument("--label", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args()
    if a.plugin and not a.plugin_out:
        a.plugin_out = (a.out or "/tmp/plugin") + ".plugin.txt"
    if a.monitor_jit:
        try:
            os.unlink(a.monitor_jit)
        except OSError:
            pass

    cmd = build_cmd(a)
    label = a.label or f"{a.task}-smp{a.smp}"

    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            bufsize=1, universal_newlines=True, errors="replace")
    jits, stop = [], threading.Event()
    th = None
    if a.monitor_jit:
        th = threading.Thread(target=jit_poller, args=(a.monitor_jit, stop, jits),
                              daemon=True)
        th.start()

    lines, events, snaps, ftrace = [], {}, {}, []
    in_ft = False
    try:
        for line in proc.stdout:
            t = time.monotonic() - t0
            line = line.rstrip("\r\n")
            lines.append((round(t, 6), line))
            if not a.quiet:
                print(f"[{t:9.3f}] {line}", flush=True)
            m = SNAP_RE.match(line)
            if m:
                tag, rest = m.group(1), m.group(2)
                d = {}
                for kv in rest.split():
                    k, _, v = kv.partition("=")
                    try:
                        d[k] = float(v) if "." in v else int(v)
                    except ValueError:
                        d[k] = v
                d["host_t"] = round(t, 6)
                snaps[tag] = d
            elif line.startswith("GRFTRACE_BEGIN"):
                in_ft = True
            elif line.startswith("GRFTRACE_END"):
                in_ft = False
            elif in_ft:
                ftrace.append(line)
            else:
                for mk in ("NATEMU_INIT_UP", "NATEMU_TASK_BEGIN",
                           "NATEMU_TASK_END", "GRFTRACE_AVAILABLE"):
                    if line.startswith(mk):
                        events.setdefault(mk, round(t, 6))
            if time.monotonic() - t0 > a.timeout:
                raise TimeoutError
    except TimeoutError:
        proc.kill()
        print("TIMEOUT", file=sys.stderr)
    finally:
        stop.set()
        if th:
            th.join(timeout=8)
        try:
            proc.wait(timeout=40)
        except subprocess.TimeoutExpired:
            proc.kill()

    delta = {}
    if "before" in snaps and "after" in snaps:
        b, aa = snaps["before"], snaps["after"]
        for k in b:
            if isinstance(b[k], (int, float)) and k in aa:
                delta[k] = aa[k] - b[k]
        delta["host_s"] = round(aa["host_t"] - b["host_t"], 6)
        delta["guest_s"] = round(aa["uptime"] - b["uptime"], 6)

    plug = None
    if a.plugin and os.path.exists(a.plugin_out):
        plug = open(a.plugin_out, errors="replace").read()

    res = {
        "label": label, "task": a.task, "cmd": cmd,
        "host_total_s": round(time.monotonic() - t0, 6),
        "events": events, "snaps": snaps, "delta": delta,
        "ftrace": ftrace,
        "jit": jits[-1][1] if jits else None,
        "jit_samples": len(jits),
        "plugin": plug,
        "returncode": proc.returncode,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w") as f:
            json.dump({**res, "lines": lines}, f, indent=1)
    print(json.dumps({k: v for k, v in res.items() if k != "lines"}, indent=1))


if __name__ == "__main__":
    main()
