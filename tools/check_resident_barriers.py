#!/usr/bin/env python3
"""Speculation-barrier audit of the resident's page walk (PCID plan P1: RDCL_NO).

    python3 tools/check_resident_barriers.py [RUNTIME]

RUNTIME defaults to target/aarch64-unknown-none/release/natemu-runtime (build it first). Every resident access through the
guest-RAM alias on a guest's behalf (system-instruction transactions through `segments::linear`/`pages`, string I/O, a
page-crossing store's completion, the fetch walk, shadow fills) uses a guest-physical address that `paging::walk`
returned (the audit in docs/restructure/pcid-plan.md, "Measured (P1)"), and the walk is not inlined, so each of its
instances is a function `natemu_abi::paging::walk::<...>` here. In each instance, every path from a page-table entry read
(a call to `GuestRam::load64` or `Tables::read`, or a load reachable from one) to a later load, atomic, call or tail
call must pass a speculation barrier: an Arm `sb` (the leaf permission check) or a `csdb` (the non-leaf levels' data
mask, `paging::descend_base`), so no entry's value is used to address memory, and no access follows, before the checks on
it have resolved. Stack-relative loads (`[sp...]`, spills) are not accesses through the alias and are ignored; plain
stores are not sinks (a speculative store is never visible). Fails (exit 1) when no instance is found or any path lacks
the barrier, naming the read and the access."""
import pathlib, re, subprocess, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from check_resident_stack import DEFAULT, parse  # noqa: E402

WALK = 'natemu_abi::paging::walk_once::<'
ENTRY_READ = re.compile(r'^bl 0x[0-9a-f]+ <.*(GuestRam>::load64|as natemu_abi::paging::Tables>::read)>$', re.I)
BRANCH = re.compile(r'^(b|b\.\w+|cbn?z|tbn?z) .*?0x([0-9a-f]+) <')


def is_load(ins):
    op, _, rest = ins.partition(' ')
    return (op.startswith('ld') or op.startswith(('cas', 'swp'))) and '[sp' not in rest


# A store whose base is not a result-pointer register (review item 3): a speculative store to an entry-derived address is
# an alias access, so it is a sink. `result_ptr_regs` is the exempt set (the returned struct and the stack).
def result_ptr_regs(insns):
    """SP, x8 (the AAPCS indirect-result pointer) and any register the prologue copies it into (`mov xN, x8`): stores
    through these write the returned `Translation` or the stack, not an entry-derived address."""
    regs = {'sp', 'x8'}
    for _, i in insns:
        m = re.fullmatch(r'mov (x\d+), x8', i)
        if m:
            regs.add(m[1])
    return regs


def is_sink(ins, safe_bases):
    op = ins.partition(' ')[0]
    if op.startswith('st') or op.startswith('cas'):
        rest = ins.partition(' ')[2]
        base = re.search(r'\[(\w+)', rest)
        return not (base and base[1] in safe_bases)
    return is_load(ins) or op in ('bl', 'blr', 'br')


def successors(insns, k, index):
    """Indices following instruction k inside the function; None in the list: control leaves it (a tail call)."""
    ins = insns[k][1]
    op = ins.partition(' ')[0]
    if op in ('ret', 'eret', 'udf', 'brk'):
        return []
    out = []
    if m := BRANCH.match(ins):
        out.append(index.get(int(m[2], 16)))
    if op not in ('b', 'br') and k + 1 < len(insns):
        out.append(k + 1)
    return out


def check_walk(name, insns):
    """Problems of one walk instance."""
    index = {a: k for k, (a, _) in enumerate(insns)}
    safe = result_ptr_regs(insns)
    sources = [k for k, (_, i) in enumerate(insns) if ENTRY_READ.match(i)]
    if not sources:
        return [f'{name}: no page-table entry read found']
    # Loads reachable from an entry read are sources too (an inlined read, or a value loaded through one).
    seen, todo = set(), list(sources)
    while todo:
        k = todo.pop()
        for n in successors(insns, k, index):
            if n is not None and n not in seen:
                seen.add(n); todo.append(n)
    sources += [k for k in sorted(seen) if is_load(insns[k][1]) and k not in sources]
    problems = []
    for s in sources:
        visited, todo = set(), [n for n in successors(insns, s, index)]
        while todo:
            k = todo.pop()
            if k is None:
                problems.append(f'{name}: read at {insns[s][0]:#x} reaches a tail call without `sb`'); break
            if k in visited:
                continue
            visited.add(k)
            ins = insns[k][1]
            if ins in ('sb', 'csdb'):
                continue
            if is_sink(ins, safe):
                problems.append(f'{name}: read at {insns[s][0]:#x} `{insns[s][1][:60]}` reaches `{ins[:60]}` at {insns[k][0]:#x} without `sb`')
                break
            todo += successors(insns, k, index)
    return problems


def check(text):
    """`walk_once` is monomorphised twice per Tables type: `..., true>` (barriers on) and `..., false>` (off; the default).
    The barrier-on instances must carry a barrier on every read -> sink path; the off instances must carry none, so a
    barrier added unconditionally (or the switch bypassed) is caught too."""
    funcs, _ = parse(text)
    walks = {n: i for n, i in funcs.items() if n.startswith(WALK)}
    on = {n: i for n, i in walks.items() if n.rstrip().endswith('true>')}
    off = {n: i for n, i in walks.items() if n.rstrip().endswith('false>')}
    problems = [p for n, i in on.items() for p in check_walk(n, i)]
    # Review item 3: the non-leaf mask's condition must include PS (bit 7) and the reserved bits (51:40), not merely sit a
    # CSDB on the path. The mask constant DESCEND_RESERVED = 0x000fff0000000080 is materialised per instance as
    # `mov ..., #0xfff0000000000` (51:40) then `movk ..., #0x80` (PS); a missing PS or reserved part drops one of them.
    for n, insns in on.items():
        text = ' ; '.join(i for _, i in insns)
        if not re.search(r'movk\s+\w+, #0x80\b', text):
            problems.append(f'{n}: no `movk ..., #0x80` (PS, bit 7) in the mask constant: review B1 regression')
        if '#0xfff0000000000' not in text:
            problems.append(f'{n}: no `#0xfff0000000000` (reserved 51:40) in the mask constant')
        if not any(re.search(r'csel\s+\w+, \w+, (xzr|wzr),', i) for _, i in insns):
            problems.append(f'{n}: no `csel ..., xzr` select-to-zero: the mask is not applied')
    for n, insns in off.items():
        barriers = [insns[k][0] for k, (_, i) in enumerate(insns) if i in ('sb', 'csdb')]
        if barriers:
            problems.append(f'{n}: barrier-off instance carries a barrier at {[hex(a) for a in barriers]}')
    if not on:
        problems.append('no barrier-on `walk_once::<..., true>` instance in the resident: the audit cannot run')
    if not off:
        problems.append('no barrier-off `walk_once::<..., false>` instance: the switch is not selectable')
    lines = [f'{n}: {sum(i in ("sb", "csdb") for _, i in insns)} barriers, {len(insns)} instructions' for n, insns in sorted(walks.items())]
    return lines, problems


def main():
    runtime = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    text = subprocess.run(['objdump', '-d', '-C', '--no-show-raw-insn', str(runtime)], check=True,
                          capture_output=True, text=True).stdout
    lines, problems = check(text)
    print('\n'.join(lines + [f'FAIL {p}' for p in problems] + ['ok' if not problems else f'{len(problems)} problem(s)']))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
