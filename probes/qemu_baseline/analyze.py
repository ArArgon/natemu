#!/usr/bin/env python3
"""
analyze.py -- turn probes/qemu_baseline/results/* into slowdown tables.

Every ratio printed here names its numerator, its denominator and its run
count. Nothing is reported as a bare multiplier.
"""
import glob, json, os, re, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")

BENCH_ORDER = ["alu", "alu4", "ls4", "ls4tlb", "ptr8", "ptr256",
               "stream", "matmul", "nqueens", "syscall"]


def parse_txt(path):
    out = {}
    for line in open(path):
        f = line.split()
        if len(f) >= 4 and f[0] == "BENCH" and f[2] == "s":
            pass
        if line.startswith("BENCH ") and " s " in line:
            f = line.split()
            out[f[1]] = float(f[2])
    return out


def parse_json(path):
    d = json.load(open(path))
    return {b["name"]: b["guest_s"] for b in d.get("benches", [])}, d


def collect(pattern, kind):
    runs = []
    for p in sorted(glob.glob(os.path.join(RES, pattern))):
        if kind == "txt":
            runs.append(parse_txt(p))
        else:
            runs.append(parse_json(p)[0])
    return [r for r in runs if r]


def agg(runs):
    """-> {bench: (median, min, max, n, cv%)}"""
    out = {}
    names = set()
    for r in runs:
        names |= set(r)
    for n in names:
        v = [r[n] for r in runs if n in r]
        if not v:
            continue
        m = statistics.median(v)
        cv = (statistics.pstdev(v) / statistics.mean(v) * 100) if len(v) > 1 else 0.0
        out[n] = (m, min(v), max(v), len(v), cv)
    return out


