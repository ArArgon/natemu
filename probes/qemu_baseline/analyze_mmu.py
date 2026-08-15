#!/usr/bin/env python3
"""
analyze_mmu.py -- turn plugin_mmu snapshots into the tables doc 26 needs.

The plugin measures the GUEST's architectural behaviour (rates) and models three
shadow-retention policies. This script does the arithmetic the corpus has been
missing: rate x measured per-event cost = fraction of execution spent in the
shadow MMU.

Per-event costs are quoted from other documents in this corpus and are held in
COSTS below so every number here is traceable. They are given as (low, high)
bands, because doc 18 section 0.2 established that single-run absolute
nanoseconds on this machine carry about a factor of two.

usage: ./analyze_mmu.py results/mmu/snap.M3.*.txt
"""
import sys, os, re

# (low_ns, high_ns, source)
COSTS = {
    # doc 16 item 4: full shadow fill, in-VM, no TLBI. doc 18 section 0.2 caveat
    # widens single-run figures; the low end is doc 16's own 28 ns permission-fault
    # floor scaled up, the high end its 146 ns full fill.
    "fill_xlat": (146.0, 146.0, "16 item 4 (full fill, no TLBI)"),
    # doc 16: permission faults are 3.3x cheaper than translation faults.
    # doc 20 item 9: a complete WP handler INSTALL case is 33.3-34.3 ns.
    "fill_perm": (33.3, 44.2, "20 item 9 / 16 (146/3.3)"),
    # doc 22 section 1.1, 5 runs: cost of one translated x86 instruction.
    "x86_insn": (0.249, 0.258, "22 section 1.1 (5 runs)"),
    # doc 20 item 10: WP MODIFY case, i.e. trap + TLBI + barriers.
    "wp_modify": (231.0, 274.0, "20 item 10"),
    # doc 20 item 9: WP INSTALL case, no TLBI.
    "wp_install": (33.3, 34.3, "20 item 9"),
}


def parse(path):
    d = {"_label": os.path.basename(path).replace("snap.", "").replace(".txt", "")}
    for line in open(path, errors="replace"):
        if line.startswith("SNAP tag=final"):
            for kv in line.split()[1:]:
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    try:
                        d[k] = int(v)
                    except ValueError:
                        d[k] = v
        elif line.startswith("AS "):
            d.setdefault("_as", []).append(
                {k: int(v) for k, v in re.findall(r"(\w+)=(0x[0-9a-f]+|\d+)", line)
                 if not v.startswith("0x")})
    return d


def per_g(n, insns):
    """events per 10^9 guest instructions"""
    return n * 1e9 / insns if insns else 0.0


