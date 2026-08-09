#!/usr/bin/env python3
"""
runvm.py -- launch a QEMU guest for the natemu baseline measurement and
timestamp every serial-console line with the HOST monotonic clock.

Why host-side timestamps: guest-side clock_gettime() under TCG is derived from
the host clock via an emulated TSC, so it *should* be wall-accurate, but that is
exactly the sort of thing that quietly is not.  We record both and cross-check.

Usage:
  runvm.py --arch x86_64 --accel tcg --task bench --smp 1 --mem 2048
  runvm.py --arch aarch64 --accel hvf --task bench
  runvm.py --arch x86_64 --accel tcg --task none        # boot-time only
"""
import argparse, json, os, re, shutil, signal, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "work")

QEMU = {
    "x86_64":  shutil.which("qemu-system-x86_64")  or "/opt/homebrew/bin/qemu-system-x86_64",
    "aarch64": shutil.which("qemu-system-aarch64") or "/opt/homebrew/bin/qemu-system-aarch64",
}


def build_cmd(a):
    kern = os.path.join(WORK, f"nb-{a.arch}", "boot", "vmlinuz-virt")
    initrd = os.path.join(WORK, f"initramfs-{a.arch}.cpio")
    console = "ttyS0" if a.arch == "x86_64" else "ttyAMA0"
    append = (f"console={console} natemu.task={a.task} "
              f"panic=-1 rdinit=/init loglevel={a.loglevel} {a.extra_append}").strip()

    accel = a.accel
    if a.accel == "tcg":
        accel = f"tcg,thread={a.tcg_thread}"
        if a.tb_size:
            accel += f",tb-size={a.tb_size}"

    cmd = [QEMU[a.arch], "-accel", accel, "-m", str(a.mem), "-smp", str(a.smp),
           "-kernel", kern, "-initrd", initrd, "-append", append,
           "-display", "none", "-serial", "stdio", "-no-reboot",
           "-nodefaults", "-monitor", "none"]
    if a.arch == "x86_64":
        cmd[1:1] = ["-machine", a.machine or "q35"]
        cmd += ["-cpu", a.cpu or "max"]
    else:
        cmd[1:1] = ["-machine", (a.machine or "virt") + ",gic-version=3"]
        cmd += ["-cpu", a.cpu or ("host" if a.accel == "hvf" else "max")]
    if a.qemu_debug:
        cmd += ["-d", a.qemu_debug, "-D", a.qemu_debug_file or "/dev/stderr"]
    cmd += a.qemu_extra
    return cmd


MARKERS = ("NATEMU_INIT_UP", "NATEMU_SHELL_UP", "NATEMU_TASK_BEGIN",
           "NATEMU_TASK_END", "BENCHVER", "BENCH ", "BENCHDONE")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--arch", default="x86_64", choices=["x86_64", "aarch64"])
    p.add_argument("--accel", default="tcg", choices=["tcg", "hvf"])
    p.add_argument("--task", default="bench")
    p.add_argument("--smp", type=int, default=1)
    p.add_argument("--mem", type=int, default=3072)
    p.add_argument("--cpu", default=None)
    p.add_argument("--machine", default=None)
    p.add_argument("--tcg-thread", default="single", choices=["single", "multi"])
    p.add_argument("--tb-size", type=int, default=0)
    p.add_argument("--loglevel", default="3")
    p.add_argument("--extra-append", default="")
    p.add_argument("--qemu-debug", default=None)
    p.add_argument("--qemu-debug-file", default=None)
    p.add_argument("--timeout", type=float, default=1800)
    p.add_argument("--label", default=None)
    p.add_argument("--out", default=None, help="write JSON result here")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("qemu_extra", nargs="*", default=[])
    a = p.parse_args()

    cmd = build_cmd(a)
    label = a.label or f"{a.arch}-{a.accel}-{a.task}-smp{a.smp}-{a.tcg_thread}"

    t_launch = time.monotonic()
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            bufsize=1, universal_newlines=True, errors="replace")
    lines = []
    events = {}
    benches = []
    try:
        for line in proc.stdout:
            t = time.monotonic() - t_launch
            line = line.rstrip("\r\n")
            lines.append((round(t, 6), line))
            if not a.quiet:
                print(f"[{t:9.3f}] {line}", flush=True)
            for m in MARKERS:
                if line.startswith(m):
                    events.setdefault(m.strip(), round(t, 6))
            if line.startswith("BENCH ") and " s " in line:
                # BENCH alu     0.1900 s   631.582 Mops  chk=...
                f = line.split()
                try:
                    benches.append({"name": f[1], "guest_s": float(f[2]),
                                    "rate": float(f[4]), "unit": f[5],
                                    "host_t_end": round(t, 6)})
                except (IndexError, ValueError):
                    pass
            if time.monotonic() - t_launch > a.timeout:
                raise TimeoutError
    except TimeoutError:
        proc.kill()
        print("TIMEOUT", file=sys.stderr)
    finally:
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    t_exit = time.monotonic() - t_launch

    # derive per-bench host-side durations from consecutive marker timestamps
    prev = events.get("NATEMU_TASK_BEGIN", 0.0)
    for b in benches:
        b["host_s"] = round(b["host_t_end"] - prev, 6)
        prev = b["host_t_end"]

    res = {
        "label": label,
        "cmd": cmd,
        "host_total_s": round(t_exit, 6),
        "events": events,
        "benches": benches,
        "returncode": proc.returncode,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if a.out:
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        with open(a.out, "w") as f:
            json.dump({**res, "lines": lines}, f, indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
