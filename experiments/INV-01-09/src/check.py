#!/usr/bin/env python3
"""Validate one INV-01-09 probe JSON output against the invariants a
truthful run must satisfy. Standard library only.

Usage: check.py <path-to-probe-output.json>
Exit 0 and print PASS if every rule holds; exit 1 and print each failing
rule otherwise. This is the code-gate validation tool: it must FAIL on the
probe's own `--negative` output, which run.sh exercises before any evidence
run and records as the negative-control result, not a claim.
"""
import errno
import json
import sys


def check(d):
    failures = []

    def require(cond, msg):
        if not cond:
            failures.append(msg)

    require(d.get("experiment") == "INV-01-09",
            "experiment must be INV-01-09")
    require(d.get("mode") in ("normal", "negative", "fixture-valid", "fixture-negative"),
            "mode must identify a declared probe or fixture mode")

    host = d.get("host", {})
    require(host.get("hw_pagesize", 0) > 0, "host.hw_pagesize must be > 0")
    require(host.get("hw_ncpu", 0) >= 1, "host.hw_ncpu must be >= 1")
    require(bool(host.get("hw_model")), "host.hw_model must be non-empty")

    vn = d.get("vmnet_start_interface", {})
    require(
        "status_code" in vn and "callback_fired" in vn and "timed_out" in vn
        and "start_returned_nonnull" in vn,
        "vmnet_start_interface must report status_code, callback_fired, "
        "timed_out, and start_returned_nonnull",
    )
    # The completion handler failing to fire within the 5 s wait
    # (callback_fired=false, timed_out=true) is a predeclared, reportable
    # outcome (CLM-01-09-14/-20), not a checker failure -- REV-047 required
    # change 5. The only structural invariant is that the two fields must be
    # opposite: dispatch_semaphore_wait either got the signal (callback ran)
    # or timed out (it did not), never both or neither.
    require(
        vn.get("callback_fired") is not None
        and vn.get("timed_out") is not None
        and vn.get("callback_fired") != vn.get("timed_out"),
        "vmnet_start_interface.callback_fired and .timed_out must be opposite",
    )
    agreed_success = (
        vn.get("start_returned_nonnull") is True
        and vn.get("callback_fired") is True
        and vn.get("status_code") == 1000
    )
    require(vn.get("success") == agreed_success,
            "vmnet_start_interface.success must require non-NULL return and "
            "callback status VMNET_SUCCESS")
    # No source establishes that the synchronous return value and the
    # asynchronous callback status must agree (CLM-01-09-02/-11: the header
    # states each contract separately and does not state precedence between
    # them). A disagreement is therefore a real, reportable possible outcome,
    # not proof of a broken probe, and must not be a hard-fail invariant here
    # -- that would bake an undocumented assumption into the code gate and
    # could never let a truthful normal run pass. Both raw fields are already
    # recorded above for Results/Analysis to examine.

    for name in ("socket_stream", "socket_dgram"):
        s = d.get(name, {})
        require(
            "fd_ok" in s and "errno_val" in s,
            f"{name} must report fd_ok and errno_val",
        )
        require(s.get("fd_ok") is True,
                f"{name}.fd_ok must be true for the unprivileged positive control")

    raw = d.get("socket_raw", {})
    require("fd_ok" in raw, "socket_raw.fd_ok must be present")
    require(raw.get("fd_ok") is False,
            "socket_raw.fd_ok must be false for the unprivileged negative control")
    if raw.get("fd_ok") is False:
        require(raw.get("errno_val") == errno.EPERM,
                f"socket_raw.errno_val must be EPERM ({errno.EPERM})")

    pty = d.get("pty", {})
    if pty.get("posix_openpt_ok"):
        require(
            "grantpt_ok" in pty and "unlockpt_ok" in pty,
            "pty.grantpt_ok/unlockpt_ok must be present when posix_openpt_ok is true",
        )
    else:
        require(
            pty.get("grantpt_ok") is False and pty.get("unlockpt_ok") is False,
            "pty.grantpt_ok/unlockpt_ok must be false when posix_openpt_ok is false",
        )
    require(
        pty.get("posix_openpt_ok") is True
        and pty.get("grantpt_ok") is True
        and pty.get("unlockpt_ok") is True,
        "pty open/grant/unlock positive-control chain must succeed",
    )

    ge = d.get("getentropy_256", {})
    require("ok" in ge, "getentropy_256.ok must be present")
    require(ge.get("ok") is True,
            "getentropy_256.ok must be true for the positive control")

    # getentropy(2) documents a 256-byte maximum per-call buffer size (SRC-115);
    # a 257-byte request must fail. This is the invariant the --negative mode
    # violates by fabricating ok=true.
    over = d.get("getentropy_over_limit", {})
    require(
        over.get("attempted_size") == 257,
        "getentropy_over_limit.attempted_size must be 257",
    )
    require(
        over.get("ok") is False,
        "getentropy_over_limit.ok must be false: getentropy(2) documents a "
        "256-byte maximum per call",
    )
    # errno_val is recorded but not asserted to a specific value: the man
    # page documents EIO for "too many bytes requested", but this is the
    # only field this checker treats as an open Hypothesis rather than a
    # hard invariant (CLM-01-09-18) -- asserting the exact errno here would
    # make the code gate pre-judge the evidence run's own question.

    bio = d.get("block_io", {})
    for field in (
        "open_ok", "f_nocache_ok", "f_fullfsync_ok", "pwrite_ok",
        "pread_ok", "data_match", "punchhole_ok",
    ):
        require(bio.get(field) is True,
                f"block_io.{field} must be true for the block-I/O positive control")
    # punchhole_ok alone only means fcntl(F_PUNCHHOLE) did not return -1;
    # punchhole_freed_blocks is derived from st_blocks captured immediately
    # before/after the call on the file the chain actually wrote, and is the
    # field that actually shows a hole was created (REV-048 required change 6).
    require(bio.get("punchhole_freed_blocks") is True,
            "block_io.punchhole_freed_blocks must be true: F_PUNCHHOLE must "
            "reduce st_blocks on the written file")

    sp = d.get("sparse_ftruncate", {})
    require(sp.get("ftruncate_ok") is True,
            "sparse_ftruncate.ftruncate_ok must be true")
    require(
        sp.get("st_size") == sp.get("requested_size"),
        "sparse_ftruncate.st_size must equal requested_size after ftruncate",
    )
    require(sp.get("is_sparse") is True,
            "sparse_ftruncate.is_sparse must be true")

    return failures


def main(argv):
    if len(argv) != 2:
        print("usage: check.py <probe-output.json>", file=sys.stderr)
        return 2
    with open(argv[1]) as f:
        d = json.load(f)
    failures = check(d)
    if failures:
        print(f"FAIL ({len(failures)} rule(s) violated):")
        for f_ in failures:
            print(f"  - {f_}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
