#!/usr/bin/env python3
"""REV-082 independent replication checker for INV-01-09.

Written by reviewer context `t01_machine_reviewer`. This is NOT the author's
`src/check.py` and deliberately shares no code with it: it re-derives the
invariants from the raw JSON, from the method of record and the cited
sources, so that agreement between the two is evidence rather than a shared
bug. Standard library only.

Usage: verify.py <arm-label> <probe-output.json>
Exit 0 = every invariant holds, 1 = at least one violated, 2 = usage/parse.
"""
import json
import sys

EPERM, EMFILE_UNUSED = 1, 24
VMNET_SUCCESS = 1000


def invariants(d):
    """Yield (ok, description) for every invariant this reviewer derives."""
    v = d.get("vmnet_start_interface", {})
    b = d.get("block_io", {})
    s = d.get("sparse_ftruncate", {})
    p = d.get("pty", {})
    over = d.get("getentropy_over_limit", {})

    yield d.get("experiment") == "INV-01-09", "experiment tag is INV-01-09"
    yield d.get("mode") in ("normal", "negative"), "mode is normal or negative"
    yield d.get("host", {}).get("hw_model") == "Mac16,10", "host is Mac16,10"
    yield d.get("host", {}).get("hw_pagesize") == 16384, "host page size is 16384"

    # vmnet: the semaphore either got the signal or timed out, never both or
    # neither. success must be exactly the conjunction the header contract
    # implies; no precedence between the two is assumed.
    fired, timed = v.get("callback_fired"), v.get("timed_out")
    yield isinstance(fired, bool) and isinstance(timed, bool) and fired != timed, \
        "vmnet callback_fired and timed_out are opposite"
    yield v.get("success") is (
        v.get("start_returned_nonnull") is True and fired is True
        and v.get("status_code") == VMNET_SUCCESS
    ), "vmnet success equals nonnull-and-callback-and-VMNET_SUCCESS"
    yield v.get("success") is False, "vmnet did not succeed unprivileged"

    # sockets: documented SOCK_RAW super-user restriction (SRC-118).
    yield d.get("socket_stream", {}).get("fd_ok") is True, "SOCK_STREAM opened"
    yield d.get("socket_dgram", {}).get("fd_ok") is True, "SOCK_DGRAM opened"
    yield d.get("socket_raw", {}).get("fd_ok") is False, "SOCK_RAW refused"
    yield d.get("socket_raw", {}).get("errno_val") == EPERM, "SOCK_RAW errno is EPERM"

    yield all(p.get(k) is True for k in
              ("posix_openpt_ok", "grantpt_ok", "unlockpt_ok")), "pty chain succeeded"

    yield d.get("getentropy_256", {}).get("ok") is True, "getentropy(256) succeeded"
    yield over.get("attempted_size") == 257, "over-limit request was 257 bytes"
    # SRC-115 documents a 256-byte maximum; the errno is deliberately NOT
    # asserted -- the author's run observed EINVAL, not the documented EIO,
    # and pinning it here would convert an open observation into a rule.
    yield over.get("ok") is False, "getentropy(257) refused (256-byte limit enforced)"

    yield all(b.get(k) is True for k in
              ("open_ok", "f_nocache_ok", "f_fullfsync_ok", "pwrite_ok",
               "pread_ok", "data_match")), "block-I/O chain succeeded"
    # A hole is only demonstrated by a block-count drop on the written file.
    bb, ba = b.get("st_blocks_before_punch"), b.get("st_blocks_after_punch")
    yield isinstance(bb, int) and isinstance(ba, int) and bb > 0 and ba < bb, \
        "F_PUNCHHOLE reduced st_blocks on the written file"
    yield b.get("punchhole_freed_blocks") is True, "punchhole_freed_blocks agrees"
    # Recompute sparseness rather than trusting the probe's own boolean.
    yield s.get("st_size") == s.get("requested_size"), "ftruncate reached requested size"
    yield isinstance(s.get("st_blocks"), int) and \
        s["st_blocks"] * 512 < s["st_size"] / 2, "grown file is sparse (recomputed)"
    yield s.get("is_sparse") is True, "probe's is_sparse agrees with recomputation"


def main(argv):
    if len(argv) != 3:
        print("usage: verify.py <arm-label> <probe-output.json>", file=sys.stderr)
        return 2
    label = argv[1]
    try:
        d = json.load(open(argv[2]))
    except (OSError, json.JSONDecodeError) as e:
        print(f"{label}: PARSE-ERROR {e}", file=sys.stderr)
        return 2
    bad = [why for ok, why in invariants(d) if not ok]
    for why in bad:
        print(f"{label}: VIOLATED {why}")
    print(f"{label}: {'FAIL' if bad else 'PASS'} ({len(bad)} violated)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
