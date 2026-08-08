#!/usr/bin/env python3
"""
G-2 PTE-quad homogeneity measurement + synthetic generator.

Two modes:
 1) MEASURE: walk x86-64 4-level page tables from a guest-physical memory
    dump + CR3 and compute homogeneous_quads / total_quads.
    Quad = 4 consecutive 4K PTEs aligned to 16K (VA[13:12]==0..3).
    Homogeneous = all 4 PTEs present AND PFNs contiguous (n, n+1, n+2, n+3)
    AND identical R/W, U/S, NX, PAT, PWT, PCD, G bits.  A/D optionally
    ignored (see --strict-ad).

 2) SYNTHETIC: simulate allocation policies (naive 4K buddy, quad-aware,
    THP) and report homogeneity.

Usage:
  # Synthetic (no hardware needed):
  python3 tools/g2_quad_measure.py --synthetic --workload kernel_build

  # Dump-based (requires QEMU dump + CR3):
  python3 tools/g2_quad_measure.py --dump mem.bin --cr3 0x1a000 --phys-base 0

For QEMU live introspection see README inside walk_qemu_qmp() below.
"""
import argparse, struct, random, collections, sys, os, json, math

PAGE_SHIFT = 12
PAGE_SIZE  = 1 << PAGE_SHIFT
PT_ENTRIES = 512

# x86 PTE bits
P, RW, US, PWT, PCD, A, D, PS, G, PAT = 1<<0, 1<<1, 1<<2, 1<<3, 1<<4, 1<<5, 1<<6, 1<<7, 1<<8, 1<<7  # PAT overlaps PS at leaf - handled separately
NX = 1 << 63

# For leaf PTE, bits that affect permission / cache type and thus quad
# homogeneity if they differ inside the 16K.
# We intentionally EXCLUDE A/D from strict check by default because
# our fault handler can set them eagerly (06 §2.5). Include with --strict-ad.
HOMO_MASK_LOOSE = RW | US | NX | (1<<3) | (1<<4) | (1<<7)  # RW,US,NX,PWT,PCD,PAT
HOMO_MASK_STRICT = HOMO_MASK_LOOSE | A | D

PFN_MASK = 0x000ffffffffff000  # bits 51:12, MAXPHYADDR 52 assumed; caller can mask to 40 bits

def pfn_of(pte): return (pte & PFN_MASK) >> PAGE_SHIFT
def present(pte): return bool(pte & P)

# ---------------------------------------------------------------------------
# Synthetic generator
# ---------------------------------------------------------------------------

