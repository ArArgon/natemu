#!/usr/bin/env python3
"""Emit compact machine-readable summary rows used to fill in doc 15."""
import glob, json, os, statistics
HERE=os.path.dirname(os.path.abspath(__file__)); RES=os.path.join(HERE,"results")
def txt(p):
    d={}
    for l in open(p):
        if l.startswith("BENCH ") and " s " in l:
            f=l.split(); d[f[1]]=float(f[2])
    return d
def js(p):
    d=json.load(open(p)); return {b["name"]:b["guest_s"] for b in d.get("benches",[])}
def coll(pat,fn): return [fn(p) for p in sorted(glob.glob(os.path.join(RES,pat)))]
E={"native":coll("native-arm64.*.txt",txt),"rosetta":coll("rosetta-x64.*.txt",txt),
   "hvf":coll("aarch64-hvf.*.json",js),"tcg":coll("x86_64-tcg-single.*.json",js),
   "atcg":coll("aarch64-tcg-single.*.json",js)}
M={k:{n:statistics.median([r[n] for r in v if n in r]) for n in (v[0] if v else {})} for k,v in E.items() if v}
N={k:len(v) for k,v in E.items()}
print(json.dumps({"median":M,"n":N},indent=1))
