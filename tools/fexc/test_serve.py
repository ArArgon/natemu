#!/usr/bin/env python3
"""fexc --serve against the one-shot CLI: same inputs, byte-identical object and JSON (S2a).

    test_serve.py FEXC [--mode fork|fresh|reuse] [--cli FEXC] [--corpus PROGRAMS.json]... [--time]

Inputs: the 64 fixture programs, every --corpus (gen_slice1.py format, e.g. `--seed 4242 --random 20` = 44 programs) and the
scan_skips.py memory-form matrix, each under profile slice1 and default, plus 32-bit, --max-insts, --feature, --config and
error requests. One server process answers all of them, then the whole list again in reverse order (same RIPs with
other bytes in between: a reused context would answer from its cache), then an adversarial set at one RIP (several byte
strings x settings) in six shuffled orders. Exit status 1 on any difference.
--time prints mean/p95 compile latency of the CLI and of the server."""
import argparse, json, os, random, statistics, struct, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_sites, scan_skips  # noqa: E402

FIXED = ['--profile', 'slice1']


class Server:
    def __init__(self, fexc, mode):
        self.p = subprocess.Popen([fexc, '--serve', '--serve-mode', mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def compile(self, args, code):
        a = b''.join(x.encode() + b'\0' for x in args)
        body = struct.pack('<I', len(a)) + a + code
        self.p.stdin.write(struct.pack('<I', len(body)) + body)
        self.p.stdin.flush()
        n, = struct.unpack('<I', self.p.stdout.read(4))
        r = self.p.stdout.read(n)
        c, jl, ol = struct.unpack('<iII', r[:12])
        return c, r[12:12 + jl], r[12 + jl:12 + jl + ol]

    def close(self):
        self.p.stdin.close(); self.p.wait()


def cli(fexc, args, code, tmp):
    obj = tmp / 'o.bin'
    obj.unlink(missing_ok=True)
    r = subprocess.run([fexc, *args, '--hex', code.hex(), '--object', str(obj)], capture_output=True)
    if r.returncode >= 2:
        return r.returncode, r.stderr.decode().removeprefix('fexc: ').rstrip('\n').encode(), b''
    return r.returncode, r.stdout, obj.read_bytes() if obj.exists() else b''


def norm(r):
    """The reply with the one process-specific value of the JSON (the dispatcher's host address, which differs between two
    runs of the same CLI too: ASLR) blanked; the object has no such value."""
    code, js, obj = r
    if code < 2:
        d = json.loads(js)
        for x in d.get('code', {}).get('relocations', []):
            if x['kind'] == 'named_symbol_literal': x['emitted_value'] = None
        js = json.dumps(d, sort_keys=True).encode()
    return code, js, obj


def requests(corpora):
    """(label, args, bytes)"""
    progs = [(p['name'], bytes.fromhex(p['code']), int(p['rip'], 0)) for c in corpora for p in c]
    progs += [(n, bytes.fromhex(h), 0x102000) for n, h in scan_skips.matrix().items()]
    out = []
    for n, code, rip in progs:
        out.append((n + ' slice1', FIXED + ['--rip', hex(rip)], code))
        out.append((n + ' default', ['--rip', hex(rip)], code))
    code = bytes.fromhex('4801d84883c001c3')
    out += [('32-bit', ['--bitness', '32', '--rip', '0x1000'], bytes.fromhex('01d883c001c3')),
            ('max-insts', ['--max-insts', '1', '--rip', '0x1000'], code),
            ('feature', FIXED + ['--feature', 'atomics_lse=0', '--rip', '0x1000'], code),
            ('config', ['--config', 'X87REDUCEDPRECISION=1', '--rip', '0x1000'], bytes.fromhex('d9c0d9c9c3')),
            ('after config', ['--rip', '0x1000'], bytes.fromhex('d9c0d9c9c3')),
            ('syscall', FIXED + ['--rip', '0x1000'], bytes.fromhex('0f05')),
            ('bad feature', ['--feature', 'nope=1', '--rip', '0x1000'], code),
            ('bad bitness', ['--bitness', '16', '--rip', '0x1000'], code),
            ('slice1 32-bit', FIXED + ['--bitness', '32', '--rip', '0x1000'], code)]
    return out


def adversarial():
    """Everything at ONE RIP: several byte strings (a prefix of each other too) under every setting. Served in shuffled orders,
    a server that kept FEX's block cache, Config or any recorder state between requests answers some of them wrongly."""
    codes = [bytes.fromhex(h) for h in ('4801d84883c001c3', '4801d84883c001', '4801d8', '4829d84883e801c3', '480fafc3c3', '4831c04889d8c3',
                                        '488b03c3', '488903c3', 'f7f3c3', '48f7f3c3', 'd9c0d9c9c3', '0f05')]
    variants = [[], FIXED, ['--max-insts', '1'], FIXED + ['--max-insts', '1'], ['--max-insts', '2'], FIXED + ['--max-insts', '2'],
                FIXED + ['--feature', 'atomics_lse=0'], FIXED + ['--feature', 'flagm=0'], ['--feature', 'flagm=0'], ['--feature', 'x86_avx=0'],
                ['--bitness', '32'], ['--no-tso'], ['--multiblock'], ['--sparse-pc'], ['--no-end-marker'],
                ['--config', 'X87REDUCEDPRECISION=1'], FIXED + ['--config', 'X87REDUCEDPRECISION=1'], ['--config', 'MAXINST=2']]
    return [(f'adv {c.hex()} {" ".join(v)}', v + ['--rip', '0x1000'], c) for c in codes for v in variants]


def p95(v):
    return sorted(v)[int(len(v) * 0.95)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('fexc'); ap.add_argument('--mode', default='fork')
    ap.add_argument('--cli', help='fexc binary for the one-shot side (default: FEXC)')
    ap.add_argument('--corpus', action='append', default=[]); ap.add_argument('--time', action='store_true')
    a = ap.parse_args()
    corpora = [check_sites.fixture_programs()] + [json.loads(Path(c).read_text())['programs'] for c in a.corpus]
    reqs = requests(corpora)
    bad, t_cli, t_srv = 0, [], []
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        expect = []
        for label, args, code in reqs:
            t0 = time.perf_counter(); expect.append(cli(a.cli or a.fexc, args, code, tmp)); t_cli.append(time.perf_counter() - t0)
        s = Server(a.fexc, a.mode)
        for rnd, order in enumerate((range(len(reqs)), reversed(range(len(reqs))))):
            for i in order:
                label, args, code = reqs[i]
                t0 = time.perf_counter(); got = s.compile(args, code)
                if rnd == 0: t_srv.append(time.perf_counter() - t0)
                if norm(got) != norm(expect[i]):
                    bad += 1
                    if bad <= 10: print(f'DIFF round {rnd} {label}: code {got[0]}/{expect[i][0]} json {len(got[1])}/{len(expect[i][1])} object {len(got[2])}/{len(expect[i][2])} equal json {norm(got)[1] == norm(expect[i])[1]} object {got[2] == expect[i][2]}')
        adv = adversarial()
        want = [cli(a.cli or a.fexc, args, code, tmp) for _, args, code in adv]
        for seed in range(6):
            order = list(range(len(adv)))
            random.Random(seed).shuffle(order)
            for i in order:
                got = s.compile(adv[i][1], adv[i][2])
                if norm(got) != norm(want[i]):
                    bad += 1
                    if bad <= 10: print(f'DIFF adversarial pass {seed} {adv[i][0]}: code {got[0]}/{want[i][0]}')
        reqs += adv; expect += want   # (only for the counts below)
        s.close()
    ok = sum(1 for e in expect if e[0] == 0)
    print(f'{len(reqs)} requests ({ok} with an object), mode {a.mode}, both orders: {"identical" if not bad else f"{bad} DIFFERENCES"}')
    if a.time:
        ms = lambda v: f'mean {statistics.mean(v) * 1e3:.2f} ms p95 {p95(v) * 1e3:.2f} ms'
        print(f'latency per compile: CLI {ms(t_cli)}; server {ms(t_srv)}')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