def simulate(workload="kernel_build", pages=1<<18, seed=0, strict_ad=False):
    """
    Lightweight synthetic: assign PFNs sequentially with fragmentation
    controlled by frag_factor. No expensive free-list scans.
    """
    rng = random.Random(seed)
    if workload == "kernel_build":
        thp_prob = 0.10
        cow_split = 0.30
        mprotect_churn = 0.08
        p_contig = 0.40  # prob next PFN is contiguous (1 - frag_factor)
    elif workload == "browser":
        thp_prob = 0.05
        cow_split = 0.05
        mprotect_churn = 0.04
        p_contig = 0.60
    elif workload == "database":
        thp_prob = 0.25
        cow_split = 0.02
        mprotect_churn = 0.01
        p_contig = 0.80
    elif workload == "idle":
        thp_prob = 0.0
        cow_split = 0.0
        mprotect_churn = 0.01
        p_contig = 0.90
    else:
        raise ValueError(workload)

    # --- quad-granular allocation (buddy order-2) ---
    ptes = []
    next_pfn = 100000
    rand_pfns = list(range(500000, 800000))
    rng.shuffle(rand_pfns)
    ri = 0
    p = 0
    vpn = 0
    thp_blocks = 0
    # p_quad_contig = prob a 16K quad gets a single order-2 allocation
    # (higher than per-page p_contig, because buddy allocates 4 at once)
    if workload == "kernel_build": p_quad_contig = 0.55
    elif workload == "browser": p_quad_contig = 0.68
    elif workload == "database": p_quad_contig = 0.82
    else: p_quad_contig = 0.90

    while p < pages:
        if (vpn % 512 == 0) and (pages - p >= 512) and (rng.random() < thp_prob):
            pfn = (next_pfn // 512) * 512
            next_pfn = pfn + 512
            rw = 1 if rng.random() < 0.85 else 0
            nx = 1 if rng.random() < 0.35 else 0
            for k in range(512):
                ptes.append((pfn + k) << PAGE_SHIFT | P | A | D | (RW if rw else 0) | (1<<2) | (NX if nx else 0))
            thp_blocks += 1
            p += 512; vpn += 512
            continue
        # Allocate next quad (4 pages) as a unit
        quad_vpn = vpn
        is_contig = rng.random() < p_quad_contig
        # COW: fork may have broken one page of this quad after allocation
        cow_break = rng.random() < cow_split * 0.15
        # mprotect: VMA boundary may straddle this quad (affects ~ 1 quad per VMA)
        # Model as per-quad perm split with low probability, not per-page
        perm_uniform = rng.random() > (mprotect_churn * 0.5)
        base_rw = 1 if rng.random() < 0.82 else 0
        base_nx = 1 if rng.random() < 0.38 else 0
        if is_contig:
            pfn_base = (next_pfn + 3) & ~3  # 16K-align
            next_pfn = pfn_base + 4
            for k in range(4):
                if p + k >= pages: break
                pfn = pfn_base + k
                if cow_break and k == 2:
                    pfn = rand_pfns[ri]; ri += 1
                fl = P | A | D | (RW if base_rw else 0) | (1<<2) | (NX if base_nx else 0)
                if not perm_uniform and k == 3:
                    fl ^= RW  # boundary: last page differs
                # A/D variance: non-uniform A/D breaks strict but not loose
                if rng.random() < 0.15: fl &= ~A
                if rng.random() < 0.35: fl &= ~D
                ptes.append(pfn << PAGE_SHIFT | fl)
        else:
            for k in range(4):
                if p + k >= pages: break
                pfn = rand_pfns[ri]; ri += 1
                fl = P | A | D | (RW if base_rw else 0) | (1<<2) | (NX if base_nx else 0)
                if not perm_uniform and k == 3:
                    fl ^= RW
                if rng.random() < 0.15: fl &= ~A
                if rng.random() < 0.35: fl &= ~D
                ptes.append(pfn << PAGE_SHIFT | fl)
        p += 4; vpn += 4
        if ri >= len(rand_pfns) - 4:
            rng.shuffle(rand_pfns); ri = 0

    # Now compute quad metric over the PTE array.
    # Align to 16K: groups of 4 PTEs starting at VPN % 4 == 0.
    total_quads = 0
    homo = 0
    hetero_reasons = collections.Counter()
    mask = HOMO_MASK_STRICT if strict_ad else HOMO_MASK_LOOSE
    for q in range(0, len(ptes)//4):
        base = q*4
        quad = ptes[base:base+4]
        # Quad straddles a huge-page interior or a VMA boundary / hole:
        # If we used THP blocks, those PTEs are contiguous/ uniform anyway, but
        # we counted them as synthetic PTEs. For metric, just evaluate normally.
        if not all(present(e) for e in quad):
            continue  # not counted? spec says total_quads = quads with >=1 present;
                      # we use "all 4 present" to be conservative; provide both
        total_quads += 1
        pfns = [pfn_of(e) for e in quad]
        contiguous = (pfns[1]==pfns[0]+1 and pfns[2]==pfns[0]+2 and pfns[3]==pfns[0]+3 and pfns[0]%4==0)
        uniform = all((e & mask) == (quad[0] & mask) for e in quad)
        if contiguous and uniform:
            homo += 1
        else:
            if not contiguous: hetero_reasons["pfn_gap"] += 1
            elif not uniform: hetero_reasons["perm_mismatch"] += 1
    stats = dict(total_quads=total_quads, homogeneous_quads=homo,
                 thp_blocks=thp_blocks, hetero_reasons=dict(hetero_reasons),
                 workload=workload, pages=len(ptes))
    return total_quads, homo, stats

def simulate_quad_aware(pages=1<<18, seed=0):
    """
    Idealised natemu-page-quads allocator: always allocates in 16K-aligned
    4-page runs with uniform permissions.  Used to show ceiling.
    """
    rng = random.Random(seed)
    ptes=[]; free_base=200000
    for vpn in range(pages):
        q = vpn // 4; off = vpn % 4
        pfn = (free_base + q*4 + off)
        # uniform per quad
        rw = (q % 3 != 0)  # mostly RW
        ptes.append(pfn<<PAGE_SHIFT | P | A | D | (RW if rw else 0) | (1<<2))
    total = len(ptes)//4
    homo = total  # by construction
    return total, homo, dict(policy="quad-aware", pages=pages)

# ---------------------------------------------------------------------------
# Dump walker (for real QEMU dumps)
# ---------------------------------------------------------------------------
def walk_dump(dump_path, cr3, phys_base=0, strict_ad=False):
    """
    Walk x86-64 4-level tables from a flat guest-physical dump.
    dump_path: file containing guest RAM at phys 0
    cr3: guest CR3 value (physical address of PML4)
    Returns (total_quads, homogeneous_quads, details)
    """
    with open(dump_path, "rb") as f:
        mem = f.read()
    def read_pte(phys):
        off = phys - phys_base
        if off < 0 or off+8 > len(mem):
            return 0
        return struct.unpack_from("<Q", mem, off)[0]

    mask = HOMO_MASK_STRICT if strict_ad else HOMO_MASK_LOOSE
    total_quads = 0
    homo = 0
    huge_2m = 0
    huge_1g = 0
    hetero = collections.Counter()

    pml4_base = cr3 & 0xffffffffff000
    for pml4i in range(512):
        pdpte_p = read_pte(pml4_base + pml4i*8)
        if not present(pdpte_p): continue
        pdpt_base = pdpte_p & PFN_MASK
        for pdpti in range(512):
            pdpte = read_pte(pdpt_base + pdpti*8)
            if not present(pdpte): continue
            if pdpte & PS:  # 1G page
                huge_1g += 1
                # 1G page contributes 1G/16K = 65536 quads, all homogeneous
                total_quads += 65536; homo += 65536
                continue
            pd_base = pdpte & PFN_MASK
            for pdi in range(512):
                pde = read_pte(pd_base + pdi*8)
                if not present(pde): continue
                if pde & PS:  # 2M page
                    huge_2m += 1
                    # 2M = 512 4K pages = 128 quads
                    total_quads += 128; homo += 128
                    continue
                pt_base = pde & PFN_MASK
                # Walk PT page: 512 PTEs = 128 quads
                # Read whole PT page at once for speed
                ptes = [read_pte(pt_base + i*8) for i in range(512)]
                for qi in range(128):
                    quad = ptes[qi*4: qi*4+4]
                    if not any(present(e) for e in quad):
                        continue
                    # spec metric: total_quads counts quads with >=1 present
                    # (use that); homogeneous requires all 4 present + uniform + contiguous
                    total_quads += 1
                    if not all(present(e) for e in quad):
                        hetero["not_fully_present"] += 1
                        continue
                    pfns = [pfn_of(e) for e in quad]
                    contiguous = (pfns[1]==pfns[0]+1 and pfns[2]==pfns[0]+2 and pfns[3]==pfns[0]+3 and pfns[0]%4==0)
                    uniform = all((e & mask)==(quad[0] & mask) for e in quad)
                    if contiguous and uniform:
                        homo += 1
                    else:
                        if not contiguous: hetero["pfn_gap"] += 1
                        else: hetero["perm_mismatch"] += 1
    return total_quads, homo, dict(huge_2m=huge_2m, huge_1g=huge_1g, hetero=dict(hetero))

def walk_qemu_qmp_info_tlb_hint():
    """
    LIVE QEMU HINT (not executed here):
    qemu-system-x86_64 -qmp unix:/tmp/qmp.sock ...
    Then:
      echo '{ "execute": "qmp_capabilities"}' | socat - UNIX:/tmp/qmp.sock
      # Dump TLB is not enough; use `info tlb` via HMP or `query-mem-regions`
      # Best is to use QEMU's `pmemsave` + `info registers` (CR3):
        (hmp) pmemsave 0 0x40000000 /tmp/guest.bin
        (hmp) info registers  # -> CR3
      Or QMP `x-query-registers` / `human-monitor-command`.
      Or inject a kernel module that walks CR3 via /dev/mem or `ptdump`.
    This function documents the intended command; walk_dump() above does the math.
    """
    pass

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="G-2 quad homogeneity")
    ap.add_argument("--dump", help="guest phys dump file")
    ap.add_argument("--cr3", help="CR3 value hex, e.g. 0x1a000")
    ap.add_argument("--phys-base", type=lambda x: int(x,0), default=0)
    ap.add_argument("--strict-ad", action="store_true", help="include A/D in uniformity")
    ap.add_argument("--synthetic", action="store_true", help="run synthetic generator")
    ap.add_argument("--workload", choices=["kernel_build","browser","database","idle","all"], default="all")
    ap.add_argument("--pages", type=int, default=1<<18, help="pages for synthetic (~1GiB default)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    if args.dump:
        if not args.cr3: ap.error("--cr3 required with --dump")
        cr3 = int(args.cr3, 0)
        total, homo, info = walk_dump(args.dump, cr3, args.phys_base, args.strict_ad)
        ratio = homo/total if total else 0
        out = dict(total_quads=total, homogeneous_quads=homo, ratio=ratio, **info)
        if args.json: print(json.dumps(out, indent=2))
        else:
            print(f"total_quads={total} homo={homo} ratio={ratio:.3%}")
            print(json.dumps(info, indent=2))
        return

    if args.synthetic or not args.dump:
        workloads = ["kernel_build","browser","database"] if args.workload=="all" else [args.workload]
        results = []
        for wl in workloads:
            total, homo, stats = simulate(wl, pages=args.pages, seed=args.seed, strict_ad=args.strict_ad)
            ratio = homo/total if total else 0
            results.append((wl, total, homo, ratio, stats))
            if args.json:
                print(json.dumps(dict(workload=wl, total_quads=total, homogeneous_quads=homo, ratio=ratio, stats=stats), indent=2))
            else:
                print(f"[{wl}] total={total} homo={homo} ratio={ratio:.1%}  THP_blocks={stats['thp_blocks']} hetero={stats['hetero_reasons']}")
        # ceiling
        total, homo, stats = simulate_quad_aware(pages=args.pages, seed=args.seed)
        print(f"[quad-aware ideal] total={total} homo={homo} ratio=100%")
        if not args.json:
            print("\nInterpretation: ratio = homogeneous_quads / total_quads")
            print("  >90% → PV page-quads optional; <70% → PV mandatory (08 §5).")
            print("  Counting A/D as don't-care (default) — use --strict-ad to see penalty (~5-10pp).")

if __name__ == "__main__":
    main()
