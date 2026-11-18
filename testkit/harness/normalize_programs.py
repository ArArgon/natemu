"""The normalized final state of a straight-line program: exactly what tools/reference/ref.py:compare compares (the 16
GPRs, RFLAGS under the mask its rules derive from the program's annotations, RIP, the program's memory regions). This
module is part of every programs answer's fingerprint (spec R2.7): change it only when the answers should change."""
import hashlib
import json
import sys

from testkit import paths

REF = paths.repo_root() / 'tools' / 'reference'
if str(REF) not in sys.path: sys.path.insert(0, str(REF))


def _ref():
    import flags, ref  # noqa: E401  (tools/reference)
    return ref, flags


def mask_of(program: dict, mask: int | None = None) -> int:
    """The RFLAGS compare mask: given (locked at seeding, from the reference's executed path) for a flow program,
    else derived from the program's annotations exactly as ref.py does."""
    ref, flags = _ref()
    if mask is not None: return mask
    if ref.norm(program)['flow']:
        raise ValueError(f"{program['name']}: a flow program needs the mask locked at seeding (ref.py derives it from the executed path)")
    return flags.TRACKED & ~flags.analyze(ref.norm(program)['insns'])


def normalize(program: dict, state: dict, mask: int | None = None) -> bytes:
    """Canonical bytes of a final state (QEMU's result['qemu'] or natemu's report): what ref.compare compares."""
    ref, _ = _ref()
    return json.dumps({'gpr': [ref.num(v) for v in state['gpr']], 'rflags': ref.num(state['rflags']) & mask_of(program, mask),
                       'rip': ref.num(state['rip']),
                       'memory': [[ref.num(m['addr']), m['data']] for m in state['memory']]}, separators=(',', ':')).encode()


def answer(program: dict, state: dict, mask: int | None = None) -> str:
    return hashlib.sha256(normalize(program, state, mask)).hexdigest()


def corpus_hash(programs: list) -> str:
    """The input's identity: the programs only, not the generator header (which records the local clang, R2.15)."""
    return hashlib.sha256(json.dumps(programs, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


