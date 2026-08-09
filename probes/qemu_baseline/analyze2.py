#!/usr/bin/env python3
"""analyze2.py -- 7-Zip, MTTCG, QEMU-knob and profile summaries."""
import glob, json, os, re, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")


def sevenzip_from_text(txt):
    """Return dict with compress/decompress/total MIPS from a `7zz b` report."""
    out = {}
    for line in txt.splitlines():
        m = re.match(r"\s*Avr:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*\|\s*"
                     r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", line)
        if m:
            out["compress_mips"] = int(m.group(4))
            out["decompress_mips"] = int(m.group(8))
        m = re.match(r"\s*Tot:\s+(\d+)\s+(\d+)\s+(\d+)", line)
        if m:
            out["total_mips"] = int(m.group(3))
        m = re.match(r"\s*22:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*\|\s*"
                     r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", line)
        if m and "compress_mips" not in out:
            out["compress_mips"] = int(m.group(4))
            out["decompress_mips"] = int(m.group(8))
        m = re.search(r"SEVENZIP_WALL start=([\d.]+) end=([\d.]+)", line)
        if m:
            out["wall_s"] = float(m.group(2)) - float(m.group(1))
    return out


def load_7z():
    envs = {}
    for p in sorted(glob.glob(os.path.join(RES, "7z.*"))):
        name = os.path.basename(p).split(".")[1]
        if p.endswith(".json"):
            d = json.load(open(p))
            txt = "\n".join(l[1] for l in d.get("lines", []))
        else:
            txt = open(p).read()
        r = sevenzip_from_text(txt)
        if r:
            envs.setdefault(name, []).append(r)
    return envs


def med(vals):
    return statistics.median(vals) if vals else None


def main():
    print("=" * 92)
    print("7-ZIP 26.01 BENCHMARK (`7zz b -mmt1 -md=22`), same upstream release in all environments")
    print("  MIPS is 7-Zip's own rating; HIGHER is better. slowdown = ref_MIPS / this_MIPS")
    print("=" * 92)
    envs = load_7z()
    ref = None
    rows = {}
    for e, runs in envs.items():
        c = med([r.get("compress_mips") for r in runs if r.get("compress_mips")])
        d = med([r.get("decompress_mips") for r in runs if r.get("decompress_mips")])
        t = med([r.get("total_mips") for r in runs if r.get("total_mips")])
        w = med([r.get("wall_s") for r in runs if r.get("wall_s")])
        rows[e] = (c, d, t, w, len(runs))
    order = ["native-arm64", "rosetta-x64", "aarch64-hvf", "x86_64-tcg"]
    print(f"{'env':16s} {'compress':>10s} {'decomp':>10s} {'total':>10s} {'wall_s':>9s} {'n':>3s}")
    for e in order + [x for x in rows if x not in order]:
        if e not in rows:
            continue
        c, d, t, w, n = rows[e]
        print(f"{e:16s} {str(c):>10s} {str(d):>10s} {str(t):>10s} "
              f"{('%.1f' % w) if w else '-':>9s} {n:>3d}")
    if "aarch64-hvf" in rows and "x86_64-tcg" in rows:
        print()
        for field, i in [("compress", 0), ("decompress", 1), ("total", 2)]:
            a = rows["aarch64-hvf"][i]
            b = rows["x86_64-tcg"][i]
            if a and b:
                print(f"  {field:12s} x86_64-tcg vs aarch64-hvf : {a/b:5.2f}x slower")
    if "native-arm64" in rows:
        for e in ["rosetta-x64", "x86_64-tcg", "aarch64-hvf"]:
            if e in rows and rows[e][0] and rows["native-arm64"][0]:
                for field, i in [("compress", 0), ("decompress", 1)]:
                    print(f"  {field:12s} {e:14s} vs native-arm64: "
                          f"{rows['native-arm64'][i]/rows[e][i]:5.2f}x slower")
    if "rosetta-x64" in rows and "x86_64-tcg" in rows:
        print()
        for field, i in [("compress", 0), ("decompress", 1)]:
            print(f"  {field:12s} Rosetta 2 is {rows['rosetta-x64'][i]/rows['x86_64-tcg'][i]:5.2f}x "
                  f"faster than QEMU TCG (both x86-64 -> arm64)")

    # ---------------- MTTCG ------------------------------------------------
    print()
    print("=" * 92)
    print("MTTCG: N independent single-threaded guest workers (alu4+ls4), -smp N")
    print("  wall time from NATEMU_TASK_BEGIN to BENCHDONE, host clock")
    print("=" * 92)
    print(f"{'thread':8s} {'smp':>4s} {'wall_s':>9s} {'n':>3s} {'cv%':>6s} {'vs single-smp1':>16s}")
    base = None
    tab = {}
    for th in ["single", "multi"]:
        for n in [1, 2, 4]:
            vals = []
            for p in sorted(glob.glob(os.path.join(RES, f"mttcg.{th}.smp{n}.*.json"))):
                d = json.load(open(p))
                ev = d["events"]
                if "BENCHDONE" in ev and "NATEMU_TASK_BEGIN" in ev:
                    vals.append(ev["BENCHDONE"] - ev["NATEMU_TASK_BEGIN"])
            if vals:
                m = statistics.median(vals)
                cv = statistics.pstdev(vals) / statistics.mean(vals) * 100 if len(vals) > 1 else 0
                tab[(th, n)] = m
                if base is None:
                    base = m
                print(f"{th:8s} {n:>4d} {m:9.3f} {len(vals):>3d} {cv:5.1f}% {m/base:15.2f}x")
    if ("single", 4) in tab and ("multi", 4) in tab:
        print(f"\n  MTTCG speedup at 4 workers / -smp 4: "
              f"{tab[('single',4)]/tab[('multi',4)]:.2f}x "
              f"(single-threaded TCG {tab[('single',4)]:.2f}s -> MTTCG {tab[('multi',4)]:.2f}s)")

    # ---------------- knobs ------------------------------------------------
    print()
    print("=" * 92)
    print("QEMU KNOB SENSITIVITY (median of n, seconds, x86_64 TCG unless noted)")
    print("=" * 92)
    def bench_map(pat):
        runs = []
        for p in sorted(glob.glob(os.path.join(RES, pat))):
            d = json.load(open(p))
            runs.append({b["name"]: b["guest_s"] for b in d.get("benches", [])})
        if not runs:
            return None
        return {k: statistics.median([r[k] for r in runs if k in r]) for k in runs[0]}, len(runs)

    baseline = bench_map("x86_64-tcg-single.*.json")
    others = {
        "cpu=qemu64": bench_map("knob.cpu-qemu64.*.json"),
        "tb-size=1024MB": bench_map("knob.tb1024.*.json"),
        "thp=never": bench_map("knob.thpoff.*.json"),
        "thp=never (HVF)": bench_map("knob.thpoff-hvf.*.json"),
    }
    if baseline:
        names = list(baseline[0])
        print(f"{'knob':20s} " + "".join(f"{n:>10s}" for n in names))
        print(f"{'baseline(cpu=max)':20s} " + "".join(f"{baseline[0][n]:10.3f}" for n in names))
        for k, v in others.items():
            if v:
                print(f"{k:20s} " + "".join(
                    f"{v[0].get(n, float('nan')):10.3f}" for n in names))

    # ---------------- profile ---------------------------------------------
    print()
    print("=" * 92)
    print("TCG PLUGIN COUNTS (guest instructions / memory ops actually executed)")
    print("=" * 92)
    for p in sorted(glob.glob(os.path.join(RES, "profile", "plugin.*.txt"))):
        w = os.path.basename(p).split(".")[1]
        for line in open(p):
            if line.startswith("PLUGINCOUNT"):
                print(f"{w:10s} {line.strip()[12:]}")


if __name__ == "__main__":
    main()
