#!/usr/bin/env python3
"""Where a sampled run's vCPUs were (the EL0 sampler, docs/restructure/el0-sampler-plan.md chunk 0): reads natemu's
`--profile NAME` files, NAME.samples and NAME.objects (their layout: crates/abi/src/sample.rs), and writes
NAME.report.json and NAME.report.md beside them.

    python3 tests/bench/sample_report.py --profile NAME [--markers FILE] [--perf FILE] [--profile NAME ...]
                                         [--window SECONDS] [--require-place PLACE] [--kernel-symbols TABLE] [--boots BZIMAGE] [--image CPIO]
                                         [-- COMMAND ...]

With `-- COMMAND`, the command runs first (a sampling boot: tools/bench/run.py --sample-us, tests/io/run_io5.py --sample-us)
and the report after it, whatever its exit status; the exit status is the command's when it failed, else the report's.
Each `--profile` may be followed by its own `--markers` (the runner's host-stamped console lines: tools/bench/run.py's
NAME.markers.tsv, run_io5.py's markers-PHASE.tsv; CLOCK_UPTIME_RAW ns, the samples' clock) and `--perf` (natemu's timing
log, for each phase's resident buckets: reported, never a decision, since sampling perturbs them). A missing markers or
perf file is noted, not an error (a boot that failed early).

Per profile, per vCPU and summed, per phase: samples by kind (EL0; resident or host: the bell was pending while the vCPU was
outside generated code; halted: the host found it halted and did not ring), EL0 samples by place (block, probe, dispatch
slot, head, step, no block, outside), by privilege and code mode (from the block's code context; a dispatcher slot's target
by its RIP where the slot has no context), dispatcher samples by their call test (the target is a return address, is not,
or unknown), the top 50 blocks by (RIP, frame, context) with their objects, the cumulative curve of block samples, the lost
and merged counts, and the measured samples per second. Phases: the whole run; the runner's markers (bench: boot, run1 and
its named phases, between, run2 and its phases, after; io5: each step); the guest's marks on the control port (F9H), each to
the next; and fixed windows (`--window`, default 1 s) as a time series in the JSON.

Fails (exit 1) when a file is missing or malformed, the run did not end (no counters), a vCPU's records in the file differ
from what the host drained, no EL0 sample was recorded, the control port counted an ignored access, or a `--require-place`
place has no sample.

Roles and symbols (the plan's chunk 1): each block sample's word gets its role from the object's records and the A64
patterns (tests/bench/sample_roles.py), and its symbol (tests/bench/sample_symbols.py): kernel samples by the kallsyms of
`--kernel-symbols TABLE` (the framework's kernel-symbols input; a bzImage is read too) with the KASLR slide from the
profile's markers (`--boots BZIMAGE`: the kernel the run booted, which must be the table's), user samples by finding their
blocks' guest bytes in
the binaries of `--image CPIO` (the bench image; without it user samples stay at block level). Per phase: block samples by
role, user and kernel and 64- and 32-bit apart, by group (FEX's body, its flag words, natemu's words, other), the shares
the plan's section-7 rows read (split checks and probes, stack checks, REP string bodies, glibc AVX2/EVEX code, flag words,
load-then-move, the kernel's copy and clear routines, FEX's body on the top 50 blocks), and the top 30 symbols; the top
50 blocks of the whole run with each word's role and count. A sample past an object's code region (`beyond`) is a
problem."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import bisect
import collections
import json
import mmap
import re
import struct
import subprocess

import sample_roles as roles_mod
import sample_symbols

ROOT = pathlib.Path(__file__).resolve().parents[2]

# --- the format (crates/abi/src/sample.rs; test_sample_report.py checks these against it) -------------------------------
FILE_MAGIC = int.from_bytes(b'NATEMUSA', 'little')
OBJECTS_MAGIC = int.from_bytes(b'NATEMUOB', 'little')
VERSION = 1
RECORD_BYTES = 32
OBJECT_HEADER_BYTES = 56
FILE, EL0, RESIDENT, HALTED, MARK, CLOCK, VCPU, COUNT, TOGGLE, PASS = range(10)
BLOCK, PROBE, DISPATCH, STEP, HEAD, NO_BLOCK, OUTSIDE, TAIL = range(1, 9)
PLACES = {BLOCK: 'block', PROBE: 'probe', DISPATCH: 'dispatch', STEP: 'step', HEAD: 'head', NO_BLOCK: 'no_block',
          OUTSIDE: 'outside', TAIL: 'tail'}
# Indirect plan step 1: a dispatch sample's slot form (its offset in the 256-byte slot: `cache::SLOT_LOOKUP`, `SLOT_RETURN`),
# and a tail sample's role (`sample::TAIL_WORD`, `TAIL_CALL`, `TAIL_POINT` in `at`).
SLOT_BYTES, SLOT_LOOKUP, SLOT_RETURN = 256, 64, 128
FORMS = ('ordinary', 'lookup', 'return')
TAIL_WORDS = ('bl', 'branch', 'pad', 'word3')
TAIL_CALL, TAIL_POINT = 4, 8


def slot_form(at):
    o = at % SLOT_BYTES
    return FORMS[0] if o < SLOT_LOOKUP else FORMS[1] if o < SLOT_RETURN else FORMS[2]


def tail_role(at):
    """'jump-bl', 'call-branch', 'link-call-pad', ...: the exit (computed, or a shared link point's `link-`), jump or call, the word."""
    return f"{'link-' if at & TAIL_POINT else ''}{'call' if at & TAIL_CALL else 'jump'}-{TAIL_WORDS[at & 3]}"
HOT, DEAD, FAR = 1, 2, 4
AT_BIAS = 4                 # a block sample's `at` is its native offset + 4 (a PIC block's full entry word, -4, is 0)
CALL_SHIFT = 2
CALL_YES, CALL_NO, CALL_UNKNOWN = 1, 2, 3
CALLS = {0: 'none', CALL_YES: 'return', CALL_NO: 'not_call', CALL_UNKNOWN: 'unknown'}
COUNTERS = ['rings', 'halted', 'written', 'lost', 'bells', 'bells_in_wait', 'unknown_calls', 'drained', 'port_accesses',
            'port_ignored', 'not_sampling']
RUN = 0xffff_ffff           # a COUNT record's vCPU for the run's own counters
BY = {0: 'start', 1: 'port', 2: 'signal'}
CONTEXT_BITS, RIP_BITS = 40, 48
M56 = (1 << 56) - 1
# The code object a served answer is (crates/abi/src/code_object.rs `Header`): the fields the report reads.
CODE_MAGIC = b'NFXOBJ\0\x01'
CODE_HEADER = struct.Struct('<8sIIIIQIIIIIII')   # magic .. code_region_end
REC = struct.Struct('<4Q')
OBJ = struct.Struct('<IBBHQQQQQII')
KINDS = {EL0: 'el0', RESIDENT: 'resident', HALTED: 'halted'}


class FormatError(Exception):
    pass


def sample(w):
    """A resident record ([`Sample::decode`]) as a dict."""
    rip = w[1] & ((1 << RIP_BITS) - 1)
    if rip >> (RIP_BITS - 1): rip |= ~((1 << RIP_BITS) - 1) & (2 ** 64 - 1)
    return dict(time=w[0], rip=rip, flushes=w[1] >> 48 & 0xfff, flags=w[1] >> 60, context=w[2] & ((1 << CONTEXT_BITS) - 1),
                at=w[2] >> 40 & 0xffff, place=w[2] >> 56 & 0xf, kind=w[2] >> 60, frame=w[3] & 0xffff_ffff, since=w[3] >> 32)


def host(w):
    """A host record ([`HostRecord::decode`]) as a dict."""
    return dict(kind=w[2] >> 60, time=w[0], value1=w[1], value2=w[2] & M56, value3=w[3] & 0xffff_ffff, value4=w[3] >> 32)


def offset(s):
    """A block sample's native offset in its block's code (from the object's code byte 0; -4: a PIC block's full entry), or
    None when it did not fit `at` (FAR)."""
    return None if s['flags'] & FAR else s['at'] - AT_BIAS


def privilege(context, rip, has_context):
    """'user' (CPL 3) or 'kernel' (CPL 0) from a code context; from the RIP's sign where there is none."""
    if has_context: return 'user' if context & 3 == 3 else 'kernel' if context & 3 == 0 else f'cpl{context & 3}'
    return 'kernel' if rip >> 63 else 'user'


def mode(context):
    return {1: '64', 2: '32', 3: '16'}.get(context >> 2 & 3, '?')


# --- the objects file ------------------------------------------------------------------------------------------------
class Objects:
    """NAME.objects: every distinct answer served, indexed by (RIP, frame, context) and by (frame, page offset, context)
    (a block placed from a template at another RIP of the same page offset has the bytes of the answer it came from)."""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        f = open(self.path, 'rb')
        size = self.path.stat().st_size
        self.data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) if size else b''
        f.close()
        if size < RECORD_BYTES: raise FormatError(f'{self.path}: no header')
        magic, version, _, _ = REC.unpack_from(self.data, 0)
        if magic != OBJECTS_MAGIC or version & 0xffff_ffff != VERSION or version >> 32 != OBJECT_HEADER_BYTES:
            raise FormatError(f'{self.path}: not a version {VERSION} objects file')
        self.exact, self.page, self.count = collections.defaultdict(list), collections.defaultdict(list), 0
        at = RECORD_BYTES
        while at < size:
            if at + OBJECT_HEADER_BYTES > size: raise FormatError(f'{self.path}: record at {at} cut short')
            length, _, _, _, rip, frame0, _, context, _, nobj, nguest = OBJ.unpack_from(self.data, at)
            if length < OBJECT_HEADER_BYTES + nobj + nguest or length % 8 or at + length > size:
                raise FormatError(f'{self.path}: record at {at}: length {length} for {nobj} + {nguest} bytes')
            self.exact[(rip, frame0 >> 12, context)].append(at)
            self.page[(frame0 >> 12, rip & 0xfff, context)].append(at)
            self.count += 1
            at += length

    def record(self, at):
        length, source, vcpu, _, rip, frame0, frame1, context, hsh, nobj, nguest = OBJ.unpack_from(self.data, at)
        body = at + OBJECT_HEADER_BYTES
        obj = bytes(self.data[body:body + nobj])
        r = dict(offset=at, source=source, vcpu=vcpu, rip=rip, frame0=frame0, frame1=frame1, context=context, hash=hsh,
                 object=obj, guest=bytes(self.data[body + nobj:body + nobj + nguest]))
        r['code'] = code_header(obj)
        return r

    def find(self, rip, frame, context):
        """(record offsets, how): the answers served for this block, exactly or by page offset; ([], None) if none."""
        if hits := self.exact.get((rip, frame, context)): return hits, 'exact'
        if hits := self.page.get((frame, rip & 0xfff, context)): return hits, 'page'
        return [], None

    def find_rip(self, rip, context):
        """The record offsets of answers served for `rip` in `context`, on any frame (a link target's: its frame unknown)."""
        if not hasattr(self, 'by_rip'):
            self.by_rip = collections.defaultdict(list)
            for (r, _, c), hits in self.exact.items(): self.by_rip[(r, c)] += hits
        return self.by_rip.get((rip, context), [])


