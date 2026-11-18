"""The FP family's rule (spec R2.13): a variant checks only correct behavior. In fast mode on a host without AFP (real
or hidden), the programs tagged with a documented deviation (tools/fp_deviations.py) are not run; everything else runs
and must match exactly."""
import sys

from testkit import paths

sys.path.insert(0, str(paths.repo_root() / 'tools'))
import fp_deviations  # noqa: E402

CORPUS = {'i8': 'i8-corpus-20261030-01', 'i8b': 'i8-corpus-20261030-02'}    # the predictions' keys for these inputs
_TAGS = {}


def applies(suite, case, variant, met):
    if variant.get('engine.fp') != 'fast' or variant.get('host.afp') is not False: return None
    tags = _TAGS.setdefault(suite, fp_deviations.corpus_deviations(CORPUS[suite]))
    return '+'.join(tags[case]['classes']) if case in tags else None
