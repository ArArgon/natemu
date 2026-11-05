#!/usr/bin/env python3
"""fexc --serve --channel FD (perf chunk 21) from the server's side, as a client the host is not (test_serve.py covers the
payloads over the pipe; the host's tests and replays cover its own client):

    test_channel.py FEXC

Replies through the channel equal the pipe's (a few requests, both serve modes); a reply over the reply cap
(`--channel-reply-cap`) is an error reply, not a crash; an empty request, stdin's EOF and a request over the request area end
the server (exit 0, 0 and 3); bad `--channel` arguments, a mapping too small and one without the layout's magic are refused
at start. Exit status 1 on any failure."""
import ctypes, mmap, os, struct, subprocess, sys, tempfile, time

BYTES, REQ, REQ_CAP, MAGIC = 16 << 20, 4096, 1 << 20, 0x314E4843
REP = REQ + REQ_CAP
libc = ctypes.CDLL(None, use_errno=True)
libc.os_sync_wake_by_address_any.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32]
ARGS = ['--profile', 'slice1', '--rip', '0x1000']
CODES = [bytes.fromhex(h) for h in ('4801d8c3', '0f0b', '90' * 40 + 'c3', 'f4', '48b8' + '11' * 8 + 'c3')]


def body(args, code):
    a = b''.join(x.encode() + b'\0' for x in args)
    return struct.pack('<I', len(a)) + a + code


class Chan:
    """A client of the channel: the mapping is a 16 MiB temporary file, passed as an inherited fd."""
    def __init__(self, fexc, extra=(), magic=MAGIC, size=BYTES):
        self.f = tempfile.TemporaryFile()
        self.f.truncate(size)
        self.m = mmap.mmap(self.f.fileno(), size)
        struct.pack_into('<I', self.m, 8, magic)
        self.addr = ctypes.addressof(ctypes.c_char.from_buffer(self.m))
        fd = self.f.fileno()
        self.p = subprocess.Popen([fexc, '--serve', '--channel', str(fd), *extra], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, pass_fds=[fd])
        self.n = 0

    def post(self, payload, length=None):
        self.n += 1
        self.m[REQ:REQ + len(payload)] = payload
        struct.pack_into('<I', self.m, 4, len(payload) if length is None else length)
        struct.pack_into('<I', self.m, 0, self.n << 1)   # the sleeping bit, if the server had set it, is dropped: wake anyway
        libc.os_sync_wake_by_address_any(self.addr, 4, 1)

    def reply(self, timeout=10):
        end = time.time() + timeout
        while struct.unpack_from('<I', self.m, 64)[0] >> 1 != self.n:
            if time.time() > end: raise TimeoutError
            time.sleep(0.0001)
        n, = struct.unpack_from('<I', self.m, 68)
        return bytes(self.m[REP:REP + n])

    def exit(self, timeout=2):
        try: return self.p.wait(timeout)
        except subprocess.TimeoutExpired: self.p.kill(); return 'still running'


def pipe_replies(fexc, mode, reqs):
    p = subprocess.Popen([fexc, '--serve', '--serve-mode', mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out = []
    for b in reqs:
        p.stdin.write(struct.pack('<I', len(b)) + b); p.stdin.flush()
        n, = struct.unpack('<I', p.stdout.read(4)); out.append(p.stdout.read(n))
    p.stdin.close(); p.wait()
    return out


def main():
    fexc = sys.argv[1]
    fails = []
    check = lambda ok, what: (print(('ok   ' if ok else 'FAIL ') + what, flush=True), ok or fails.append(what))
    reqs = [body(ARGS, c) for c in CODES] + [body(['--profile', 'slice1', '--rip', '0x7f0000001000', '--cpl0'], c) for c in CODES]
    for mode in ('reuse', 'fork'):
        c = Chan(fexc, ['--serve-mode', mode])
        got = []
        for b in reqs:
            c.post(b); got.append(c.reply())
            time.sleep(0.03)   # past the server's spin: the next request finds it asleep and wakes it
        check(got == pipe_replies(fexc, mode, reqs), f'{mode}: {len(reqs)} replies equal the pipe\'s')
        c.post(b'')
        check(c.exit() == 0, f'{mode}: an empty request ends the server')
    c = Chan(fexc, ['--channel-reply-cap', '405'])   # reqs[0]'s reply is 412 bytes, reqs[3]'s 401
    c.post(reqs[0]); r = c.reply()
    code, jl = struct.unpack_from('<iI', r)
    check(code == 2 and b'exceeds the channel' in r[20:20 + jl], f'a reply over the cap is an error reply: {r[20:20 + jl]!r}')
    c.post(reqs[3]); r = c.reply()
    check(r == pipe_replies(fexc, 'reuse', [reqs[3]])[0], 'a reply under the cap still goes out whole')
    c.p.stdin.close()
    check(c.exit() == 0, "stdin's EOF ends a sleeping server")
    c = Chan(fexc)
    c.post(b'\0' * 8, length=REQ_CAP + 1)
    check(c.exit() == 3, 'a request over the request area ends the server (3)')
    bad = [(['--channel'], 'usage'), (['--channel', 'x'], 'usage'), (['--channel', '-1'], 'usage'), (['--channel', '99'], 'at least'),
           (['--channel-reply-cap', '64'], 'with --channel')]
    for args, want in bad:
        r = subprocess.run([fexc, '--serve', *args], capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
        msg = r.stderr.decode()
        check(r.returncode == 2 and (want == 'usage' and '--serve [' in msg or want in msg), f'{args} refused: {msg.strip()}')
    for kw, want in (({'size': 4096}, 'at least'), ({'magic': 0}, 'magic')):
        c = Chan(fexc, **kw)
        c.p.stdin.close()
        msg = c.p.stderr.read().decode()
        check(c.exit() == 2 and want in msg, f'{kw} refused: {msg.strip()}')
    print(f'{len(fails)} failures')
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
