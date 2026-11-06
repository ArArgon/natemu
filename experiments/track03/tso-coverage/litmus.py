"""Litmus declarations shared by gen.py (guest code) and oracle.py (allowed sets).

Ops: ('st', loc, value[, 'h']), ('ld', reg, loc[, 'h']), ('dsb',) (handler only).
A trailing 'h' makes the access run in an EL1 exception handler (svc ... eret)
through the second virtual alias of the same RAM; consecutive 'h' ops share one
handler.  Values are 1 or 2; digit 0 = zero, 1 = value 1, 2 = value 2, 3 = torn/other.
`outputs` order fixes digit positions: register names, or 'mem:x' (final value of x,
read by thread 0 after the end barrier).
"""

def t(*ops): return list(ops)

LITMUS = {
    'sb':   dict(threads=[t(('st','x',1),('ld','r0','y')), t(('st','y',1),('ld','r1','x'))],
                 outputs=['r0','r1']),
    'mp':   dict(threads=[t(('st','x',1),('st','y',1)), t(('ld','r0','y'),('ld','r1','x'))],
                 outputs=['r0','r1']),
    'lb':   dict(threads=[t(('ld','r0','x'),('st','y',1)), t(('ld','r1','y'),('st','x',1))],
                 outputs=['r0','r1']),
    'wrc':  dict(threads=[t(('st','x',1)), t(('ld','r0','x'),('st','y',1)),
                          t(('ld','r1','y'),('ld','r2','x'))], outputs=['r0','r1','r2']),
    'iriw': dict(threads=[t(('st','x',1)), t(('st','y',1)),
                          t(('ld','r0','x'),('ld','r1','y')), t(('ld','r2','y'),('ld','r3','x'))],
                 outputs=['r0','r1','r2','r3']),
    '2+2w': dict(threads=[t(('st','x',1),('st','y',2)), t(('st','y',1),('st','x',2))],
                 outputs=['mem:x','mem:y']),
    's':    dict(threads=[t(('st','x',2),('st','y',1)), t(('ld','r0','y'),('st','x',1))],
                 outputs=['r0','mem:x']),
    'r':    dict(threads=[t(('st','x',1),('st','y',1)), t(('st','y',2),('ld','r0','x'))],
                 outputs=['mem:y','r0']),
    'sbf':  dict(threads=[t(('st','x',1),('ld','r0','x'),('ld','r1','y')),
                          t(('st','y',1),('ld','r2','y'),('ld','r3','x'))],
                 outputs=['r0','r1','r2','r3']),
    'corr': dict(threads=[t(('st','x',1)), t(('ld','r0','x'),('ld','r1','x'))], outputs=['r0','r1']),
    # single-copy atomicity rounds: one writer, three readers re-fetch the freshly zeroed location after a barrier
    'tear': dict(threads=[t(('st','x',1)), t(('ld','r0','x'),('ld','r1','x')), t(('ld','r2','x'),('ld','r3','x')), t(('ld','r4','x'))],
                 outputs=['r0','r1','r2','r3','r4']),
    # same with the writer a swpal (op 'swp') / two half-width stores (op 'sth': deliberate tear, positive control)
    'tear-swp':   dict(threads=[t(('swp','x',1)), t(('ld','r0','x'),('ld','r1','x')), t(('ld','r2','x'),('ld','r3','x')), t(('ld','r4','x'))],
                 outputs=['r0','r1','r2','r3','r4']),
    'tear-split': dict(threads=[t(('sth','x',1)), t(('ld','r0','x'),('ld','r1','x')), t(('ld','r2','x'),('ld','r3','x')), t(('ld','r4','x'))],
                 outputs=['r0','r1','r2','r3','r4']),
    'cow':  dict(threads=[t(('st','x',1),('ld','r0','x')), t(('st','x',2),('ld','r1','x'))],
                 outputs=['r0','r1','mem:x']),
}

# Exception-entry variants (store/load made by EL1 code after exception entry, via alias).
EX = {
    'ex-mp-a':   dict(threads=[t(('st','x',1),('st','y',1,'h')), t(('ld','r0','y'),('ld','r1','x'))], outputs=['r0','r1']),
    'ex-mp-b':   dict(threads=[t(('st','x',1,'h'),('st','y',1)), t(('ld','r0','y'),('ld','r1','x'))], outputs=['r0','r1']),
    'ex-mp-hh':  dict(threads=[t(('st','x',1,'h'),('st','y',1,'h')), t(('ld','r0','y'),('ld','r1','x'))], outputs=['r0','r1']),
    'ex-mp-r':   dict(threads=[t(('st','x',1),('st','y',1)), t(('ld','r0','y'),('ld','r1','x','h'))], outputs=['r0','r1']),
    'ex-mp-a-dsb': dict(threads=[t(('st','x',1),('st','y',1,'h'),('dsb',)), t(('ld','r0','y'),('ld','r1','x'))], outputs=['r0','r1']),
    'ex-mp-b-dsb': dict(threads=[t(('st','x',1,'h'),('dsb',),('st','y',1)), t(('ld','r0','y'),('ld','r1','x'))], outputs=['r0','r1']),
    'ex-sb':     dict(threads=[t(('st','x',1,'h'),('ld','r0','y')), t(('st','y',1),('ld','r1','x'))], outputs=['r0','r1']),
}
# model programs: litmus name -> base litmus for the oracle
EX_BASE = {'ex-mp-a':'mp','ex-mp-b':'mp','ex-mp-hh':'mp','ex-mp-r':'mp','ex-mp-a-dsb':'mp','ex-mp-b-dsb':'mp','ex-sb':'sb'}
ALL = {**LITMUS, **EX}

# access forms: (class, offsets in a 128-byte-aligned block by width)
OFFS = {'al': {1:0, 2:0, 4:0, 8:0},
        'u16': {2:7, 4:6, 8:5},       # inside one 16-byte block, crossing an 8-byte boundary
        'x16': {2:15, 4:14, 8:12},    # crosses a 16-byte boundary inside a 64-byte line
        'x64': {2:63, 4:62, 8:60},    # crosses a 64-byte boundary (inside Apple's 128-byte line)
        'x128': {2:127, 4:126, 8:124}}  # crosses the 128-byte line
def forms():
    yield (1, 'al', 0)
    for w in (2, 4, 8):
        for cls in OFFS:
            yield (w, cls, OFFS[cls][w])
# page crossing: X base sits at page offset 0xF80 (gen.py passes xbase), so the 128-byte-crossing offsets cross a 4 KiB page
PG_LITMUS = ('mp', 'lb', 'sb', 'corr', 'tear', 'tear-swp')
PG_XBASE = 0xF80
EX_FORMS = [(1,'al',0),(4,'al',0),(8,'al',0),(8,'u16',5),(8,'x16',12),(8,'x64',60)]
