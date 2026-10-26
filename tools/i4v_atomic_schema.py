"""Structural transport for opt-in atomic progress observations.

This only decodes bounded retained evidence. It does not decide semantic
correctness or synthesize state; an independent oracle must replay the trace,
actual token chains, old-route store and later physical destinations.
"""
from check_i1 import state
from i4v_check import exact, require
from check_i4v_ordering import numeric_transport

IMAGE_BYTES = 12288
SCALARS = set(('magic n actor writer parked requested drained mutated acknowledged blocked opened delivered snapshot_count').split())
OWNER_SCALARS = set(('saved_gpa saved_leaf fault_pc resume_pc queued resumed completed ack_leaf new_leaf').split())
WALK_FIELDS = set(('address gpa leaf_bytes writable dirty read_count writes reads').split())
EVENT_FIELDS = set(('consumed deliveries boundary blocked wait_checks shadow priority packet').split())


def image(raw, label):
    require(type(raw) is str and len(raw)==IMAGE_BYTES*2,label+' bounded complete image')
    try:decoded=bytes.fromhex(raw)
    except ValueError:raise ValueError(label+' invalid hex')
    exact(len(decoded),IMAGE_BYTES,label+' decoded image')
    return decoded


def observations(report):
    """Expose exact source observations for the independent replay author."""
    numeric_transport(report)
    exact(report['case'],'ordering','underlying native participant report')
    exact(report['image_bytes'],IMAGE_BYTES,'extended data/old-results/spare image')
    atomic=report['atomic']
    exact(set(atomic),SCALARS|{'owners','cuts','tables_before','tables_after','coordinator'},'atomic evidence schema')
    exact(atomic['n'],report['n'],'atomic participant binding')
    require(1<=atomic['n']<=4 and atomic['actor']<atomic['n'],'finite atomic participants')
    exact(len(atomic['owners']),4,'fixed owner evidence capacity')
    for owner in atomic['owners']:
        exact(set(owner),OWNER_SCALARS|{'fault_frame','before_guest','completed_guest','event','walk'},'retained owner schema')
        state(owner['fault_frame'],'retained native frame')
        exact(set(owner['walk']),WALK_FIELDS,'actual remap walk schema')
        exact(len(owner['walk']['reads']),4,'four-level read capacity')
        require(all(type(row) is list and len(row)==2 for row in owner['walk']['reads']),'ordered actual table words')
        exact(set(owner['event']),EVENT_FIELDS,'canonical event handoff schema')
    for field in ('tables_before','tables_after'):
        values=atomic[field]
        require(type(values) is list and len(values)==16384 and all(type(v) is int and 0<=v<256 for v in values),field+' full table bytes')
    exact(len(atomic['cuts']),3,'three bounded coherent cuts')
    exact(len(report['images']),32,'all actual token-chain trials')
    return dict(cuts=[image(raw,'cut') for raw in atomic['cuts']],
                trials=[image(raw,'trial') for raw in report['images']])
