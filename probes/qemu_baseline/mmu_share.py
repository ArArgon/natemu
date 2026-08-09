#!/usr/bin/env python3
"""
mmu_share.py -- how much of QEMU TCG's runtime is the software MMU?

Combines three independent measurements:
  1. exact guest memory-op counts from the TCG plugin  (results/profile/plugin.*.txt)
  2. the marginal fast-path cost of one guest memory access, from the
     alu4/ls4 ablation                                  (0.776 ns on this host)
  3. the slow-path share from the host profile          (results/profile/sample.*.txt)

softmmu_share = (memops * fast_ns) / t_tcg   +   slowpath_fraction

and the "host MMU instead" projection is 1 / (1 - softmmu_share), i.e. what you
would gain if guest loads/stores became as cheap as they are under HVF (where
the measured marginal cost is 0.006 ns, i.e. free).

This is an UPPER BOUND on the win: it charges nothing for the replacement
mechanism (shadow page-table maintenance, ASID recycling, fault-driven A/D).
"""
import glob, json, math, os, re, statistics, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
PROF = os.path.join(RES, "profile")
FAST_NS = None  # computed from the ablation below


def bench_medians(pat, kind):
    runs = []
    for p in sorted(glob.glob(os.path.join(RES, pat))):
        if kind == "txt":
            d = {}
            for l in open(p):
                if l.startswith("BENCH ") and " s " in l:
                    f = l.split(); d[f[1]] = float(f[2])
        else:
            j = json.load(open(p))
            d = {b["name"]: b["guest_s"] for b in j.get("benches", [])}
        if d:
            runs.append(d)
    if not runs:
        return {}
    keys = set().union(*runs)
    return {k: statistics.median([r[k] for r in runs if k in r]) for k in keys}


def plugin_counts():
    out = {}
    for p in sorted(glob.glob(os.path.join(PROF, "plugin.*.txt"))):
        w = os.path.basename(p).split(".")[1]
        for line in open(p):
            if line.startswith("PLUGINCOUNT"):
                d = dict(kv.split("=") for kv in line.split()[1:] if "=" in kv)
                out[w] = {k: float(v) for k, v in d.items()}
    return out


def slowpath_shares():
    """Re-use analyze_profile.py's bucketing."""
    sys.path.insert(0, HERE)
    import analyze_profile as ap
    out = {}
    for f in sorted(glob.glob(os.path.join(PROF, "sample.*.txt"))):
        w = os.path.basename(f).split(".")[1]
        rows = ap.parse_tree(f)
        sym, total = ap.self_times(rows, "ALL CPUs/TCG")
        if not sym:
            continue
        b = ap.bucketize(sym)
        tot = sum(b.values())
        key = "softmmu slow path: TLB refill + software page-table walk"
        out[w] = b.get(key, 0) / tot if tot else 0.0
    return out


def main():
    tcg = bench_medians("x86_64-tcg-single.*.json", "json")
    hvf = bench_medians("aarch64-hvf.*.json", "json")
    pc = plugin_counts()
    sp = slowpath_shares()

    fast_ns = (tcg["ls4"] - tcg["alu4"]) / 2.4e9 * 1e9
    print(f"fast-path marginal cost from ablation: {fast_ns:.3f} ns/guest memory access")

    # boot + libc baseline: alu4's loop performs no memory ops, so its whole
    # count is boot + process startup, common to every run.
    base = pc.get("alu4", {}).get("guest_mem_ops", 0.0)
    print(f"boot+startup baseline subtracted: {base/1e6:.1f} M memory ops\n")

    print(f"{'bench':9s} {'t_tcg':>8s} {'memops(G)':>10s} {'insns(G)':>9s} "
          f"{'mem/insn':>9s} {'fast%':>7s} {'slow%':>7s} {'softmmu%':>9s} "
          f"{'MMU-free':>9s} {'ns/guest-insn':>14s}")
    rows = []
    for w in ["alu", "alu4", "ls4", "ls4tlb", "ptr8", "ptr256", "stream",
              "matmul", "nqueens", "syscall"]:
        if w not in tcg:
            continue
        t = tcg[w]
        if w in pc:
            mem = max(pc[w]["guest_mem_ops"] - base, 0.0)
            ins = pc[w]["guest_insns"]
            mpi = pc[w]["mem_per_insn"]
        else:
            mem = ins = mpi = float("nan")
        fast = (mem * fast_ns * 1e-9) / t if mem == mem else float("nan")
        slow = sp.get(w, float("nan"))
        share = (0 if fast != fast else fast) + (0 if slow != slow else slow)
        proj = 1.0 / (1.0 - share) if share == share and share < 0.99 else float("nan")
        if mem == mem:
            rows.append((w, proj))
        print(f"{w:9s} {t:8.3f} {mem/1e9:10.3f} {ins/1e9:9.3f} {mpi:9.4f} "
              f"{fast*100:6.1f}% {slow*100:6.1f}% {share*100:8.1f}% "
              f"{proj:8.2f}x {t/ins*1e9:13.3f}")

    ok = [p for _, p in rows if p == p]
    if ok:
        g = math.exp(sum(math.log(p) for p in ok) / len(ok))
        print(f"\nGeomean 'remove the software MMU' projection over {len(ok)} "
              f"workloads: {g:.2f}x  (UPPER BOUND -- charges nothing for shadow paging)")


if __name__ == "__main__":
    main()