def main():
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        return 2
    runs = [parse(p) for p in paths]
    runs = [r for r in runs if r.get("insns")]
    if not runs:
        print("no final snapshots found")
        return 2

    print("=" * 100)
    print("1. GUEST RATES  (events per 10^9 guest instructions)")
    print("=" * 100)
    hdr = f"{'workload':<16}{'Ginsns':>8}{'CR3 wr':>10}{'INVLPG':>10}{'excep':>10}{'intr':>10}{'PTE wr':>10}{'zero wr':>11}"
    print(hdr)
    for r in runs:
        i = r["insns"]
        pte = r.get("ptwrite", 0) + r.get("ptwrite_unwalked", 0)
        print(f"{r['_label']:<16}{i/1e9:>8.2f}{per_g(r.get('cr3w',0),i):>10.0f}"
              f"{per_g(r.get('invlpg',0),i):>10.0f}{per_g(r.get('excep',0),i):>10.0f}"
              f"{per_g(r.get('intr',0),i):>10.0f}{per_g(pte,i):>10.0f}"
              f"{per_g(r.get('ptwrite_unwalked_zero',0),i):>11.0f}")

    print()
    print("=" * 100)
    print("2. PAGE-TABLE WRITE TRAFFIC  (the number doc 20's argument rests on)")
    print("=" * 100)
    print(f"{'workload':<16}{'counted':>10}{'+unwalked':>11}{'=PTE tot':>10}{'zeroing':>10}"
          f"{'PTEwr/INVLPG':>14}{'(PTE+0)/INVLPG':>16}")
    for r in runs:
        c = r.get("ptwrite", 0)
        u = r.get("ptwrite_unwalked", 0)
        z = r.get("ptwrite_unwalked_zero", 0)
        inv = max(r.get("invlpg", 0), 1)
        print(f"{r['_label']:<16}{c:>10}{u:>11}{c+u:>10}{z:>10}"
              f"{(c+u)/inv:>14.2f}{(c+u+z)/inv:>16.2f}")
    print("\n  counted   = stores to a frame already identified as a page table")
    print("  unwalked  = PTE-shaped stores to a frame BEFORE it was promoted (bound, heuristic)")
    print("  zeroing   = zero stores to such a frame, i.e. clear_page() on a fresh table")
    print("  A write-protect design traps on PTE+zeroing. A lazy design (doc 20) pays neither.")

    print()
    print("=" * 100)
    print("3. SHADOW-FILL RATES BY RETENTION POLICY")
    print("=" * 100)
    print(f"{'workload':<16}{'A xlat':>10}{'A perm':>10}{'B xlat':>10}{'B perm':>10}"
          f"{'C xlat':>10}{'C perm':>10}{'B/A':>8}{'evict':>9}")
    for r in runs:
        a = r.get("fillA_xlat",0)+r.get("fillA_perm",0)
        b = r.get("fillB_xlat",0)+r.get("fillB_perm",0)
        print(f"{r['_label']:<16}{r.get('fillA_xlat',0):>10}{r.get('fillA_perm',0):>10}"
              f"{r.get('fillB_xlat',0):>10}{r.get('fillB_perm',0):>10}"
              f"{r.get('fillC_xlat',0):>10}{r.get('fillC_perm',0):>10}"
              f"{b/max(a,1):>8.2f}{r.get('asid_evict',0):>9}")
    print("\n  A = per-CR3 shadows kept forever   B = one shadow, wiped on every CR3 load")
    print("  C = per-CR3 shadows, 256 live max, LRU (the host's real ASID budget)")

    print()
    print("=" * 100)
    print("4. THE ARITHMETIC: shadow-MMU cost as a fraction of translated execution")
    print("=" * 100)
    xl, xh = COSTS["x86_insn"][:2]
    fx = COSTS["fill_xlat"][0]
    fpl, fph = COSTS["fill_perm"][:2]
    print(f"  translated x86 instruction : {xl}-{xh} ns   [{COSTS['x86_insn'][2]}]")
    print(f"  shadow fill (translation)  : {fx} ns        [{COSTS['fill_xlat'][2]}]")
    print(f"  shadow fill (permission)   : {fpl}-{fph} ns [{COSTS['fill_perm'][2]}]")
    print()
    # Invalidation is NOT free and an earlier version of this script omitted it.
    # Filling a shadow entry (invalid->valid) needs no TLBI -- that is doc 16's
    # "never TLBI on invalid->valid" rule, worth 3x. But *dropping* one
    # (valid->invalid) does, and shadow_inval counts exactly those. Two ways to
    # pay for it, both real design options:
    #   per-page : one TLBI VAAE1IS per invalidated page (doc 18 section 6.2: 203 ns;
    #              doc 20/16: 279-285 ns).
    #   batched  : accumulate, and issue one TLBI VMALLE1IS at the next context
    #              switch (doc 18 section 6.2: 294 ns). Legal because VMALLE1IS flushes
    #              the ARM TLB without destroying our shadow tables.
    tlbi_page = (203.0, 285.0)
    tlbi_all = 294.0
    print(f"  TLBI per page              : {tlbi_page[0]}-{tlbi_page[1]} ns [18 section 6.2 / 20]")
    print(f"  TLBI all (batched)         : {tlbi_all} ns        [18 section 6.2 VMALLE1IS]")
    print()
    print(f"{'workload':<14}{'exec ms':>9}{'A fill':>8}{'A+inv':>8}{'A%':>7}{'A+bat%':>8}"
          f"{'B fill':>8}{'B+inv':>8}{'B%':>7}{'C%':>7}")
    for r in runs:
        i = r["insns"]
        exec_lo, exec_hi = i*xl/1e6, i*xh/1e6
        exec_mid = (exec_lo+exec_hi)/2
        inv = r.get("shadow_inval", 0)
        inv_ms = inv*(tlbi_page[0]+tlbi_page[1])/2/1e6
        bat_ms = r.get("cr3w", 0)*tlbi_all/1e6
        out = [f"{r['_label']:<14}{exec_mid:>9.0f}"]
        cells = {}
        for p in ("A", "B", "C"):
            fill_ms = (r.get(f"fill{p}_xlat",0)*fx
                       + r.get(f"fill{p}_perm",0)*(fpl+fph)/2)/1e6
            # policy B wipes the whole shadow on CR3, so it pays the batched form
            # by construction; A and C pay per-page unless they batch.
            tot = fill_ms + (bat_ms if p == "B" else inv_ms)
            cells[p] = (fill_ms, tot, 100*tot/exec_mid,
                        100*(fill_ms+bat_ms)/exec_mid)
        out.append(f"{cells['A'][0]:>8.1f}{cells['A'][1]:>8.1f}{cells['A'][2]:>7.1f}{cells['A'][3]:>8.1f}")
        out.append(f"{cells['B'][0]:>8.1f}{cells['B'][1]:>8.1f}{cells['B'][2]:>7.1f}")
        out.append(f"{cells['C'][2]:>7.1f}")
        print("".join(out))
    print("\n  'exec ms' is guest instructions x the measured translated-instruction cost;")
    print("  it is the denominator, NOT a prediction of natemu's runtime. It is also")
    print("  OPTIMISTIC: doc 22 measured 0.25 ns/insn on a synthetic ALU+memory loop with")
    print("  no branch or i-cache pressure, so real translated code is slower and these")
    print("  percentages are correspondingly PESSIMISTIC (too high) by an unknown factor.")
    print("  'A+inv'  = fills + one TLBI per invalidated shadow page")
    print("  'A+bat%' = fills + one batched TLBI VMALLE1IS per CR3 load instead")

    print()
    print("=" * 100)
    print("5. FOOTPRINT")
    print("=" * 100)
    print(f"{'workload':<16}{'roots':>8}{'retired':>9}{'AS live':>9}{'PT frames':>11}"
          f"{'peak PT':>9}{'code pg':>9}{'phys pg':>9}{'shadow_inval':>13}")
    for r in runs:
        print(f"{r['_label']:<16}{r.get('roots',0):>8}{r.get('root_retire',0):>9}"
              f"{r.get('as_live_A',0):>9}{r.get('pt_frames',0):>11}"
              f"{r.get('pt_nodes_peak',0):>9}{r.get('codepages',0):>9}"
              f"{r.get('physframes',0):>9}{r.get('shadow_inval',0):>13}")

    print()
    print("=" * 100)
    print("6. INSTRUMENT VALIDATION")
    print("=" * 100)
    for r in runs:
        bad = r.get("hwchk_bad", 0)
        print(f"  {r['_label']:<16} hwchk={r.get('hwchk',0):>4} hwchk_bad={bad:>4}"
              f"  pt_read_fail={r.get('pt_read_fail',0):>7}"
              f"  ptwrite_unwalked stub fixed={'yes' if 'ptwrite_unwalked_zero' in r else 'NO'}"
              f"  {'OK' if bad == 0 else '*** WALK MISMATCH ***'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
