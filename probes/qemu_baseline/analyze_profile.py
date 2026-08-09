#!/usr/bin/env python3
"""
analyze_profile.py -- bucket macOS `sample` output for the QEMU TCG vCPU thread.

`sample` prints an indented call graph whose counts are INCLUSIVE. Self time is
count(node) - sum(count(children)). We rebuild the tree from the column at
which each count starts, then attribute self time to buckets.

Only the "ALL CPUs/TCG" thread is bucketed: with -accel tcg,thread=single every
other QEMU thread is blocked in a syscall for the whole window (verified in the
same file), so that one thread IS the process's CPU time.

TCG-generated code has no symbol and no Mach-O image, so `sample` prints
"???  (in <unknown binary>)". That bucket is exactly "executing translated
code" -- which INCLUDES the inlined softmmu fast path, since QEMU emits it into
the TB itself. The symbolicated qemu-system-x86_64 frames are the runtime:
slow-path memory helpers, translation, dispatch, device model.
"""
import glob, os, re, sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
PROF = os.path.join(HERE, "results", "profile")

LINE = re.compile(r"^(?P<pre>[\s+!:|]*)(?P<n>\d+)\s+(?P<sym>.*?)\s*$")

BUCKETS = [
    ("translated code (TCG output, incl. inlined softmmu fast path)",
     lambda s: "<unknown binary>" in s),
    ("softmmu slow path: TLB refill + software page-table walk",
     lambda s: re.match(r"^(do_ld|do_st|mmu_lookup|mmu_translate|tlb_|victim_|"
                        r"io_read|io_write|address_space_|memory_region_|"
                        r"flatview_|cpu_io_recompile|helper_ld|helper_st|"
                        r"atomic_mmu|cpu_ldst|load_helper|store_helper|"
                        r"probe_access|ptw_|get_physical_address|"
                        r"x86_cpu_tlb_fill|physical_memory_get_dirty|"
                        r"physical_memory_set_dirty|cpu_physical_memory|"
                        r"get_ptr_rcu_reader|qemu_ram_block_from_host|"
                        r"qemu_ram_addr_from_host|ram_block_|find_ram|"
                        r"section_covers|address_space_translate|"
                        r"cpu_unwind_data_from_tb|notdirty|get_pg_mode|find_next_bit|"
                        r"cpu_asidx_from_attrs|x86_get_tb_cpu_state)", s)),
    ("translation / codegen",
     lambda s: re.match(r"^(tb_gen_code|translator_|tcg_gen_|tcg_op|tcg_opt|"
                        r"tcg_reg|tcg_out|tcg_temp|tcg_func|tcg_reg_alloc|"
                        r"liveness_|disas_insn|gen_|decode|translate_|"
                        r"tb_link|tb_page|tb_htable|qht_insert|tcg_tb_alloc|"
                        r"tcg_ctx|do_tb_flush|tb_flush|i386_tr_|init_ts_info|la_func_end|"
                        r"la_cross_call|la_global|la_reset|tcg_flush_jmp_cache|"
                        r"tcg_op_supported|temp_load|tcg_reg_alloc)", s)),
    ("TB dispatch / lookup",
     lambda s: re.match(r"^(cpu_tb_exec|cpu_exec|tb_lookup|tcg_tb_lookup|"
                        r"helper_lookup_tb_ptr|tb_jmp|qht_lookup|g_tree|"
                        r"tb_tc_cmp|tcg_cpu_exec|rr_cpu_thread_fn|"
                        r"cpu_handle|process_queued|curr_cflags)", s)),
    ("other guest-visible helpers",
     lambda s: s.startswith("helper_")),
    ("host kernel / locks / idle",
     lambda s: re.search(r"(in libsystem_|in libpthread|__psynch|mach_msg|"
                         r"__select|pthread_mutex|_longjmp|__ulock)", s)),
]


def parse_tree(path):
    """-> list of (depth_col, count, symbol, thread)"""
    rows = []
    thread = None
    for raw in open(path, errors="replace"):
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        m = re.match(r"^\s*(\d+)\s+(Thread_\S+.*)$", line)
        if m and "Thread_" in line and re.match(r"^\s{0,6}\d", line):
            thread = m.group(2)
            rows.append((0, int(m.group(1)), "THREADROOT " + thread, thread))
            continue
        if line.startswith("Binary Images") or line.startswith("Total number"):
            break
        m = LINE.match(line)
        if not m or thread is None:
            continue
        col = len(m.group("pre"))
        rows.append((col + 1, int(m.group("n")), m.group("sym"), thread))
    return rows


def self_times(rows, want_thread_sub):
    """Return {symbol: self_samples} for the thread whose name contains sub."""
    sel = [r for r in rows if want_thread_sub in (r[3] or "")]
    self_ = [r[1] for r in sel]
    stack = []  # (col, index)
    for i, (col, n, sym, th) in enumerate(sel):
        while stack and stack[-1][0] >= col:
            stack.pop()
        if stack:
            self_[stack[-1][1]] -= n
        stack.append((col, i))
    out = defaultdict(int)
    for i, (col, n, sym, th) in enumerate(sel):
        if sym.startswith("THREADROOT"):
            continue
        out[sym] += self_[i]
    return out, (sel[0][1] if sel else 0)


def bucketize(sym_self):
    b = defaultdict(int)
    for sym, n in sym_self.items():
        if n <= 0:
            continue
        for name, pred in BUCKETS:
            if pred(sym):
                b[name] += n
                break
        else:
            b["unclassified"] += n
    return b


def main():
    files = sorted(glob.glob(os.path.join(PROF, "sample.*.txt")))
    if not files:
        print("no sample files")
        return
    print("=" * 104)
    print("HOST-SIDE PROFILE OF THE QEMU TCG vCPU THREAD  (macOS `sample`, 1 ms, 12 s window)")
    print("=" * 104)
    for f in files:
        w = os.path.basename(f).split(".")[1]
        rows = parse_tree(f)
        sym, total = self_times(rows, "ALL CPUs/TCG")
        if not sym:
            print(f"\n--- {w}: no TCG thread found")
            continue
        b = bucketize(sym)
        tot = sum(v for v in b.values())
        print(f"\n--- {w}   (vCPU-thread samples: {total}, classified {tot})")
        for name, _ in BUCKETS:
            if b.get(name):
                print(f"      {b[name]*100.0/tot:6.2f}%  {name}")
        if b.get("unclassified"):
            print(f"      {b['unclassified']*100.0/tot:6.2f}%  unclassified")
            top = sorted(((n, s) for s, n in sym.items()
                          if n > 0 and not any(p(s) for _, p in BUCKETS)),
                         reverse=True)[:8]
            for n, s in top:
                print(f"              {n*100.0/tot:6.2f}%  {s[:88]}")


if __name__ == "__main__":
    main()
