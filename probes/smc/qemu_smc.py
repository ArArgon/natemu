#!/usr/bin/env python3
"""
qemu_smc.py -- how often does a real x86-64 Linux guest actually invalidate translated code?

Doc 21 needs a MEASURED answer to "who writes code at run time in a Linux guest, and how
often".  QEMU TCG already implements exactly the mechanism doc 21 is designing (write-protect
the guest-physical page a TB was translated from; invalidate on a write), and it already keeps
the counter: `info jit`'s "TB invalidate count" is `tb_ctx.tb_phys_invalidate_count`, bumped
once per translation block destroyed by `tb_phys_invalidate()`.

This script boots the same Alpine 3.24.1 x86-64 guest that doc 15 measured, under the same
QEMU 11.0.3, and polls `info jit` over a QMP socket a few times a second for the whole run.
That turns the counter into a time series, which separates the boot-time storm from the
steady state -- the distinction doc 21 part (B) is about.

It reuses probes/qemu_baseline/work/ (fetched by that probe's build.sh); it does not download
anything and does not modify anything outside probes/smc/.

Usage:  ./qemu_smc.py --task none --label boot
        ./qemu_smc.py --all
"""
import argparse, json, os, re, socket, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.abspath(os.path.join(HERE, "..", "qemu_baseline", "work"))
RES  = os.path.join(HERE, "results")
QEMU = "/opt/homebrew/bin/qemu-system-x86_64"
LOGLEVEL = 3
EXTRA_APPEND = ""

FIELDS = [
    ("gen_code_size",   r"gen code size\s+(\d+)"),
    ("tb_count",        r"TB count\s+(\d+)"),
    ("tb_flush",        r"TB flush count\s+(\d+)"),
    ("tb_invalidate",   r"TB invalidate count\s+(\d+)"),
    ("tlb_full",        r"TLB full flushes\s+(\d+)"),
    ("tlb_partial",     r"TLB partial flushes\s+(\d+)"),
    ("tlb_elided",      r"TLB elided flushes\s+(\d+)"),
]


class Qmp:
    def __init__(self, path, timeout=60):
        t0 = time.time()
        while True:
            try:
                self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.s.connect(path)
                break
            except OSError:
                if time.time() - t0 > timeout:
                    raise
                time.sleep(0.05)
        self.f = self.s.makefile("rwb")
        self._read()                                  # greeting
        self.cmd("qmp_capabilities")

    def _read(self):
        while True:
            line = self.f.readline()
            if not line:
                raise EOFError
            m = json.loads(line)
            if "event" in m:
                continue
            return m

    def cmd(self, name, **args):
        self.f.write((json.dumps({"execute": name, "arguments": args}) + "\n").encode())
        self.f.flush()
        return self._read()

    def hmp(self, line):
        r = self.cmd("human-monitor-command", **{"command-line": line})
        return r.get("return", "")


def parse_jit(text):
    out = {}
    for key, pat in FIELDS:
        m = re.search(pat, text)
        out[key] = int(m.group(1)) if m else None
    return out


