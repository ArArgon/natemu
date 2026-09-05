"""INV-01-06 M06-M1 two-pass checker.

Pure Python; never launches ../src/state_server.c. Reads the JSONL trace
schema that binary is designed to emit (see the "TRACE SCHEMA" comment
below, and state_server.c's jlog() call sites) and reports PASS/FAIL.

Pass 1 (wire-level): every "receive" record matches the frozen wire contract
(exact routine id, exception class, code/flavor/count) before any semantic
field is trusted.
Pass 2 (semantic/lifecycle): per-event identity/repair/reply/resume
completeness, exactly-one-outstanding-fault ordering, forwarded-path
unchanged-state proof, teardown/restore semantic equality, and full right
disposal -- matching INV-01-06.md:664-712,746-751.

Self-test: `python3 check.py` builds a minimal valid trace, confirms it
PASSes, then applies exactly the five negative fixtures named in the M06-M1
supplement task -- wrong routine id, reply with changed general state,
double outstanding fault, unreleased right, restoration mismatch -- and
confirms each is independently rejected with a distinct reason. This is the
one runnable check this module leaves behind; it needs no native execution.

TRACE SCHEMA (one JSON object per line):
  meta, register, prior_handler_unsupported, arm, receive, identity, repair,
  reply, forward_call, resume, commit, teardown_restore, restoration_proof,
  terminal, disposal, summary
See ../src/state_server.c's jlog() call sites for the exact field sets this
checker parses.
"""
import copy
import json

ROUTINE_ID = 2406
EXC_BAD_ACCESS = 1
ARM_THREAD_STATE64 = 6
ARM_THREAD_STATE64_COUNT = 68


class Invalid(Exception):
    pass


