#!/usr/bin/env python3
"""Resolve an Alpine package closure from a local APKINDEX and download the .apks.

usage: resolve_apk.py WORKDIR CDN_BRANCH_URL ARCH pkg [pkg...]
"""
import os, re, subprocess, sys

work, base, arch = sys.argv[1], sys.argv[2], sys.argv[3]
want = sys.argv[4:]

# provider name -> (repo, pkgname, version)
provides = {}
deps = {}

for repo in ("main", "community"):
    path = os.path.join(work, f"idx-{repo}-{arch}", "APKINDEX")
    if not os.path.exists(path):
        continue
    cur = {}
    for line in open(path, errors="replace"):
        line = line.rstrip("\n")
        if not line:
            if cur.get("P"):
                key = (repo, cur["P"], cur["V"])
                names = [cur["P"]] + [
                    p.split("=")[0] for p in cur.get("p", "").split()
                ]
                for n in names:
                    provides.setdefault(n, key)
                deps[key] = [
                    d.split("=")[0].split("<")[0].split(">")[0]
                    for d in cur.get("D", "").split()
                    if not d.startswith("!")
                ]
            cur = {}
            continue
        k, _, v = line.partition(":")
        cur[k] = v

seen, order = set(), []
stack = list(want)
missing = []
while stack:
    n = stack.pop()
    key = provides.get(n)
    if key is None:
        if n.startswith("/") or n.startswith("so:") or n.startswith("cmd:") \
           or n.startswith("pc:"):
            missing.append(n)
        continue
    if key in seen:
        continue
    seen.add(key)
    order.append(key)
    stack.extend(deps.get(key, []))

cache = os.path.join(work, "apkcache")
os.makedirs(cache, exist_ok=True)
print(f"resolved {len(order)} packages; unresolved provider names: {len(missing)}")
for repo, name, ver in sorted(order):
    fn = f"{name}-{ver}.apk"
    out = os.path.join(cache, fn)
    if not os.path.exists(out):
        url = f"{base}/{repo}/{arch}/{fn}"
        subprocess.run(["curl", "-sS", "-L", "-o", out, url], check=True)
    print(f"  {repo:9s} {fn}  {os.path.getsize(out)} bytes")