def run(task, label, smp, mem, cpu, timeout, poll):
    os.makedirs(RES, exist_ok=True)
    sock = os.path.join(tempfile.mkdtemp(), "qmp.sock")
    kern = os.path.join(WORK, "nb-x86_64", "boot", "vmlinuz-virt")
    initrd = os.path.join(WORK, "initramfs-x86_64.cpio")
    for p in (kern, initrd):
        if not os.path.exists(p):
            sys.exit(f"missing {p} -- run probes/qemu_baseline/build.sh first")
    cmd = [QEMU, "-machine", "q35", "-accel", "tcg,thread=single", "-cpu", cpu,
           "-m", str(mem), "-smp", str(smp),
           "-kernel", kern, "-initrd", initrd,
           "-append", f"console=ttyS0 natemu.task={task} panic=-1 rdinit=/init "
                     f"loglevel={LOGLEVEL} {EXTRA_APPEND}".strip(),
           "-display", "none", "-serial", "stdio", "-no-reboot", "-nodefaults",
           "-monitor", "none", "-qmp", f"unix:{sock},server,nowait"]

    t0 = time.monotonic()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    console = []

    def reader():
        for raw in p.stdout:
            console.append((time.monotonic() - t0, raw.decode("utf-8", "replace").rstrip()))
    th = threading.Thread(target=reader, daemon=True)
    th.start()

    series = []
    try:
        q = Qmp(sock)
    except Exception as e:
        p.kill()
        sys.exit(f"qmp connect failed: {e}")
    while p.poll() is None and time.monotonic() - t0 < timeout:
        try:
            rec = parse_jit(q.hmp("info jit"))
        except Exception:
            break
        rec["t"] = round(time.monotonic() - t0, 4)
        series.append(rec)
        time.sleep(poll)
    if p.poll() is None:
        p.kill()
    p.wait()
    th.join(timeout=2)

    marks = {}
    for t, line in console:
        for m in ("NATEMU_INIT_UP", "NATEMU_TASK_BEGIN", "NATEMU_TASK_END",
                  "BENCHDONE", "SEVENZIP_WALL", "BENCH spawn400"):
            if line.startswith(m) and m not in marks:
                marks[m] = t
    out = {"task": task, "label": label, "smp": smp, "cpu": cpu, "cmd": cmd,
           "wall": round(time.monotonic() - t0, 4), "marks": marks,
           "series": series, "console": [[round(t, 4), l] for t, l in console]}
    path = os.path.join(RES, f"jit.{label}.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=1)

    summarize(out)
    return out


def summarize(out):
    s = out["series"]
    if not s:
        print("  (no samples)")
        return
    last = s[-1]
    init_up = out["marks"].get("NATEMU_INIT_UP")
    begin = out["marks"].get("NATEMU_TASK_BEGIN")
    print(f"\n== {out['label']}  (task={out['task']}, {out['wall']:.2f} s wall, "
          f"{len(s)} samples)")
    print(f"   marks: " + ", ".join(f"{k}@{v:.2f}s" for k, v in out["marks"].items()))
    print(f"   {'t(s)':>7} {'TB count':>10} {'TB inval':>10} {'gen code':>11} "
          f"{'TLB part':>10} {'TLB elided':>11}")
    step = max(1, len(s) // 14)
    for r in s[::step] + [last]:
        print(f"   {r['t']:7.2f} {r['tb_count'] or 0:10d} {r['tb_invalidate'] or 0:10d} "
              f"{r['gen_code_size'] or 0:11d} {r['tlb_partial'] or 0:10d} "
              f"{r['tlb_elided'] or 0:11d}")
    # split boot vs task
    def at(t):
        best = s[0]
        for r in s:
            if r["t"] <= t:
                best = r
        return best
    if begin:
        b = at(begin)
        print(f"   -> during boot (0..{begin:.2f}s): {b['tb_invalidate']} TB invalidations, "
              f"{b['tb_count']} TBs live")
        dt = last["t"] - b["t"]
        di = (last["tb_invalidate"] or 0) - (b["tb_invalidate"] or 0)
        dc = (last["tb_count"] or 0) - (b["tb_count"] or 0)
        print(f"   -> during the task ({dt:.2f}s): {di} TB invalidations "
              f"({di/max(dt,1e-9):.1f}/s), TB count {dc:+d}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="none")
    ap.add_argument("--label", default=None)
    ap.add_argument("--smp", type=int, default=1)
    ap.add_argument("--mem", type=int, default=3072)
    ap.add_argument("--cpu", default="max")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--poll", type=float, default=0.20)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--loglevel", default="3")
    ap.add_argument("--extra-append", default="")
    a = ap.parse_args()
    global LOGLEVEL, EXTRA_APPEND
    LOGLEVEL = a.loglevel; EXTRA_APPEND = a.extra_append
    if a.all:
        for task, label, smp in [("none", "boot", 1), ("bench:alu4,ls4", "bench", 1),
                                 ("spawn", "spawn", 1), ("sevenzip", "sevenzip", 1),
                                 ("none", "boot.smp4", 4)]:
            run(task, label, smp, a.mem, a.cpu, a.timeout, a.poll)
    else:
        run(a.task, a.label or a.task.replace(":", "_"), a.smp, a.mem, a.cpu,
            a.timeout, a.poll)


if __name__ == "__main__":
    main()
