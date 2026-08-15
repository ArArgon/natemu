#!/usr/bin/env python3
"""
runmmu.py -- drive an x86-64 Linux guest under QEMU TCG with plugin_mmu.so
attached, and record both streams needed to slice the run into phases:

  * every serial-console line, stamped with the HOST REALTIME clock
    (time.time(), epoch seconds) -- the plugin stamps its snapshots with
    gettimeofday() from the same clock, so the two streams are directly
    comparable with no epoch-conversion guesswork.
  * the plugin's periodic counter snapshots (written by the plugin itself).

Usage:
  runmmu.py --task none --idle 20 --label bootidle
  runmmu.py --task sevenzip:1 --label sevenzip
  runmmu.py --task spawn400   --label spawn
"""
import argparse, json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "work")
RES = os.path.join(HERE, "results", "mmu")
QEMU = "/opt/homebrew/bin/qemu-system-x86_64"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--task", default="none")
    p.add_argument("--idle", type=int, default=0)
    p.add_argument("--hold", type=int, default=0)
    p.add_argument("--mem", type=int, default=3072)
    p.add_argument("--cpu", default="max")
    p.add_argument("--smp", type=int, default=1,
                   help="vCPU count. >1 needs the per-vCPU plugin state added 2026-08-15; "
                        "TCG stays thread=single so vCPUs remain serialised.")
    p.add_argument("--label", required=True)
    p.add_argument("--pt", type=int, default=1,
                   help="page-table walker. MUST be 0 at --smp > 1: the refcounting is "
                        "not multi-root safe and segfaults (see doc 26 section 8 item 1).")
    p.add_argument("--shadow", type=int, default=1)
    p.add_argument("--memcb", type=int, default=1,
                   help="0 skips the per-access memory callback (SMP bisect)")
    p.add_argument("--regs", type=int, default=1,
                   help="0 disables all guest-register reads (bisect switch for SMP)")
    p.add_argument("--snap-ms", type=int, default=250)
    p.add_argument("--timeout", type=float, default=5400)
    p.add_argument("--no-plugin", action="store_true")
    p.add_argument("--extra-append", default="")
    a = p.parse_args()

    os.makedirs(RES, exist_ok=True)
    snap = os.path.join(RES, f"snap.{a.label}.txt")
    con = os.path.join(RES, f"con.{a.label}.jsonl")

    append = (f"console=ttyS0 natemu.task={a.task} natemu.idle={a.idle} "
              f"natemu.hold={a.hold} panic=-1 rdinit=/init loglevel=3 "
              f"nokaslr {a.extra_append}").strip()

    # thread=single corrupts the guest at -smp > 1 under any nontrivial plugin
    # (0 NATEMU markers vs 123 with thread=multi, same plugin and guest). The plugin
    # takes a coarse lock so thread=multi is safe for it.
    accel = "tcg,thread=single" if a.smp == 1 else "tcg,thread=multi"
    cmd = [QEMU, "-machine", "q35", "-accel", accel,
           "-cpu", a.cpu, "-m", str(a.mem), "-smp", str(a.smp),
           "-kernel", os.path.join(WORK, "nb-x86_64", "boot", "vmlinuz-virt"),
           "-initrd", os.path.join(WORK, "initramfs2-x86_64.cpio"),
           "-append", append,
           "-display", "none", "-serial", "stdio", "-no-reboot",
           "-nodefaults", "-monitor", "none"]
    if not a.no_plugin:
        cmd[1:1] = ["-plugin",
                    f"{HERE}/bin/plugin_mmu.so,out={snap},pt={a.pt},"
                    f"shadow={a.shadow},regs={a.regs},mem={a.memcb},snap_ms={a.snap_ms}"]

    t_launch = time.time()
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            bufsize=1, universal_newlines=True, errors="replace")
    out = open(con, "w")
    out.write(json.dumps({"t_epoch": t_launch, "line": "__LAUNCH__",
                          "cmd": cmd}) + "\n")
    try:
        for line in proc.stdout:
            t = time.time()
            line = line.rstrip("\r\n")
            out.write(json.dumps({"t_epoch": t, "line": line}) + "\n")
            out.flush()
            if line.startswith(("NATEMU_", "BENCH", "SEVENZIP")):
                print(f"[{t - t_launch:9.3f}] {line}", flush=True)
            if time.time() - t_launch > a.timeout:
                raise TimeoutError
    except TimeoutError:
        print("TIMEOUT", file=sys.stderr)
        proc.kill()
    finally:
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
        out.write(json.dumps({"t_epoch": time.time(), "line": "__EXIT__",
                              "rc": proc.returncode}) + "\n")
        out.close()
    print(f"total host wall {time.time() - t_launch:.1f}s rc={proc.returncode}")
    print(f"  console -> {con}")
    print(f"  snaps   -> {snap}")


if __name__ == "__main__":
    main()
