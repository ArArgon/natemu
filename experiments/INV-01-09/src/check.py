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

    host = d.get("host", {})
    require(host.get("hw_pagesize", 0) > 0, "host.hw_pagesize must be > 0")
    require(host.get("hw_ncpu", 0) >= 1, "host.hw_ncpu must be >= 1")
    require(bool(host.get("hw_model")), "host.hw_model must be non-empty")

    vn = d.get("vmnet_start_interface", {})
    require("status_code" in vn, "vmnet_start_interface.status_code must be present")
    require(vn.get("callback_fired") is True,
            "vmnet_start_interface callback must fire before the 5 s timeout")
    require(vn.get("timed_out") is False,
            "vmnet_start_interface must not time out")
    agreed_success = (
        vn.get("start_returned_nonnull") is True
        and vn.get("callback_fired") is True
        and vn.get("status_code") == 1000
    )
    require(vn.get("success") == agreed_success,
            "vmnet_start_interface.success must require non-NULL return and "
            "callback status VMNET_SUCCESS")
    require(
        not (
            (vn.get("start_returned_nonnull") is True
             and vn.get("status_code") != 1000)
            or (vn.get("start_returned_nonnull") is False
                and vn.get("status_code") == 1000)
        ),
        "vmnet synchronous return and callback status disagree; outcome is ambiguous",
    )

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
    require(over.get("errno_val") == errno.EIO,
            f"getentropy over-limit errno must be EIO ({errno.EIO})")

    bio = d.get("block_io", {})
    for field in (
        "open_ok", "f_nocache_ok", "f_fullfsync_ok", "pwrite_ok",
        "pread_ok", "data_match", "punchhole_ok",
    ):
        require(bio.get(field) is True,
                f"block_io.{field} must be true for the block-I/O positive control")

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
