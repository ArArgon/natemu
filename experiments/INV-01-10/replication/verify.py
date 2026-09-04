#!/usr/bin/env python3
"""REV-095 independent checker for INV-01-10. Reviewer-written; not derived
from the author's check.py. Recomputes from the raw JSON the invariants
CLM-01-10-04 and -05 rest on, including the provenance distinction KA-09
turns on, and prints claim -> field -> value.

Usage: verify.py <tools.json> <negative.json>   Exit 0 if reproduced.
"""
import json, sys

CLT = ("clang", "lldb", "llvm_objdump", "python3", "git")

def main(a):
    if len(a) != 3:
        print("usage: verify.py <tools.json> <negative.json>"); return 2
    t, n = (json.load(open(p)) for p in a[1:])
    bad = []
    def want(cond, label, got):
        print(f"{'ok  ' if cond else 'FAIL'} {label}: {got}")
        if not cond: bad.append(label)

    tools = t["tools"]
    want(all(tools[k]["present"] and tools[k]["provenance"] == "CLT" and tools[k]["version"] for k in CLT),
         "CLM-01-10-04 five tools present with provenance CLT",
         {k: tools[k]["provenance"] for k in CLT})
    want(tools["rustc"]["present"] and tools["rustc"]["provenance"] == "user-local",
         "CLM-01-10-04 rustc present but NOT CLT (user-local)", tools["rustc"]["path"])
    want(tools["codesign"]["present"] and tools["codesign"]["provenance"] == "OS"
         and tools["codesign"]["version"] is None and tools["codesign"]["version_unavailable_reason"],
         "CLM-01-10-04 codesign present but NOT CLT (OS), null version + reason",
         tools["codesign"]["path"])
    p = t["presence"]
    want(not p["cmake"]["present"] and not p["ninja"]["present"]
         and not p["qemu_system_x86_64"]["present"],
         "CLM-01-10-04 cmake/ninja/qemu absent",
         [k for k in ("cmake", "ninja", "qemu_system_x86_64") if p[k]["present"]] or "all absent")
    x = p["xctrace"]
    want(x["present"] and x["functional_without_xcode"] is False and "requires Xcode" in (x.get("message") or ""),
         "CLM-01-10-04 xctrace present but non-functional, message captured", x.get("message", "")[:60])
    want(p["brew"]["present"] and p["brew"]["version"],
         "CLM-01-10-04 Homebrew present with version", p["brew"]["version"])

    lst = t["sdk_directory_listing"]
    want(len(lst) == 5 and any(s.startswith("MacOSX15") for s in lst)
         and any(s.startswith("MacOSX26") for s in lst),
         "CLM-01-10-05 five SDK entries spanning release lines 15 and 26", lst)
    want(bool(t["clt_build"]), "CLM-01-10-05 CLT package build identifier recorded", t["clt_build"])
    want(bool(t.get("path")), "PATH recorded (confound control)", "recorded")

    want(n["negative_control"] is True and n["tools"]["clang"]["present"] is True
         and not n["tools"]["clang"]["version"],
         "negative control mis-reports clang version as empty",
         repr(n["tools"]["clang"]["version"]))

    print("\nREPRODUCED" if not bad else f"\nNOT REPRODUCED ({len(bad)} check(s) failed)")
    return 0 if not bad else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv))