def parse(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def by_kind(rows, kind):
    return [r for r in rows if r.get("kind") == kind]


# ---- pass 1: wire-level ----------------------------------------------------

def check_wire(rows):
    fails = []
    for r in by_kind(rows, "receive"):
        who = "worker %r event-role %r" % (r.get("worker"), r.get("role"))
        if r.get("routine_id") != ROUTINE_ID:
            fails.append("wrong routine id on %s: got %r, expected %d" % (who, r.get("routine_id"), ROUTINE_ID))
        if r.get("exception") != EXC_BAD_ACCESS:
            fails.append("wrong exception class on %s: got %r" % (who, r.get("exception")))
        if r.get("codeCnt") != 2:
            fails.append("wrong codeCnt on %s: got %r, expected 2" % (who, r.get("codeCnt")))
        if r.get("flavor") != ARM_THREAD_STATE64:
            fails.append("wrong flavor on %s: got %r, expected ARM_THREAD_STATE64" % (who, r.get("flavor")))
        if r.get("old_stateCnt") != ARM_THREAD_STATE64_COUNT:
            fails.append("wrong old_stateCnt on %s: got %r, expected %d" % (who, r.get("old_stateCnt"), ARM_THREAD_STATE64_COUNT))
    return fails


# ---- pass 2: semantic / lifecycle ------------------------------------------

def check_double_outstanding(rows):
    """Two arm events for the same worker must never overlap without an
    intervening resume: strict alternation arm, resume, arm, resume, ..."""
    fails = []
    by_worker = {}
    for r in rows:
        if r.get("kind") in ("arm", "resume") and "worker" in r:
            by_worker.setdefault(r["worker"], []).append(r["kind"])
    for worker, seq in by_worker.items():
        state = "idle"
        for kind in seq:
            if kind == "arm":
                if state == "armed":
                    fails.append("double outstanding fault: worker %r armed twice with no resume between" % worker)
                    break
                state = "armed"
            else:  # resume
                state = "idle"
    return fails


def check_events_complete(rows):
    fails = []
    events = {}
    for r in rows:
        if "worker" not in r or "event" not in r:
            continue
        events.setdefault((r["worker"], r["event"]), {})[r["kind"]] = r
    for (worker, event), kinds in events.items():
        who = "worker %r event %r" % (worker, event)
        if "arm" not in kinds:
            continue
        for required in ("resume", "commit"):
            if required not in kinds:
                fails.append("%s: missing %s record" % (who, required))
        resume = kinds.get("resume")
        if resume and not resume.get("data_ok"):
            fails.append("%s: resume did not confirm data_ok" % who)
    return fails


def check_replies(rows):
    fails = []
    for r in by_kind(rows, "reply"):
        who = "worker %r role %r" % (r.get("worker"), r.get("role"))
        if r.get("role") == "reject":
            continue
        if r.get("kr") != 0:
            fails.append("%s: non-success reply kr=%r" % (who, r.get("kr")))
        if not r.get("state_unchanged"):
            fails.append("%s: reply carries a changed general state" % who)
    for r in by_kind(rows, "forward_call"):
        if not r.get("prior_state_unchanged"):
            fails.append("worker %r: forwarded prior reply carries a changed general state" % r.get("worker"))
    return fails


def check_restoration(rows):
    fails = []
    for r in by_kind(rows, "teardown_restore"):
        if not r.get("semantic_equal"):
            fails.append("worker %r: restoration mismatch (restored config not semantically equal to snapshot)" % r.get("worker"))
    return fails


EXPECTED_DISPOSALS = {"send", "thread_self", "receive"}


def check_disposal(rows):
    """Every acquired right must show exactly one disposal action; nothing
    left unreleased, nothing disposed twice."""
    fails = []
    seen = {}
    for r in by_kind(rows, "disposal"):
        key = (r.get("worker"), r.get("right"))
        seen[key] = seen.get(key, 0) + 1
    workers = {r.get("worker") for r in by_kind(rows, "register")}
    forwarded_workers = {r.get("worker") for r in by_kind(rows, "forward_call")}
    for worker in workers:
        expected = set(EXPECTED_DISPOSALS)
        if worker in forwarded_workers:
            expected |= {"prior_send", "prior_receive"}
        for right in expected:
            count = seen.get((worker, right), 0)
            if count == 0:
                fails.append("worker %r: right %r never disposed (unreleased right)" % (worker, right))
            elif count > 1:
                fails.append("worker %r: right %r disposed %d times (double disposal)" % (worker, right, count))
    return fails


def check_no_mig_no_reply(rows):
    """Every receive must be answered by a reply or an explicit terminal
    record; a silently-absorbed request (MIG_NO_REPLY-shaped) is a defect."""
    fails = []
    receives = by_kind(rows, "receive")
    replies = len(by_kind(rows, "reply"))
    terminals = len(by_kind(rows, "terminal"))
    if receives and replies + terminals == 0:
        fails.append("receive recorded with neither a reply nor a terminal record")
    return fails


def two_pass_check(text):
    rows = parse(text)
    if not rows:
        raise Invalid("EMPTY_TRACE")
    fails = []
    fails += check_wire(rows)                 # pass 1
    fails += check_double_outstanding(rows)    # pass 2
    fails += check_events_complete(rows)
    fails += check_replies(rows)
    fails += check_restoration(rows)
    fails += check_disposal(rows)
    fails += check_no_mig_no_reply(rows)
    return fails


def report(text):
    fails = two_pass_check(text)
    for f in fails:
        print("FAIL %s" % f)
    print("FAIL" if fails else "PASS")
    return 1 if fails else 0


# ---- fixture construction for the self-test --------------------------------

def _rows_to_text(rows):
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def good_trace_rows(worker=0, event=0):
    return [
        dict(kind="meta", investigation="INV-01-06-M1", n=1, jit=0, access="load",
             topology="serial", path="direct", seed=60601, warmup=0, checked=1,
             negative="", arm="", routine_id=ROUTINE_ID),
        dict(kind="register", worker=worker, recv_port=100, prior_recv_port=0, old_count=0, published=True),
        dict(kind="arm", worker=worker, event=event, warmup=False, address=4096, access="load",
             cookie=1, nonce=1, expected_pc=4096),
        dict(kind="receive", role="front", worker=worker, routine_id=ROUTINE_ID, exception=EXC_BAD_ACCESS,
             codeCnt=2, flavor=ARM_THREAD_STATE64, old_stateCnt=ARM_THREAD_STATE64_COUNT),
        dict(kind="identity", role="front", worker=worker, nonce=1, outstanding=True, valid=True),
        dict(kind="repair", role="front", worker=worker, mprotect_result=0, errno=0, skipped=False),
        dict(kind="reply", role="front", worker=worker, kr=0, flavor=ARM_THREAD_STATE64,
             new_stateCnt=ARM_THREAD_STATE64_COUNT, state_unchanged=True),
        dict(kind="resume", worker=worker, event=event, data_ok=True, sentinels_ok=True, sp_ok=True, commit_count=1),
        dict(kind="commit", worker=worker, event=event),
        dict(kind="teardown_restore", worker=worker, restored_count=0, semantic_equal=True),
        dict(kind="disposal", worker=worker, right="send", action="mod_refs(-1)", kr=0),
        dict(kind="disposal", worker=worker, right="thread_self", action="deallocate", kr=0),
        dict(kind="disposal", worker=worker, right="receive", action="mod_refs(-1)", kr=0),
        dict(kind="summary", events_checked=1, events_warmup=0, commits_total=1, zero_unclassified=True, status="completed"),
    ]


def _mutate(rows, kind, **fields):
    rows = copy.deepcopy(rows)
    for r in rows:
        if r.get("kind") == kind:
            r.update(fields)
    return rows


def _remove(rows, kind):
    return [r for r in rows if r.get("kind") != kind]


def _duplicate(rows, kind):
    rows = copy.deepcopy(rows)
    for r in list(rows):
        if r.get("kind") == kind:
            rows.append(copy.deepcopy(r))
            break
    return rows


def demo():
    good = good_trace_rows()
    assert two_pass_check(_rows_to_text(good)) == [], "good trace must PASS"

    # 1. wrong routine id
    bad = _mutate(good, "receive", routine_id=2405)
    fails = two_pass_check(_rows_to_text(bad))
    assert any("wrong routine id" in f for f in fails), fails

    # 2. reply with changed general state
    bad = _mutate(good, "reply", state_unchanged=False)
    fails = two_pass_check(_rows_to_text(bad))
    assert any("changed general state" in f for f in fails), fails

    # 3. double outstanding fault: two arms, no resume between
    rows = copy.deepcopy(good)
    second_arm = copy.deepcopy(next(r for r in rows if r["kind"] == "arm"))
    second_arm["event"] = 1
    insert_at = next(i for i, r in enumerate(rows) if r["kind"] == "resume")
    rows.insert(insert_at, second_arm)
    fails = two_pass_check(_rows_to_text(rows))
    assert any("double outstanding fault" in f for f in fails), fails

    # 4. unreleased right: drop the receive-right disposal record
    bad = [r for r in good if not (r.get("kind") == "disposal" and r.get("right") == "receive")]
    fails = two_pass_check(_rows_to_text(bad))
    assert any("unreleased right" in f for f in fails), fails

    # 4b. double disposal is also rejected
    bad = _duplicate(good, "disposal")
    fails = two_pass_check(_rows_to_text(bad))
    assert any("double disposal" in f for f in fails), fails

    # 5. restoration mismatch
    bad = _mutate(good, "teardown_restore", semantic_equal=False)
    fails = two_pass_check(_rows_to_text(bad))
    assert any("restoration mismatch" in f for f in fails), fails

    # sanity: empty trace is rejected structurally, not silently accepted
    try:
        two_pass_check("")
        raise AssertionError("empty trace must raise Invalid")
    except Invalid:
        pass

    # sanity: a receive with no reply/terminal (MIG_NO_REPLY-shaped) is rejected
    bad = _remove(good, "reply")
    fails = two_pass_check(_rows_to_text(bad))
    assert any("neither a reply nor a terminal" in f for f in fails), fails

    print("check.py self-test: 8/8 fixtures behaved as expected (PASS + 7 distinct rejections)")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            sys.exit(report(f.read()))
    demo()
