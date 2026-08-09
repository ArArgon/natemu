#!/usr/bin/env python3
"""final_numbers.py -- the exact figures quoted in docs/research/15-qemu-tcg-baseline.md.

Rule: where a workload has isolated fresh-VM runs (results/solo.*), those are
used in preference to the in-suite run, because ptr8 is order-dependent (§5.3).
Everything else comes from the 3-run suite medians.
"""
import glob, json, math, os, statistics
RES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
ORD = ["alu","alu4","ls4","ls4tlb","ptr8","ptr256","stream","matmul","nqueens","syscall"]

def txt(p):
    d={}
    for l in open(p):
        if l.startswith("BENCH ") and " s " in l:
            f=l.split(); d[f[1]]=float(f[2])
    return d
def js(p):
    return {b["name"]: b["guest_s"] for b in json.load(open(p)).get("benches", [])}
def med(pat, fn=js):
    rs=[fn(p) for p in sorted(glob.glob(os.path.join(RES,pat)))]
    rs=[r for r in rs if r]
    if not rs: return {}
    ks=set().union(*rs)
    return {k: statistics.median([r[k] for r in rs if k in r]) for k in ks}

E = {
 "native":  med("native-arm64.*.txt", txt),
 "rosetta": med("rosetta-x64.*.txt", txt),
 "hvf":     med("aarch64-hvf.*.json"),
 "tcg":     med("x86_64-tcg-single.*.json"),
 "atcg":    med("aarch64-tcg-single.*.json"),
 "quser":   med("qemuuser.*.json"),
}
solo = med("solo.*.json")
E["tcg"].update({k: v for k, v in solo.items() if k == "ptr8"})   # §5.3

g = lambda v: math.exp(sum(math.log(x) for x in v)/len(v))
PAIRS = [("tcg","hvf"),("tcg","native"),("hvf","native"),("atcg","hvf"),
         ("rosetta","native"),("tcg","rosetta"),("quser","hvf"),("tcg","quser")]
print(f"{'ratio':22s} {'all-10':>8s} {'excl.matmul':>12s} {'scalar-int':>11s} {'memory':>8s}")
SI=["alu","alu4","nqueens"]; MEM=["ls4","ls4tlb","ptr8","ptr256","stream"]
for a,b in PAIRS:
    if not E[a] or not E[b]: continue
    f=lambda S: g([E[a][k]/E[b][k] for k in S if k in E[a] and k in E[b]])
    print(f"{a+'/'+b:22s} {f(ORD):7.2f}x {f([k for k in ORD if k!='matmul']):11.2f}x "
          f"{f(SI):10.2f}x {f(MEM):7.2f}x")
print()
print(f"{'bench':9s} " + "".join(f"{k:>10s}" for k in E))
for k in ORD:
    print(f"{k:9s} " + "".join(f"{E[e].get(k,float('nan')):10.3f}" for e in E))
