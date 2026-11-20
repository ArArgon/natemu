"""Harnesses run one kind of workload and say how each case came out. Each module defines HARNESS, an object with:

    cases(family, suite) -> list[str]                       the case names (deterministic suites: from the lock)
    requirements(family, suite, variant) -> list[Requirement]  what to prepare before any test starts
    run(batch: list[CaseItem], ctx: RunContext) -> dict[nodeid, CaseResult]
"""
import dataclasses


@dataclasses.dataclass
class CaseResult:
    outcome: str                    # passed | failed | skipped
    message: str = ''
    category: str | None = None     # a testkit category (reused, unsupported, build-failure, ...)
    sub: list = dataclasses.field(default_factory=list)      # [SubResult]: per-test results of a units suite
    output: bytes | None = None     # the normalized output (deterministic suites), for reuse
    own: bool = False               # failed for a reason of its own, not only through its sub-results (known_bugs)


@dataclasses.dataclass
class SubResult:
    name: str                       # appended to the item's node id with '::'
    outcome: str
    longrepr: str = ''
    duration: float = 0.0
    category: str | None = None     # a testkit category (expected-fail, missing, ...: testkit/report.py); longrepr its note


@dataclasses.dataclass(frozen=True)
class Requirement:
    kind: str                       # artifact | resource | input
    name: str