def code_header(obj):
    """The code object's header fields the report reads, or None if the bytes are not a code object."""
    if len(obj) < CODE_HEADER.size or obj[:8] != CODE_MAGIC: return None
    (_, version, header_size, total, bitness, rip, guest_bytes, guest_insns, code_offset, code_size, body_offset,
     end_marker, region_end) = CODE_HEADER.unpack_from(obj, 0)
    return dict(version=version, header_size=header_size, total_size=total, guest_bitness=bitness, guest_rip=rip,
                guest_bytes=guest_bytes, guest_insns=guest_insns, code_offset=code_offset, code_size=code_size,
                body_offset=body_offset, end_marker_offset=end_marker, code_region_end=region_end)


def locate(objects, s):
    """A block sample's (place BLOCK, PROBE or STEP) object and word: None if the place has no block or no object was
    served for it; else {record, how ('exact' or 'page'), candidates (answers with that key: their bytes differ), offset
    (the native offset in the object's code: a PIC block's full entry is at -4; None when it did not fit the record, FAR),
    word_index, word (its 4 bytes; None outside the code region: FEX's tail is not placed), beyond (a sample past the code
    region: a fault the report counts)}. The first candidate is taken."""
    if s['place'] not in (BLOCK, PROBE, STEP): return None
    hits, how = objects.find(s['rip'], s['frame'], s['context'])
    if not hits: return None
    r = objects.record(hits[0])
    off = offset(s)
    c, word = r['code'], None
    # Admission places the code region only (code_region_end); FEX's tail after it is stripped, so no sample lies there.
    beyond = bool(c) and off is not None and (off < -4 or off + 4 > c['code_region_end'])
    if c and off is not None and 0 <= off and off + 4 <= c['code_region_end']:
        base = c['code_offset'] + off
        word = r['object'][base:base + 4] if base + 4 <= len(r['object']) else None
    return dict(record=r, how=how, candidates=len(hits), offset=off, word_index=off // 4 if off is not None and off >= 0 else None,
                word=word, beyond=beyond)


# --- roles and symbols (chunk 1) ---------------------------------------------------------------------------------------
NO_OFFSET = 1 << 30         # a FAR sample's offset, for sorting