def main():
    envs = {
        "native-arm64":       collect("native-arm64.*.txt", "txt"),
        "rosetta-x64":        collect("rosetta-x64.*.txt", "txt"),
        "aarch64-hvf":        collect("aarch64-hvf.*.json", "json"),
        "x86_64-tcg-single":  collect("x86_64-tcg-single.*.json", "json"),
        "aarch64-tcg-single": collect("aarch64-tcg-single.*.json", "json"),
    }
    A = {k: agg(v) for k, v in envs.items() if v}

    print("=" * 100)
    print("ABSOLUTE TIMES (median of n runs, seconds; cv% = coefficient of variation)")
    print("=" * 100)
    hdr = f"{'bench':10s}"
    for e in A:
        hdr += f"{e:>22s}"
    print(hdr)
    for b in BENCH_ORDER:
        row = f"{b:10s}"
        for e in A:
            if b in A[e]:
                m, lo, hi, n, cv = A[e][b]
                row += f"{m:12.4f} (n{n} {cv:4.1f}%)"
            else:
                row += f"{'-':>22s}"
        print(row)

    def table(num, den, title):
        if num not in A or den not in A:
            return
        print()
        print("=" * 100)
        print(f"{title}\n  numerator = {num}   denominator = {den}")
        print("=" * 100)
        print(f"{'bench':10s} {'num_s':>10s} {'den_s':>10s} {'slowdown':>10s} "
              f"{'n_num':>6s} {'n_den':>6s} {'cv_num':>8s} {'cv_den':>8s} {'flag':>8s}")
        for b in BENCH_ORDER:
            if b not in A[num] or b not in A[den]:
                continue
            mn, _, _, nn, cvn = A[num][b]
            md, _, _, nd, cvd = A[den][b]
            flag = "NOISY" if (cvn > 5 or cvd > 5) else ""
            print(f"{b:10s} {mn:10.4f} {md:10.4f} {mn/md:9.2f}x {nn:6d} {nd:6d} "
                  f"{cvn:7.1f}% {cvd:7.1f}% {flag:>8s}")

    table("x86_64-tcg-single", "aarch64-hvf",
          "PRIMARY: x86-64 full-system TCG vs aarch64 full-system HVF (same QEMU, same guest OS)")
    table("x86_64-tcg-single", "native-arm64",
          "x86-64 full-system TCG vs bare macOS/arm64 (conflates emulation + virtualization)")
    table("rosetta-x64", "native-arm64",
          "Rosetta 2 x86-64 userspace vs bare macOS/arm64 (translation-quality ceiling)")
    table("x86_64-tcg-single", "rosetta-x64",
          "x86-64 TCG vs Rosetta 2 (both translate x86-64 -> arm64)")
    table("aarch64-tcg-single", "aarch64-hvf",
          "CONTROL: aarch64 TCG vs aarch64 HVF (same ISA: isolates 'TCG-ness' from 'x86-ness')")
    table("aarch64-hvf", "native-arm64",
          "CONTROL: aarch64 HVF guest vs bare macOS (virtualization + guest-OS cost alone)")

    # ---- softmmu marginal cost ------------------------------------------
    print()
    print("=" * 100)
    print("SOFTMMU MARGINAL COST  (ls4 - alu4) / (8 memops * 3.0e8 iters) = 2.4e9 memops")
    print("  ls4 and alu4 are the identical loop; ls4 adds exactly 1 load + 1 store per chain.")
    print("=" * 100)
    MEMOPS = 8 * 300_000_000
    base = {}
    for e in A:
        if "ls4" in A[e] and "alu4" in A[e]:
            d = A[e]["ls4"][0] - A[e]["alu4"][0]
            base[e] = d / MEMOPS * 1e9
            print(f"{e:22s} alu4={A[e]['alu4'][0]:8.4f}s ls4={A[e]['ls4'][0]:8.4f}s "
                  f"delta={d:8.4f}s -> {base[e]:7.3f} ns per guest memory access")
    if "x86_64-tcg-single" in base and "aarch64-hvf" in base:
        print()
        print(f"  TCG marginal cost - HVF marginal cost = "
              f"{base['x86_64-tcg-single'] - base['aarch64-hvf']:.3f} ns/access "
              f"attributable to QEMU softmmu.")
    for e in A:
        if "ls4" in A[e] and "ls4tlb" in A[e]:
            # ls4tlb runs 6e7 iters, ls4 runs 3e8
            per_tlb = A[e]["ls4tlb"][0] / 6e7
            per_ls4 = A[e]["ls4"][0] / 3e8
            print(f"{e:22s} per-iteration: ls4={per_ls4*1e9:7.2f} ns  "
                  f"ls4tlb={per_tlb*1e9:7.2f} ns  ratio={per_tlb/per_ls4:5.2f}x "
                  f"(8192-page address footprint vs 1 page)")

    # ---- whole-suite geomean --------------------------------------------
    print()
    print("=" * 100)
    print("GEOMEANS")
    print("=" * 100)

    def geo(num, den, subset=None):
        if num not in A or den not in A:
            return None
        vals = []
        for b in (subset or BENCH_ORDER):
            if b in A[num] and b in A[den]:
                vals.append(A[num][b][0] / A[den][b][0])
        if not vals:
            return None
        p = 1.0
        for v in vals:
            p *= v
        return p ** (1.0 / len(vals)), len(vals)

    CPU_ONLY = ["alu", "alu4", "ls4", "matmul", "nqueens"]
    for num, den in [("x86_64-tcg-single", "aarch64-hvf"),
                     ("x86_64-tcg-single", "native-arm64"),
                     ("rosetta-x64", "native-arm64"),
                     ("aarch64-tcg-single", "aarch64-hvf"),
                     ("aarch64-hvf", "native-arm64")]:
        g = geo(num, den)
        gc = geo(num, den, CPU_ONLY)
        if g:
            print(f"{num:22s} / {den:20s}  all10 geomean {g[0]:6.2f}x (n={g[1]})   "
                  f"cpu-only[{','.join(CPU_ONLY)}] {gc[0]:6.2f}x")

    # ---- boot times ------------------------------------------------------
    print()
    print("=" * 100)
    print("BOOT TIME (host wall clock from qemu exec to the marker, seconds)")
    print("=" * 100)
    for cfg in ["aarch64-hvf", "x86_64-tcg", "aarch64-tcg"]:
        for marker in ["NATEMU_INIT_UP", "NATEMU_SHELL_UP"]:
            vals = []
            for p in sorted(glob.glob(os.path.join(RES, f"boot.{cfg}.*.json"))):
                d = json.load(open(p))
                if marker in d["events"]:
                    vals.append(d["events"][marker])
            if vals:
                cv = statistics.pstdev(vals) / statistics.mean(vals) * 100 if len(vals) > 1 else 0
                print(f"{cfg:14s} {marker:18s} median {statistics.median(vals):7.3f}s  "
                      f"min {min(vals):6.3f}  max {max(vals):6.3f}  n={len(vals)}  cv={cv:.1f}%")


if __name__ == "__main__":
    main()
