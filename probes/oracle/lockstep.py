#!/usr/bin/env python3
"""
lockstep.py -- two full-system x86-64 emulators, single-stepped in lockstep
through a real Linux boot, with their architectural state diffed every
instruction.

This is the prototype of the "QEMU TCG as the reference oracle" claim in
docs/research/25-correctness-oracle.md.  Today both sides are QEMU (that is the
only x86-64 system emulator on this host); the point is to measure the
*mechanism* -- steps/second, bytes/step, and what a divergence report looks
like -- so the cost of pointing side B at natemu later is a measured number and
not a guess.

  ./lockstep.py --steps 20000
  ./lockstep.py --steps 20000 --b-args -cpu,Nehalem     # deliberate divergence
  ./lockstep.py --steps 5000 --inject-divergence 3000   # synthetic divergence

Reuses probes/qemu_baseline/work/ for the kernel + initramfs.  It does not
write anything there.
"""
import argparse, os, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rsp import RSP, decode_core, diff_core, eflags_str, CORE_HEXLEN

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.abspath(os.path.join(HERE, "..", "qemu_baseline", "work"))
QEMU = "/opt/homebrew/bin/qemu-system-x86_64"


def launch(port, extra, icount, log=None):
    kern = os.path.join(WORK, "nb-x86_64", "boot", "vmlinuz-virt")
    initrd = os.path.join(WORK, "initramfs-x86_64.cpio")
    cmd = [QEMU, "-accel", "tcg", "-machine", "q35", "-cpu", "qemu64",
           "-m", "1024", "-smp", "1",
           "-kernel", kern, "-initrd", initrd,
           "-append", "console=ttyS0 natemu.task=none panic=-1 rdinit=/init loglevel=3",
           "-display", "none", "-nodefaults", "-monitor", "none", "-no-reboot",
           "-S", "-gdb", "tcp::%d" % port]
    if icount is not None:
        cmd += ["-icount", "shift=%d,sleep=off" % icount]
    cmd += extra
    out = open(log, "w") if log else subprocess.DEVNULL
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out)


def connect(port, tries=60):
    for _ in range(tries):
        try:
            return RSP(port=port)
        except OSError:
            time.sleep(0.25)
    raise SystemExit("could not connect to gdbstub on %d" % port)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--steps", type=int, default=20000)
    p.add_argument("--icount", type=int, default=None,
                   help="pass -icount shift=N,sleep=off to both sides")
    p.add_argument("--a-args", default="", help="comma-separated extra QEMU args, side A")
    p.add_argument("--b-args", default="", help="comma-separated extra QEMU args, side B")
    p.add_argument("--inject-divergence", type=int, default=-1,
                   help="corrupt side B's reported state at step N (self-test)")
    p.add_argument("--report-every", type=int, default=5000)
    p.add_argument("--full-regs", action="store_true",
                   help="compare the entire g packet (incl. x87/SSE), not just the 24 core regs")
    a = p.parse_args()

    ea = [x for x in a.a_args.split(",") if x]
    eb = [x for x in a.b_args.split(",") if x]
    pa = launch(7801, ea, a.icount, "/tmp/lockstep_a.log")
    pb = launch(7802, eb, a.icount, "/tmp/lockstep_b.log")
    ra, rb = connect(7801), connect(7802)
    print("A: qemu %s  B: qemu %s  noack=%s/%s" % (ea or "(baseline)", eb or "(baseline)",
                                                   ra.noack, rb.noack))

    n = 0
    t0 = time.monotonic()
    diverged = None
    hist = []  # ring buffer of the last few (n, rip) for the post-mortem
    try:
        while n < a.steps:
            ra.step(); rb.step()
            ga, gb = ra.regs_hex(), rb.regs_hex()
            if not a.full_regs:
                ga, gb = ga[:CORE_HEXLEN], gb[:CORE_HEXLEN]
            n += 1
            if n == a.inject_divergence:
                gb = gb[:2] + ("f" if gb[2] != "f" else "0") + gb[3:]
            ca = decode_core(ga)
            hist.append((n, ca["rip"]))
            if len(hist) > 8:
                hist.pop(0)
            if ga != gb:
                diverged = (n, ca, decode_core(gb))
                break
            if a.report_every and n % a.report_every == 0:
                el = time.monotonic() - t0
                print("  step %8d  rip=%016x  %8.0f steps/s" % (n, ca["rip"], n / el))
    except KeyboardInterrupt:
        pass
    el = time.monotonic() - t0

    print("\n--- lockstep result ---")
    print("steps            : %d" % n)
    print("wall             : %.3f s" % el)
    print("rate             : %.0f steps/s" % (n / el if el else 0))
    print("regs compared    : %s" % ("full g packet" if a.full_regs else
                                     "%d core (16 GPR + RIP + EFLAGS + 6 seg)" % 24))
    if diverged:
        n, ca, cb = diverged
        print("\nDIVERGENCE at step %d" % n)
        print("recent RIP trail : %s" % " ".join("%d:%x" % h for h in hist))
        for name, va, vb in sorted(diff_core(ca, cb)):
            if name == "eflags":
                print("  %-7s A=%08x [%s]" % (name, va, eflags_str(va)))
                print("  %-7s B=%08x [%s]" % ("", vb, eflags_str(vb)))
                print("  %-7s delta bits: %s" % ("", eflags_str(va ^ vb)))
            else:
                print("  %-7s A=%016x  B=%016x  xor=%016x" % (name, va, vb, va ^ vb))
        rc = 1
    else:
        print("\nNO DIVERGENCE in %d instructions" % n)
        rc = 0

    ra.kill(); rb.kill()
    for q in (pa, pb):
        q.terminate()
        try:
            q.wait(timeout=5)
        except subprocess.TimeoutExpired:
            q.kill()
    return rc


if __name__ == "__main__":
    sys.exit(main())