class Attribution:
    """Each block sample's role and symbol, from its object (sample_roles, sample_symbols). `kernel` a Kallsyms, `slide` the
    guest's KASLR slide, `image` a sample_symbols.Image (None: user samples stay at block level)."""

    def __init__(self, objects, kernel=None, slide=0, image=None):
        self.objects, self.kernel, self.slide, self.image = objects, kernel, slide, image
        self.info_of, self.user, self.sym_of = {}, {}, {}
        self.ambiguous = self.matched = self.looked_up = self.short = self.multi_in_file = 0
        self.reach = {}             # per block, the highest native offset sampled (set_reach)
        self.entry_of = {}

    def set_reach(self, words):
        for (k, off, _), _ in words.items():
            if off is not None and off > self.reach.get(k, -4): self.reach[k] = off

    def info(self, key):
        """The block's roles, per-word instruction offsets, REP string kinds per instruction and whether it holds a VEX.256 or
        EVEX instruction; None when no object (or no code object) was served for it. Of several answers served with the
        block's key (their bytes differ: a shorter compile, code patched since), the first whose code region holds every
        sampled offset, else the one with the largest region."""
        if key in self.info_of: return self.info_of[key]
        info = None
        hits, _ = self.objects.find(*key) if self.objects is not None else ([], None)
        codes = []
        for h in hits:
            r = self.objects.record(h)
            try: codes.append((roles_mod.Code(r['object']), r))
            except (ValueError, IndexError, struct.error): continue
        if codes:
            reach = self.reach.get(key, -4)
            c, r = next(((c, r) for c, r in codes if c.region_end > reach), max(codes, key=lambda x: x[0].region_end))
            if c:
                insns, guest, bits = c.word_insns(), r['guest'], 64 if mode(key[2]) == '64' else 32
                rep, form, wide = {}, {}, False
                for g in {x for x in insns if x is not None}:
                    ib = roles_mod.insn_bytes(c, guest, g)
                    if k := roles_mod.x86_rep_string(ib, bits): rep[g] = k
                    if f := roles_mod.x86_rep_form(ib, bits): form[g] = f
                    wide |= roles_mod.x86_wide_vector(ib, bits) is not None
                info = dict(roles=roles_mod.roles(c), insns=insns, rep=rep, rep_form=form, wide=wide, exits=roles_mod.exit_targets(c),
                            delta=key[0] - c.rip, guest=guest if privilege(key[2], key[0], True) == 'user' else None)
        self.info_of[key] = info
        return info

    def entry_role(self, rip, context):
        """The role of the first body word (ENTRY_OFFSET) of the block for `rip` in `context`, or None (no object)."""
        k = (rip, context)
        if k not in self.entry_of:
            role = None
            for h in self.objects.find_rip(rip, context) if self.objects is not None else []:
                try: c = roles_mod.Code(self.objects.record(h)['object'])
                except (ValueError, IndexError, struct.error): continue
                rs = roles_mod.roles(c)
                if len(rs) > 1: role = rs[1]; break
            self.entry_of[k] = role
        return self.entry_of[k]

    def skid_role(self, key, info, off, probe, role):
        """The role under the one-word skid correction (sample_roles.NOT_MOVED's comment): the next word executed."""
        if probe or info is None or role in roles_mod.NOT_MOVED: return role
        if role == 'exit_literal' and off is not None and off >= 0:
            t = info['exits'].get(off // 4)
            r = self.entry_role((t + info['delta']) & (2 ** 64 - 1), key[2]) if t is not None else None
            return r or role
        i = roles_mod.next_word(info['roles'], off)
        return role if i is None else info['roles'][i]

    def insn(self, info, off):
        return info['insns'][off // 4] if info and off is not None and 0 <= off < 4 * len(info['insns']) else None

    def match_user(self, keys):
        """Finds the user blocks `keys` in the image's binaries (once each)."""
        if self.image is None: return
        queries = {}
        for k in keys:
            if k in self.user or privilege(k[2], k[0], True) != 'user': continue
            info = self.info(k)
            self.user[k] = None
            if not (info and info['guest']): continue
            if len(info['guest']) < sample_symbols.MATCH_BYTES: self.user[k] = 'short'; self.short += 1; continue
            queries[k] = (k[0] & 0xfff, info['guest'], mode(k[2]) == '64')
        self.looked_up += len(queries)
        for k, hits in self.image.match(queries).items():
            files = sorted({e.name for e, _ in hits})
            if len(files) > 1:
                self.ambiguous += 1
                self.user[k] = ('ambiguous', files)
                continue
            if len(hits) > 1: self.multi_in_file += 1     # the same bytes twice in one file: named by the first
            self.user[k] = hits[0]
            self.matched += 1

    def symbol(self, key, info, off):
        """'kernel:NAME', 'kernel:module' (outside the image's text, in the module area), 'FILE:NAME', 'FILE:?' (found, no
        symbol), 'ambiguous:F1|F2' (found in several files), 'user:short' (fewer guest bytes than a search needs), 'kernel:?'
        or 'user:?' (not found)."""
        g = self.insn(info, off)
        if (key, g) in self.sym_of: return self.sym_of[(key, g)]
        rip, _, ctx = key
        if privilege(ctx, rip, True) == 'kernel':
            a = rip + (g or 0) - self.slide
            s = self.kernel.lookup(a) if self.kernel else None
            name = f'kernel:{s[0]}' if s else 'kernel:module' if self.kernel and self.kernel.module(a) else 'kernel:?'
        else:
            m = self.user.get(key)
            if m is None: name = 'user:?'
            elif m == 'short': name = 'user:short'
            elif m[0] == 'ambiguous': name = 'ambiguous:' + '|'.join(sample_symbols.basename(f) for f in m[1][:3])
            else:
                elf, va = m
                name = f'{sample_symbols.basename(elf.name)}:{elf.lookup(va + (g or 0)) or "?"}'
        self.sym_of[(key, g)] = name
        return name

    def words(self, t, key, items=None):
        """[[native offset, role, samples]] of one block in a phase, in offset order (a probe's samples apart)."""
        info = self.info(key)
        acc = collections.Counter()
        for off, probe, n in (items if items is not None else [(o, p, n) for (k, o, p), n in t.words.items() if k == key]):
            acc[(NO_OFFSET if off is None else off, roles_mod.role_at(info['roles'] if info else None, off, probe))] += n
        return [[None if o == NO_OFFSET else o, r, n] for (o, r), n in sorted(acc.items())]

    def phase(self, t, el0, top_keys):
        """A phase's block samples by role (privilege/mode apart), by group, the section-7 shares (% of the phase's EL0
        samples; the hot-50 share of the top blocks' samples) and the top 30 symbols."""
        by = collections.defaultdict(collections.Counter)
        hot, rep, sym, bins = collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter()
        forms = collections.Counter()
        skid, skid_hot = collections.Counter(), collections.Counter()
        wide_libc = beyond = unresolved = 0
        for (key, off, probe), n in t.words.items():
            info = self.info(key)
            role = roles_mod.role_at(info['roles'] if info else None, off, probe)
            if role == 'beyond': beyond += n
            by[f'{privilege(key[2], key[0], True)}/{mode(key[2])}'][role] += n
            if key in top_keys: hot[roles_mod.GROUPS[role]] += n
            moved = self.skid_role(key, info, off, probe, role)
            if role == 'exit_literal' and moved == role: unresolved += n
            skid[moved] += n
            if key in top_keys: skid_hot[roles_mod.GROUPS[moved]] += n
            g = self.insn(info, off)
            if info and g in info['rep']: rep[info['rep'][g]] += n
            if info and g in info.get('rep_form', {}):
                forms[f"{info['rep_form'][g]} {mode(key[2])}-bit {privilege(key[2], key[0], True)}"] += n
            s = self.symbol(key, info, off)
            sym[s] += n
            bins[s.split(':')[0]] += n
            if s.startswith('libc.so.6:') and info and info['wide']: wide_libc += n
        allr = collections.Counter()
        for c in by.values(): allr.update(c)
        groups = collections.Counter()
        for r, n in allr.items(): groups[roles_mod.GROUPS[r]] += n
        pct = lambda n: round(100 * n / el0, 2) if el0 else None
        avx2_named = sum(n for s, n in sym.items() if sample_symbols.AVX2_NAME.search(s))
        kcopy = {s[7:]: n for s, n in sym.items() if s.startswith('kernel:') and s[7:] in sample_symbols.KERNEL_COPY}
        hot_total = sum(hot.values())
        shares = dict(
            split_checks_and_probes=pct(allr['split_check'] + allr['split_stub'] + allr['probe']), stack_checks=pct(allr['stack_check']),
            rep_string=pct(sum(rep.values())), rep_movs=pct(rep['movs']), rep_stos=pct(rep['stos']),
            glibc_avx2_evex_named=pct(avx2_named), libc_wide_vector_blocks=pct(wide_libc), flag_words=pct(allr['flags']),
            load_then_move=pct(allr['load_move']), kernel_copy_clear=pct(sum(kcopy.values())),
            exits=pct(allr['exit_literal'] + allr['exit_computed']), x87_run=pct(allr['x87_run']),
            fex_body=pct(groups['fex_body']), fex_flags=pct(groups['fex_flags']), natemu=pct(groups['natemu']),
            hot50_fex_body_pct=round(100 * hot['fex_body'] / hot_total, 2) if hot_total else None,
            hot50_fex_flags_pct=round(100 * hot['fex_flags'] / hot_total, 2) if hot_total else None, fex_mem=pct(allr['fex_mem']))
        shares['rep_forms'] = {f: pct(n) for f, n in forms.most_common()}
        sg = collections.Counter()
        for r, n in skid.items(): sg[roles_mod.GROUPS[r]] += n
        shares['skid'] = dict(
            fex_mem=pct(skid['fex_mem']), fex_body=pct(sg['fex_body']), fex_flags=pct(sg['fex_flags']), natemu=pct(sg['natemu']),
            split_checks_and_probes=pct(skid['split_check'] + skid['split_stub'] + skid['probe']), flag_words=pct(skid['flags']),
            x87_run=pct(skid['x87_run']),
            exits=pct(skid['exit_literal'] + skid['exit_computed']),
            hot50_fex_body_pct=round(100 * skid_hot['fex_body'] / hot_total, 2) if hot_total else None,
            exit_samples_unresolved=unresolved)
        return dict(block_samples=sum(allr.values()), el0=el0, roles={c: dict(v.most_common()) for c, v in sorted(by.items())},
                    all_roles=dict(allr.most_common()), skid_roles=dict(skid.most_common()), groups=dict(groups), shares=shares, beyond=beyond,
                    kernel_copy_clear=kcopy, rep_counts=dict(rep),
                    symbols=[dict(symbol=s, samples=n, pct=pct(n)) for s, n in sym.most_common(30)],
                    binaries=dict(bins.most_common()))


# --- phases ----------------------------------------------------------------------------------------------------------
class Phases:
    """Named windows [t0, t1) of CNTVCT ticks, possibly overlapping: `at(t)` gives the indices of those holding t."""

    def __init__(self, windows):
        self.windows = [(n, a, b) for n, a, b in windows if b > a]
        cuts = sorted({x for _, a, b in self.windows for x in (a, b)})
        self.cuts = cuts
        self.sets = [tuple(i for i, (_, a, b) in enumerate(self.windows) if a <= lo < b) for lo in cuts]

    def at(self, t):
        k = bisect.bisect_right(self.cuts, t) - 1
        return self.sets[k] if k >= 0 else ()


def read_markers(path):
    """[(ns, text)] from a host-stamped markers file (NS TAB LINE)."""
    out = []
    for line in pathlib.Path(path).read_text(errors='replace').splitlines():
        ns, _, text = line.partition('\t')
        if ns.strip().isdigit(): out.append((int(ns), text.strip()))
    return out


def marker_windows(markers, end_ns):
    """Phases from the runner's markers, in ns: tools/bench/run.py's NATEMU-BENCH lines (boot, run1, run1/NAME, between, run2,
    run2/NAME, after) or run_io5.py's `IO5 step NAME STATUS SECONDS` lines (step/NAME, ending at its line)."""
    out, bench = [], {}
    phase_start = {}
    for ns, text in markers:
        if text.startswith('NATEMU-BENCH-'):
            words = text[len('NATEMU-BENCH-'):].split()
            tag = words[0] if words else ''
            run = 'run2' if tag.endswith('2') else 'run1'
            if tag in ('START', 'START2'):
                bench.setdefault(tag, ns); phase_start[run] = ns
            elif tag in ('END', 'END2'):
                bench.setdefault(tag, ns)
            elif tag in ('PHASE', 'PHASE2'):
                name = next((w[5:] for w in words if w.startswith('name=')), '?')
                if run in phase_start: out.append((f'{run}/{name}', phase_start[run], ns))
                phase_start[run] = ns
        elif text.startswith('IO5 step '):
            m = text.split()
            try: secs = float(m[4])
            except (IndexError, ValueError): continue
            out.append((f'step/{m[2]}', ns - int(secs * 1e9), ns))
    if 'START' in bench:
        out.insert(0, ('boot', 0, bench['START']))
        if 'END' in bench: out.insert(1, ('run1', bench['START'], bench['END']))
        if 'END' in bench and 'START2' in bench: out.append(('between', bench['END'], bench['START2']))
        if 'START2' in bench and 'END2' in bench: out.append(('run2', bench['START2'], bench['END2']))
        last = bench.get('END2') or bench.get('END')
        if last: out.append(('after', last, end_ns))
    return out


def snaps(perf):
    """The timing log's `snap NS v0b0 v0b1 ...` lines: [(ns, [per-vCPU-bucket ns...])]."""
    out = []
    for line in open(perf, errors='replace'):
        if line.startswith('snap '):
            w = line.split()
            out.append((int(w[1]), list(map(int, w[2:]))))
    return out


def bucket_names(root=ROOT):
    """The resident's perf bucket names, in order (natemu_abi::execution::perf::NAMES)."""
    text = (root / 'crates/abi/src/execution.rs').read_text()
    m = re.search(r'pub const NAMES: \[&str; BUCKETS\] = \[(.*?)\];', text, re.S)
    return re.findall(r'"(\w+)"', m[1])


def buckets_between(snap_lines, t0, t1, names):
    """Seconds per bucket summed over vCPUs between the last snapshots at or before t0 and t1 (ns; before the first snapshot
    every bucket is 0), or None when no snapshot lies between them."""
    times = [t for t, _ in snap_lines]
    i, j = bisect.bisect_right(times, t0) - 1, bisect.bisect_right(times, t1) - 1
    if j < 0 or j <= i: return None
    b, n = snap_lines[j][1], len(names)
    a = snap_lines[i][1] if i >= 0 else [0] * len(b)
    return {k: sum(b[v * n + x] - a[v * n + x] for v in range(len(a) // n)) / 1e9 for x, k in enumerate(names)}


# --- the samples file ------------------------------------------------------------------------------------------------
class Tally:
    """Counts of one (vCPU or all, phase)."""
    __slots__ = ('kinds', 'places', 'privilege', 'modes', 'calls', 'flags', 'since', 'blocks', 'words', 'forms', 'tails')

    def __init__(self, blocks=False):
        self.kinds, self.places, self.privilege, self.modes = (collections.Counter() for _ in range(4))
        self.forms, self.tails = collections.Counter(), collections.Counter()
        self.calls, self.flags, self.since = collections.Counter(), collections.Counter(), collections.Counter()
        self.blocks = collections.Counter() if blocks else None
        self.words = collections.Counter() if blocks else None


def since_bucket(ticks, freq):
    """log2 microseconds, 0 for under 1 us."""
    us = ticks * 1_000_000 // freq
    return 0 if us < 1 else us.bit_length()


class Profile:
    """One NAME.samples (and NAME.objects) read: header, clocks, toggles, marks, passes, counters, and the tallies."""

    def __init__(self, name, markers=None, perf=None, window_s=1.0):
        self.name = pathlib.Path(name)
        self.problems, self.notes = [], []
        path = self.name.with_name(self.name.name + '.samples')
        if not path.exists(): raise FormatError(f'{path}: missing')
        f = open(path, 'rb')
        size = path.stat().st_size
        data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) if size else b''
        f.close()
        if size % RECORD_BYTES: self.problems.append(f'{path.name}: {size} bytes, not a whole number of records')
        if size < RECORD_BYTES: raise FormatError(f'{path}: no header')
        size -= size % RECORD_BYTES
        h = REC.unpack_from(data, 0)
        if h[0] != FILE_MAGIC or h[1] & 0xffff_ffff != VERSION or h[1] >> 32 != RECORD_BYTES or h[2] >> 60:
            raise FormatError(f'{path}: not a version {VERSION} samples file')
        self.freq, self.vcpus, self.interval_us = h[2], h[3] & 0xffff_ffff, h[3] >> 32
        if not self.freq: raise FormatError(f'{path}: frequency 0')
        view = memoryview(data)[RECORD_BYTES:size]
        # Pass 1: the host's records (clock pairs, toggles, marks, passes, counters).
        self.clocks, self.toggles, self.marks, self.passes = [], [], [], []
        self.counters = collections.defaultdict(dict)
        for w in REC.iter_unpack(view):
            k = w[2] >> 60
            if k == CLOCK: self.clocks.append(host(w))
            elif k == TOGGLE: self.toggles.append(host(w))
            elif k == MARK: self.marks.append(host(w))
            elif k == PASS: self.passes.append(host(w))
            elif k == COUNT:
                r = host(w)
                self.counters['run' if r['value3'] == RUN else r['value3']][
                    COUNTERS[r['value4']] if r['value4'] < len(COUNTERS) else f'counter{r["value4"]}'] = r['value1']
        if not self.clocks: raise FormatError(f'{path}: no clock record')
        self.origin, self.end = self.clocks[0]['time'], max(c['time'] for c in self.clocks)
        self.wall0 = self.clocks[0]['value1']
        # Phases, in ticks.
        windows = [('all', 0, 2 ** 64)]
        self.markers_file = markers
        if markers:
            if pathlib.Path(markers).exists():
                tick = lambda ns: ns * self.freq // 1_000_000_000
                for n, a, b in marker_windows(read_markers(markers), self.end * 1_000_000_000 // self.freq):
                    windows.append((n, tick(a) if a else self.origin, tick(b)))
            else: self.notes.append(f'no markers file {markers}')
        marks = sorted(self.marks, key=lambda r: r['time'])
        for i, m in enumerate(marks):
            windows.append((f'mark/{m["value1"]}', m['time'], marks[i + 1]['time'] if i + 1 < len(marks) else self.end + 1))
        self.phases = Phases(windows)
        self.window_ticks = max(1, int(window_s * self.freq))
        self.perf = perf
        # Pass 2: the vCPUs' records, tallied per (vCPU, phase), summed per phase (with blocks), and per fixed window.
        P = len(self.phases.windows)
        self.per = {(v, p): Tally() for v in range(self.vcpus) for p in range(P)}
        self.sum = [Tally(blocks=True) for _ in range(P)]
        self.series = collections.defaultdict(collections.Counter)
        self.records = collections.Counter()        # per vCPU: ring records found after its VCPU records
        pending, vcpu, bad = 0, 0, 0
        origin, wt, freq, phases_at = self.origin, self.window_ticks, self.freq, self.phases.at
        per, summ, series = self.per, self.sum, self.series
        for w in REC.iter_unpack(view):
            k = w[2] >> 60
            if pending:
                pending -= 1
                self.records[vcpu] += 1
                if k not in (EL0, RESIDENT): bad += 1; continue
                if vcpu >= self.vcpus: continue
                t = w[0]
                ps = phases_at(t)
                win = (t - origin) // wt if t >= origin else -1
                if k == RESIDENT:
                    for p in ps: per[(vcpu, p)].kinds['resident'] += 1; summ[p].kinds['resident'] += 1
                    series[win]['resident'] += 1
                    continue
                place = w[2] >> 56 & 0xf
                flags = w[1] >> 60
                context = w[2] & ((1 << CONTEXT_BITS) - 1)
                rip = w[1] & ((1 << RIP_BITS) - 1)
                if rip >> (RIP_BITS - 1): rip |= ~((1 << RIP_BITS) - 1) & (2 ** 64 - 1)
                pname = PLACES.get(place, f'place{place}')
                inblock = place in (BLOCK, PROBE, STEP)
                if inblock or place in (DISPATCH, TAIL):
                    has_ctx = inblock or context != 0     # a dispatcher sample has its slot's context, or 0 for none
                    priv = privilege(context, rip, has_ctx)
                    md = mode(context) if has_ctx else None
                else: priv, md = 'unknown', None
                call = CALLS.get(flags >> CALL_SHIFT & 3) if place == DISPATCH else None
                at_ = w[2] >> 40 & 0xffff
                form = slot_form(at_) if place == DISPATCH else None
                role = tail_role(at_) if place == TAIL else None
                sb = since_bucket(w[3] >> 32, freq)
                key = (rip, w[3] & 0xffff_ffff, context) if inblock else None
                for p in ps:
                    for t_ in (per[(vcpu, p)], summ[p]):
                        t_.kinds['el0'] += 1; t_.places[pname] += 1; t_.privilege[priv] += 1; t_.since[sb] += 1
                        if md: t_.modes[md] += 1
                        if call: t_.calls[call] += 1
                        if form: t_.forms[form] += 1
                        if role: t_.tails[role] += 1
                        if inblock:
                            if flags & HOT: t_.flags['hot'] += 1
                            if flags & DEAD: t_.flags['dead'] += 1
                            if flags & FAR: t_.flags['far'] += 1
                    if key:
                        summ[p].blocks[key] += 1
                        summ[p].words[(key, None if flags & FAR else (w[2] >> 40 & 0xffff) - AT_BIAS, place == PROBE)] += 1
                s = series[win]; s['el0'] += 1; s[pname] += 1
                continue
            if k == VCPU:
                r = host(w)
                vcpu, pending = r['value3'], r['value4']
                if vcpu >= self.vcpus: self.problems.append(f'records of vCPU {vcpu}: the header says {self.vcpus} vCPUs')
            elif k == HALTED:
                r = host(w); v = r['value3']
                for p in phases_at(r['time']):
                    if (v, p) in per: per[(v, p)].kinds['halted'] += 1
                    summ[p].kinds['halted'] += 1
                series[(r['time'] - origin) // wt if r['time'] >= origin else -1]['halted'] += 1
        if pending: self.problems.append(f'the file ends {pending} records into vCPU {vcpu}\'s batch')
        if bad: self.problems.append(f'{bad} ring records of a kind that is not EL0 or resident')
        self.check()

    # -- derived ------------------------------------------------------------------------------------------------------
    def on_intervals(self):
        """[(t0, t1)] in ticks while sampling was on, from the toggles (the start's included) to the last clock."""
        out, on_at = [], None
        for t in sorted(self.toggles, key=lambda r: (r['time'], r['value1'])):
            if t['value2'] and on_at is None: on_at = t['time']
            elif not t['value2'] and on_at is not None: out.append((on_at, t['time'])); on_at = None
        if on_at is not None: out.append((on_at, self.end))
        return out

    def on_seconds(self, a=0, b=2 ** 64):
        return sum(max(0, min(y, b) - max(x, a)) for x, y in self.on_intervals()) / self.freq

    def check(self):
        if not self.counters: self.problems.append('no counters: the run did not end (natemu killed?)'); return
        for v in range(self.vcpus):
            c = self.counters.get(v)
            if c is None:
                if self.counters['run'].get('not_sampling', 0) == 0: self.problems.append(f'vCPU {v}: no counters')
                continue
            if c.get('drained') != self.records[v]:
                self.problems.append(f'vCPU {v}: {self.records[v]} ring records in the file, the host drained {c.get("drained")}')
            if c.get('written', 0) != c.get('drained', 0):     # the last drain runs after the vCPUs stopped: nothing is left
                self.problems.append(f'vCPU {v}: {c.get("written", 0) - c.get("drained", 0)} records written after the last drain')
        run = self.counters['run']
        if run.get('port_ignored'): self.problems.append(f'the control port ignored {run["port_ignored"]} accesses (a stray port access)')
        if run.get('not_sampling'): self.problems.append(f'{run["not_sampling"]} vCPU(s) never rung: the resident does not sample')
        if not self.sum[0].kinds['el0']: self.problems.append('no EL0 sample')

    def shares(self, t: Tally):
        total = sum(t.kinds.values())
        el0 = t.kinds['el0']
        pct = lambda n, d: round(100 * n / d, 2) if d else None
        return dict(
            samples=total, kinds={k: t.kinds[k] for k in ('el0', 'resident', 'halted')},
            kind_pct={k: pct(t.kinds[k], total) for k in ('el0', 'resident', 'halted')},
            el0_places={n: t.places[n] for n in PLACES.values()}, el0_place_pct={n: pct(t.places[n], el0) for n in PLACES.values()},
            privilege=dict(t.privilege), privilege_pct={k: pct(n, el0) for k, n in t.privilege.items()},
            modes=dict(t.modes), dispatch_calls={k: t.calls[k] for k in ('return', 'not_call', 'unknown')},
            dispatch_call_pct={k: pct(t.calls[k], t.places['dispatch']) for k in ('return', 'not_call', 'unknown')},
            dispatch_forms={k: t.forms[k] for k in FORMS}, tail_roles=dict(sorted(t.tails.items())),
            hot=t.flags['hot'], dead=t.flags['dead'], far=t.flags['far'],
            since_log2_us={str(k): n for k, n in sorted(t.since.items())})

    def attribute(self, attribution, p, d, top):
        """Phase p's roles and symbols into its dict d; the whole run's top blocks get their words' roles and a symbol."""
        t = self.sum[p]
        ranked = [k for k, _ in t.blocks.most_common(top)]
        if p == 0:
            attribution.set_reach(t.words)
            attribution.match_user(t.blocks)
            by_key = collections.defaultdict(list)
            for (k, off, probe), n in t.words.items(): by_key[k].append((off, probe, n))
            for row, k in zip(d['blocks']['top'], ranked):
                row['words'] = attribution.words(t, k, by_key[k])
                busiest = max(row['words'], key=lambda w: w[2])[0] if row['words'] else None
                row['symbol'] = attribution.symbol(k, attribution.info(k), busiest)
        d['attribution'] = a = attribution.phase(t, t.kinds['el0'], set(ranked))
        if p == 0:
            if a['beyond']: self.problems.append(f'{a["beyond"]} block samples past their object\'s code region')
            a['user_blocks_looked_up'], a['user_blocks_found'] = attribution.looked_up, attribution.matched
            a['user_blocks_ambiguous'] = attribution.ambiguous
            a['user_blocks_short'], a['user_blocks_twice_in_a_file'] = attribution.short, attribution.multi_in_file

    def report(self, objects=None, top=50, attribution=None):
        names = [n for n, _, _ in self.phases.windows]
        snap_lines = names_b = None
        if self.perf and pathlib.Path(self.perf).exists():
            snap_lines, names_b = snaps(self.perf), bucket_names()
        elif self.perf: self.notes.append(f'no timing log {self.perf}')
        phases = []
        for p, (name, a, b) in enumerate(self.phases.windows):
            lo, hi = max(a, self.origin), min(b, self.end)
            d = dict(name=name, start_s=round((lo - self.origin) / self.freq, 3), end_s=round((hi - self.origin) / self.freq, 3),
                     summed=self.shares(self.sum[p]),
                     vcpus={v: self.shares(self.per[(v, p)]) for v in range(self.vcpus)})
            on = self.on_seconds(a, b)
            d['on_s'] = round(on, 3)
            d['samples_per_s'] = {v: round(d['vcpus'][v]['samples'] / on, 1) if on else None for v in range(self.vcpus)}
            if snap_lines:
                ns = lambda t: t * 1_000_000_000 // self.freq
                bk = buckets_between(snap_lines, ns(lo), ns(hi), names_b)
                if bk:
                    busy = sum(bk.values()) - bk.get('halted', 0)
                    d['buckets_s'] = {k: round(v, 3) for k, v in sorted(bk.items(), key=lambda kv: -kv[1]) if v >= 0.0005}
                    d['guest_share_of_busy'] = round(bk.get('guest', 0) / busy, 4) if busy else None
            d['blocks'] = self.blocks(self.sum[p], objects, top)
            if attribution is not None: self.attribute(attribution, p, d, top)
            phases.append(d)
        counters = {str(k): v for k, v in self.counters.items()}
        for v in range(self.vcpus):
            c = self.counters.get(v)
            if c: c['merged'] = c.get('rings', 0) - c.get('bells', 0) - c.get('bells_in_wait', 0)
        series = [dict(t_s=round(k * self.window_ticks / self.freq, 3), **dict(c)) for k, c in sorted(self.series.items()) if k >= 0]
        passes = [dict(start_s=round((r['time'] - self.origin) / self.freq, 6), end_s=round((r['value1'] - self.origin) / self.freq, 6),
                       seq_start=r['value2'], seq_end=r['value3'], rung=r['value4']) for r in self.passes]
        toggles = [dict(t_s=round((r['time'] - self.origin) / self.freq, 6), seq=r['value1'], on=bool(r['value2']),
                        by=BY.get(r['value3'], r['value3']), vcpu=None if r['value4'] == RUN else r['value4']) for r in self.toggles]
        marks = [dict(t_s=round((r['time'] - self.origin) / self.freq, 6), mark=r['value1'], vcpu=r['value3']) for r in self.marks]
        return dict(profile=str(self.name), frequency=self.freq, vcpus=self.vcpus, interval_us=self.interval_us,
                    requested_per_s=round(1e6 / self.interval_us, 1) if self.interval_us else None,
                    duration_s=round((self.end - self.origin) / self.freq, 3), on_s=round(self.on_seconds(), 3),
                    objects=objects.count if objects else None, counters=counters, phases=phases, toggles=toggles,
                    marks=marks, passes=passes, window_s=round(self.window_ticks / self.freq, 3), series=series,
                    problems=self.problems, notes=self.notes)

    def blocks(self, t: Tally, objects, top):
        """The top blocks of a phase, and the cumulative curve of its block samples."""
        total = sum(t.blocks.values())
        ranked = t.blocks.most_common()
        rows, cum = [], 0
        for (rip, frame, context), n in ranked[:top]:
            cum += n
            row = dict(rip=hex(rip), frame=hex(frame), context=hex(context), samples=n, pct=round(100 * n / total, 2),
                       cum_pct=round(100 * cum / total, 2), privilege=privilege(context, rip, True), mode=mode(context))
            if objects is not None:
                hits, how = objects.find(rip, frame, context)
                row['object'] = how
                if hits:
                    r = objects.record(hits[0])
                    row['candidates'] = len(hits)
                    row['guest_bytes'] = r['guest'][:16].hex()
                    if r['code']: row.update(guest_insns=r['code']['guest_insns'], code_size=r['code']['code_size'])
            rows.append(row)
        curve, cum, need = {}, 0, {}
        marks = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000]
        targets = [50, 80, 90, 95, 99]
        for i, (_, n) in enumerate(ranked, 1):
            cum += n
            if i in marks: curve[i] = round(100 * cum / total, 2)
            for g in targets:
                if g not in need and 100 * cum >= g * total: need[g] = i
        return dict(distinct=len(ranked), samples=total, top=rows, cum_pct_at=curve, blocks_for_pct=need)


# --- the Markdown --------------------------------------------------------------------------------------------------------
def markdown(rep):
    p = lambda x: '' if x is None else f'{x:.1f}'
    out = [f'# Samples: `{pathlib.Path(rep["profile"]).name}`', '',
           f'{rep["vcpus"]} vCPU(s), every {rep["interval_us"]} us requested ({rep["requested_per_s"]}/s per vCPU), '
           f'{rep["duration_s"]} s, sampling on {rep["on_s"]} s; {rep["objects"]} objects. A profile, not a timing.', '',
           '## By phase (all vCPUs)', '',
           '| phase | s | samples | EL0 % | resident % | halted % | block | probe | dispatch | tail | step | head | no block | outside | user % | kernel % | ret % | not call % | unknown % |',
           '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for ph in rep['phases']:
        s = ph['summed']; e = s['el0_place_pct']; c = s['dispatch_call_pct']; k = s['kind_pct']; pr = s['privilege_pct']
        out.append(f'| {ph["name"]} | {ph["end_s"] - ph["start_s"]:.1f} | {s["samples"]} | {p(k["el0"])} | {p(k["resident"])} | '
                   f'{p(k["halted"])} | {p(e["block"])} | {p(e["probe"])} | {p(e["dispatch"])} | {p(e["tail"])} | {p(e["step"])} | {p(e["head"])} | '
                   f'{p(e["no_block"])} | {p(e["outside"])} | {p(pr.get("user"))} | {p(pr.get("kernel"))} | {p(c["return"])} | '
                   f'{p(c["not_call"])} | {p(c["unknown"])} |')
    out += ['', 'EL0 places are % of EL0 samples; the call test (return, not a call, unknown) is % of dispatch samples; tail: the words',
            'appended to a block for an indirect jump or call or a shared block\'s checked link (indirect plan step 1).', '']
    whole_s = rep['phases'][0]['summed']
    out += [f"Dispatch samples by slot form: {whole_s['dispatch_forms']}; tail samples by role: {whole_s['tail_roles'] or 'none'}.", '',
            '## Per vCPU (whole run)', '', '| vCPU | samples | per s | EL0 % | resident % | halted % | rings | bells | in wait | merged | lost | written | drained |',
            '| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    whole = rep['phases'][0]
    for v, s in whole['vcpus'].items():
        c = rep['counters'].get(str(v), {})
        k = s['kind_pct']
        out.append(f'| {v} | {s["samples"]} | {whole["samples_per_s"][v]} | {p(k["el0"])} | {p(k["resident"])} | {p(k["halted"])} | '
                   f'{c.get("rings")} | {c.get("bells")} | {c.get("bells_in_wait")} | {c.get("merged")} | {c.get("lost")} | '
                   f'{c.get("written")} | {c.get("drained")} |')
    run = rep['counters'].get('run', {})
    out += ['', f'Control port: {run.get("port_accesses")} accesses, {run.get("port_ignored")} ignored. '
            f'Merged: rings not taken as a bell (rings - bells - bells in wait).', '']
    b = whole['blocks']
    out += ['## Top blocks (whole run)', '',
            f'{b["distinct"]} distinct blocks, {b["samples"]} block samples (block, probe, step). Cumulative: '
            + ', '.join(f'top {n}: {v}%' for n, v in b['cum_pct_at'].items()) + '; blocks for '
            + ', '.join(f'{g}%: {n}' for g, n in b['blocks_for_pct'].items()) + '.', '',
            '| # | RIP | frame | context | samples | % | cum % | priv | mode | object | insns | guest bytes |',
            '| ---: | --- | --- | --- | ---: | ---: | ---: | --- | --- | --- | ---: | --- |']
    for i, r in enumerate(b['top'], 1):
        out.append(f'| {i} | `{r["rip"]}` | `{r["frame"]}` | `{r["context"]}` | {r["samples"]} | {r["pct"]} | {r["cum_pct"]} | '
                   f'{r["privilege"]} | {r["mode"]} | {r.get("object") or "none"} | {r.get("guest_insns", "")} | `{r.get("guest_bytes", "")}` |')
    if 'attribution' in whole: out += attribution_markdown(rep)
    if rep['problems'] or rep['notes']:
        out += ['', '## Problems and notes', ''] + [f'- problem: {x}' for x in rep['problems']] + [f'- note: {x}' for x in rep['notes']]
    return '\n'.join(out) + '\n'


def attribution_markdown(rep):
    p = lambda x: '' if x is None else f'{x:.1f}'
    whole = rep['phases'][0]
    out = ['', '## Roles and symbols by phase', '',
           'Shares are % of the phase\'s EL0 samples; hot 50: FEX\'s body as % of the samples on the phase\'s top 50 blocks. '
           'AVX2/EVEX named: samples on a symbol named *_avx2* or *_evex*; libc wide: samples in libc.so.6 blocks that hold a '
           'VEX.256 or EVEX instruction (libc.so.6 is stripped: its string functions have no names).', '',
           '| phase | block samples | FEX body | FEX flags | natemu | split + probe | stack | load-move | exits | x87 run | REP str | '
           'AVX2/EVEX named | libc wide | kernel copy | hot 50 FEX body |',
           '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for ph in rep['phases']:
        a = ph.get('attribution')
        if not a: continue
        s = a['shares']
        out.append(f'| {ph["name"]} | {a["block_samples"]} | {p(s["fex_body"])} | {p(s["fex_flags"])} | {p(s["natemu"])} | '
                   f'{p(s["split_checks_and_probes"])} | {p(s["stack_checks"])} | {p(s["load_then_move"])} | {p(s["exits"])} | '
                   f'{p(s["x87_run"])} | '
                   f'{p(s["rep_string"])} | {p(s["glibc_avx2_evex_named"])} | {p(s["libc_wide_vector_blocks"])} | '
                   f'{p(s["kernel_copy_clear"])} | {p(s["hot50_fex_body_pct"])} |')
    out += ['', 'REP strings by form (prefix, opcode and width, code mode, privilege), % of the phase\'s EL0 samples:', '']
    for ph in rep['phases']:
        a = ph.get('attribution')
        if a and a['shares'].get('rep_forms'):
            out.append(f'- {ph["name"]}: ' + ', '.join(f'`{f}` {p(v)}' for f, v in a['shares']['rep_forms'].items()))
    out += ['', '### One-word skid correction', '',
            'Each sample also counted on the next word executed (the next word of its block, or a linked exit\'s target '
            'block\'s first body word; sample_roles.NOT_MOVED: the validity case\'s chase phase put 58% of a slow load\'s '
            'samples on the word before it): % of EL0 samples, raw / corrected.', '',
            '| phase | fex_mem | FEX body | FEX flags | natemu | split + probe | exits | x87 run | hot 50 FEX body | exit samples unresolved |',
            '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for ph in rep['phases']:
        a = ph.get('attribution')
        if not a: continue
        s, k = a['shares'], a['shares']['skid']
        rc = lambda x: f'{p(s[x])} / {p(k[x])}'
        out.append(f'| {ph["name"]} | {rc("fex_mem")} | {rc("fex_body")} | {rc("fex_flags")} | {rc("natemu")} | '
                   f'{rc("split_checks_and_probes")} | {rc("exits")} | {rc("x87_run")} | {p(s["hot50_fex_body_pct"])} / {p(k["hot50_fex_body_pct"])} | '
                   f'{k["exit_samples_unresolved"]} |')
    a = whole['attribution']
    classes = sorted(a['roles'])
    total = a['block_samples']
    out += ['', '## Block samples by role (whole run)', '', '| role | group | ' + ' | '.join(classes) + ' | % of block samples |',
            '| --- | --- | ' + ' | '.join('---:' for _ in classes) + ' | ---: |']
    for r, n in a['all_roles'].items():
        out.append(f'| {r} | {roles_mod.GROUPS[r]} | ' + ' | '.join(str(a['roles'][c].get(r, 0)) for c in classes)
                   + f' | {100 * n / total:.1f} |')
    out += ['', f'User blocks looked up in the image: {a.get("user_blocks_looked_up")}; found in one file '
            f'{a.get("user_blocks_found")} ({a.get("user_blocks_twice_in_a_file")} of them twice in it: named by the first), in '
            f'several files {a.get("user_blocks_ambiguous")} (`ambiguous:`), not looked up (under '
            f'{sample_symbols.MATCH_BYTES} guest bytes) {a.get("user_blocks_short")} (`user:short`). A bulk REP STOS loop '
            '(--bulk, kernel code) counts as FEX body; its guest instruction counts in REP str.',
            '', '## Top blocks\' words (whole run)', '',
            'Per block: its symbol, then each sampled word as native offset (hex), role and samples.', '']
    for i, r in enumerate(whole['blocks']['top'], 1):
        ws = ', '.join(f'{"far" if o is None else hex(o)} {role} {n}' for o, role, n in r.get('words', []))
        out.append(f'{i}. `{r["rip"]}` {r.get("symbol", "")}: {ws}')
    shown = [ph for ph in rep['phases'] if ph.get('attribution') and not ph['name'].startswith('mark/')]
    if len(shown) <= 1: shown = [ph for ph in rep['phases'] if ph.get('attribution')]
    for ph in shown:
        a = ph['attribution']
        out += ['', f'## Top 30 symbols: {ph["name"]}', '', '| symbol | samples | % of EL0 |', '| --- | ---: | ---: |']
        out += [f'| `{x["symbol"]}` | {x["samples"]} | {p(x["pct"])} |' for x in a['symbols']]
        if a['kernel_copy_clear']:
            out += ['', 'Kernel copy and clear routines: ' + ', '.join(f'{k} {n}' for k, n in sorted(a['kernel_copy_clear'].items(), key=lambda kv: -kv[1]))]
    return out


def attribution_for(objects, markers=None, kernel=None, image=None, notes=None):
    """The Attribution of one profile: the kernel's symbols with the slide from its markers, the image's binaries."""
    notes = notes if notes is not None else []
    slide = 0
    if kernel is not None and markers and pathlib.Path(markers).exists():
        t, note = sample_symbols.ktext(markers)
        if note: notes.append(note)
        elif kernel.text is not None: slide = t - kernel.text
    elif kernel is not None: notes.append('no markers: no KASLR slide assumed')
    if kernel is None: notes.append('no --kernel-symbols: kernel samples stay at block level')
    if image is None: notes.append('no --image: user samples stay at block level')
    return Attribution(objects, kernel, slide, image)


def report_one(name, markers=None, perf=None, window_s=1.0, require=(), kernel=None, image=None):
    """Reads NAME.samples and NAME.objects, writes NAME.report.json and NAME.report.md; the problems. `kernel` a
    sample_symbols.Kallsyms, `image` a sample_symbols.Image (both optional)."""
    name = pathlib.Path(name)
    try:
        prof = Profile(name, markers, perf, window_s)
        objects_path = name.with_name(name.name + '.objects')
        objects = Objects(objects_path) if objects_path.exists() else None
        if objects is None: prof.problems.append(f'{objects_path.name}: missing')
    except (FormatError, OSError) as e:
        return [str(e)]
    rep = prof.report(objects, attribution=attribution_for(objects, markers, kernel, image, prof.notes))
    whole = rep['phases'][0]['summed']['el0_places']
    for place in require:
        if not whole.get(place): rep['problems'].append(f'no {place} sample (--require-place {place})')
    name.with_name(name.name + '.report.json').write_text(json.dumps(rep, indent=1) + '\n')
    name.with_name(name.name + '.report.md').write_text(markdown(rep))
    s = rep['phases'][0]['summed']
    print(f'{name.name}: {s["samples"]} samples, EL0 {s["kind_pct"]["el0"]}%, dispatch {s["el0_place_pct"]["dispatch"]}% of EL0, '
          f'{rep["phases"][0]["blocks"]["distinct"]} blocks; {name.name}.report.md', flush=True)
    return rep['problems']


def main(argv):
    command = []
    if '--' in argv:
        i = argv.index('--'); argv, command = argv[:i], argv[i + 1:]
    profiles, require, window, kernel_path, image_path, boots = [], [], 1.0, None, None, None
    it = iter(argv)
    for a in it:
        if a == '--profile': profiles.append({'name': next(it)})
        elif a in ('--markers', '--perf'):
            if not profiles: sys.exit(f'{a} before any --profile')
            profiles[-1][a[2:]] = next(it)
        elif a == '--window': window = float(next(it))
        elif a in ('--kernel-symbols', '--kernel'): kernel_path = next(it)
        elif a == '--boots': boots = next(it)
        elif a == '--image': image_path = next(it)
        elif a == '--require-place':
            place = next(it)
            if place not in PLACES.values(): sys.exit(f'--require-place: one of {", ".join(PLACES.values())}')
            require.append(place)
        else: sys.exit(f'unknown argument {a!r}\n\n{__doc__}')
    if not profiles: sys.exit(__doc__)
    code = 0
    if command:
        print('running:', ' '.join(command), flush=True)
        code = subprocess.run(command, cwd=ROOT).returncode
    problems, kernel, image = [], None, None
    try:
        if kernel_path: kernel = sample_symbols.kernel_symbols(kernel_path)
        if kernel is not None and boots and (got := sample_symbols.sha256(boots)) != kernel.bzimage_sha256:
            problems.append(f'symbols: the run booted {boots} (sha256 {got[:16]}), the symbols are of {str(kernel.bzimage_sha256)[:16]}')
        if image_path: image = sample_symbols.Image(image_path)
    except (OSError, RuntimeError, struct.error, subprocess.CalledProcessError) as e:
        problems.append(f'symbols: {e}')
    for p in profiles:
        problems += [f'{pathlib.Path(p["name"]).name}: {x}'
                     for x in report_one(p['name'], p.get('markers'), p.get('perf'), window, require, kernel, image)]
    for x in problems: print('problem:', x, flush=True)
    return code if code else (1 if problems else 0)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main(sys.argv[1:]))
