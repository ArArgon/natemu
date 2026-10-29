#!/usr/bin/env python3 -I
"""Static census of x86 instruction forms natemu admission refuses.
Usage: census.py RAW_DIR OUT_MD   (needs RAW_DIR/x from extract.py), or census.py --selftest
Classifier = a byte-level port of tools/fexc/shim/fexc_slice1.inc (Primary/Two/Vex/Classify), the S3e, I1, I7 and I5a additions."""
import collections, json, os, re, struct, subprocess, sys
from multiprocessing import Pool

LEGACY = {0xF0, 0xF2, 0xF3, 0x66, 0x67, 0x2E, 0x36, 0x3E, 0x26, 0x64, 0x65}
BMI = {'andn', 'bextr', 'blsi', 'blsmsk', 'blsr', 'bzhi', 'mulx', 'pdep', 'pext', 'rorx', 'sarx', 'shlx', 'shrx'}
ALLOW, SYS, UNS = 'ALLOW', 'SYS', 'UNS'

def primary(op, modrm):
    mod, reg = modrm >> 6, (modrm >> 3) & 7
    if op < 0x40 and (op & 7) < 6: return ALLOW, False
    if op in (0x06, 0x07, 0x0E, 0x16, 0x17, 0x1E, 0x1F, 0x8C, 0x8E, 0x9A, 0xEA, 0xCA, 0xCB, 0xCF, 0xCC, 0xCD, 0xCE, 0xF1,
              0x9D, 0xFA, 0xFB, 0xF4, 0xE4, 0xE5, 0xE6, 0xE7, 0xEC, 0xED, 0xEE, 0xEF, 0x6C, 0x6D, 0x6E, 0x6F): return SYS, False
    if op in (0x63, 0x68, 0x6A, 0x69, 0x6B, 0x84, 0x85, 0x88, 0x89, 0x8A, 0x8B, 0x8D, 0x98, 0x99, 0xA0, 0xA1, 0xA2, 0xA3, 0xA8, 0xA9,
              0x80, 0x81, 0x83, 0x86, 0x87): return ALLOW, False
    if op in (0x9C, 0xFC, 0xFD, 0xF5, 0xF8, 0xF9, 0x9E, 0x9F, 0xC8, 0xC9): return ALLOW, False   # I1 (enter: level 0, below)
    if op == 0x8F: return (ALLOW if reg == 0 else UNS), False
    if op in (0xC0, 0xC1, 0xD0, 0xD1, 0xD2, 0xD3):
        return (UNS if reg == 6 or (reg in (2, 3) and mod != 3) else ALLOW), False
    if op in (0xC6, 0xC7): return (ALLOW if reg == 0 else UNS), False
    if op in (0xF6, 0xF7): return (UNS if reg == 1 else ALLOW), False
    if op == 0xFE: return (ALLOW if reg <= 1 else UNS), False
    if op == 0xFF:
        if reg in (3, 5): return SYS, False
        if reg in (2, 4): return ALLOW, True
        return (ALLOW if reg <= 1 or reg == 6 else UNS), False
    if op in (0xC2, 0xC3, 0xE8, 0xE9, 0xEB, 0xE0, 0xE1, 0xE2, 0xE3): return ALLOW, True
    if 0x50 <= op <= 0x5F or 0x90 <= op <= 0x97 or 0xB0 <= op <= 0xBF: return ALLOW, False
    if 0x70 <= op <= 0x7F: return ALLOW, True
    return UNS, False

def two(op, plain, modrm):
    mod, reg = modrm >> 6, (modrm >> 3) & 7
    if op in (0x00, 0x01, 0x02, 0x03, 0x05, 0x07, 0x34, 0x35, 0x06, 0x08, 0x09, 0x0B, 0xB9, 0xFF, 0x20, 0x21, 0x22, 0x23, 0x30, 0x32,
              0x33, 0x31, 0x78, 0x79, 0xA0, 0xA1, 0xA8, 0xA9, 0xA2, 0xB2, 0xB4, 0xB5): return SYS, False
    if op == 0xAE:
        if mod != 3 and reg == 7: return SYS, False
        if mod == 3 and reg < 4: return SYS, False
        return UNS, False
    if op == 0xC7 and mod == 3 and reg >= 6: return SYS, False                                      # S3: rdrand, rdseed, rdpid
    if not plain: return UNS, False
    if op in (0x1F, 0xA3, 0xAB, 0xB3, 0xBB, 0xAF, 0xB6, 0xB7, 0xBE, 0xBF, 0xBC, 0xBD): return ALLOW, False
    if op in (0xA4, 0xA5, 0xAC, 0xAD, 0xB0, 0xB1, 0xC0, 0xC1): return ALLOW, False                 # I1: shld/shrd, cmpxchg/xadd
    if op == 0x18: return (ALLOW if mod != 3 and reg <= 3 else UNS), False                         # I1: prefetchh m8
    if op == 0xC3: return (ALLOW if mod != 3 else UNS), False                                      # I1: movnti m
    if op == 0xBA: return (ALLOW if reg >= 4 else UNS), False
    if 0x40 <= op <= 0x4F or 0x90 <= op <= 0x9F or 0xC8 <= op <= 0xCF: return ALLOW, False
    if 0x80 <= op <= 0x8F: return ALLOW, True
    return UNS, False

def parse(b):
    """-> dict(prefixes, rex, kind('vex'|'xop'|'p'|'0f'|'0f38'|'0f3a'), op, modrm) or None"""
    i, pre = 0, set()
    while i < len(b) and b[i] in LEGACY: pre.add(b[i]); i += 1
    rex = 0
    if i < len(b) and 0x40 <= b[i] <= 0x4F: rex = b[i]; i += 1
    if i >= len(b): return None
    o = b[i]
    g = lambda k: b[i + k] if i + k < len(b) else 0
    if o in (0xC4, 0xC5, 0x62): return dict(pre=pre, rex=rex, kind='vex', op=o, modrm=0)
    if o == 0x8F and (g(1) >> 3) & 7 != 0: return dict(pre=pre, rex=rex, kind='xop', op=o, modrm=0)
    if o == 0x0F:
        if g(1) == 0x38: return dict(pre=pre, rex=rex, kind='0f38', op=g(2), modrm=g(3))
        if g(1) == 0x3A: return dict(pre=pre, rex=rex, kind='0f3a', op=g(2), modrm=g(3))
        return dict(pre=pre, rex=rex, kind='0f', op=g(1), modrm=g(2))
    return dict(pre=pre, rex=rex, kind='p', op=o, modrm=g(1))

SSE2 = set(range(0x10, 0x18)) | set(range(0x28, 0x30)) | set(range(0x50, 0x78)) | {0x77, 0x7C, 0x7D, 0x7E, 0x7F, 0xC2, 0xC4, 0xC5, 0xC6} | set(range(0xD0, 0xFF))
SMALL2 = {0xA4, 0xA5, 0xAC, 0xAD, 0xB0, 0xB1, 0xC0, 0xC1, 0xC3, 0x0D, 0x18, 0xC7}
SMALL1 = {0xFC, 0xFD, 0xF8, 0xF9, 0xF5, 0x9E, 0x9F, 0xC8, 0xC9}

def vex(b):
    """(map_select, pp, L, opcode, modrm.reg, legacy prefixes or REX before it) of a c4/c5 instruction, else None"""
    i = 0
    while i < len(b) and (b[i] in LEGACY or 0x40 <= b[i] <= 0x4F): i += 1
    if i + 3 >= len(b) or b[i] not in (0xC4, 0xC5): return None
    if b[i] == 0xC5: return 1, b[i + 1] & 3, b[i + 1] >> 2 & 1, b[i + 2], b[i + 3] >> 3 & 7, i > 0
    if i + 4 >= len(b): return None
    return b[i + 1] & 0x1F, b[i + 2] & 3, b[i + 2] >> 2 & 1, b[i + 3], b[i + 4] >> 3 & 7, i > 0

SEGS = {0x26, 0x2E, 0x36, 0x3E, 0x64, 0x65}

def vex_allowed(v, b=b''):
    """fexc_slice1.inc Vex(): the BMI1/BMI2 general-register forms but pdep/pext, VEX.L = 0, no prefix before VEX but a
    segment prefix (I7; its rules are segment()'s)"""
    m, pp, l, op, reg, prefixed = v
    if l or (prefixed and not set(leading(b)) <= SEGS): return False
    if m == 2:
        return (op == 0xF2 and pp == 0 or op == 0xF3 and pp == 0 and 1 <= reg <= 3 or op == 0xF5 and pp == 0
                or op == 0xF6 and pp == 3 or op == 0xF7)
    return m == 3 and op == 0xF0 and pp == 3

def leading(b):
    """The legacy prefix and REX bytes before the opcode, in order (duplicates kept)."""
    i = 0
    while i < len(b) and (b[i] in LEGACY or 0x40 <= b[i] <= 0x4F): i += 1
    return b[:i]

NO_MODRM_P = {0x50 + i for i in range(16)} | {0x70 + i for i in range(16)} | {0x90 + i for i in range(10)} | {0xB0 + i for i in range(16)} | \
    {0x68, 0x6A, 0x9C, 0x9E, 0x9F, 0xA0, 0xA1, 0xA2, 0xA3, 0xA8, 0xA9, 0xC2, 0xC3, 0xC8, 0xC9, 0xE0, 0xE1, 0xE2, 0xE3, 0xE8, 0xE9, 0xEB,
     0xF5, 0xF8, 0xF9, 0xFC, 0xFD} | {o for o in range(0x40) if o & 7 in (4, 5)}

def mem_base(b, k, op):
    """(explicit memory operand?, its base register number or None) of an admitted form (ModRM/SIB from the bytes)."""
    i = len(leading(b)); rex = b[i - 1] if i and 0x40 <= b[i - 1] <= 0x4F else 0
    if k == 'p':
        if op in (0xA0, 0xA1, 0xA2, 0xA3): return True, None
        if op in NO_MODRM_P: return False, None
        at, xb = i + 1, rex & 1
    elif k == '0f':
        if 0x80 <= op <= 0x8F or 0xC8 <= op <= 0xCF: return False, None
        at, xb = i + 2, rex & 1
    elif k == 'vex':
        at, xb = (i + 3, 0) if b[i] == 0xC5 else (i + 4, (~b[i + 1] >> 5) & 1)
    else:
        at, xb = i + 3, rex & 1
    if at >= len(b): return False, None
    modrm = b[at]; mod, rm = modrm >> 6, modrm & 7
    if mod == 3: return False, None
    if rm == 4:
        base = b[at + 1] & 7 if at + 1 < len(b) else 0
        return True, None if base == 5 and mod == 0 else base | xb << 3
    return True, None if rm == 5 and mod == 0 else rm | xb << 3

def segment(b, k, op):
    """fexc_slice1.inc's segment-prefix rules (I7) for an otherwise admitted form: (class, why) of a refusal, else None."""
    pre = leading(b)
    ss, data, high = pre.count(0x36), sum(pre.count(x) for x in (0x26, 0x2E, 0x3E)), pre.count(0x64) + pre.count(0x65)
    memory, base = mem_base(b, k, op)
    if high and not memory: return 'FS/GS', 'FS/GS override without a memory operand'
    if high and ss + data + high != 1: return 'FS/GS', 'FS/GS override with another segment prefix'
    if not high and memory and ss + data and (bool(ss) != (base in (4, 5))):
        return 'FS/GS', 'CS/DS/ES/SS override naming another segment than the default'
    return None

def string(pre, op):   # pre: the prefix bytes (leading)
    """fexc_slice1.inc's string-instruction rules (I2): REP on all five, REPE/REPNE on cmps/scas only, never both; no
    address-size prefix or LOCK; FS/GS (alone) and CS/DS/ES only on the DS:RSI source of movs/cmps/lods, never SS there."""
    compare, source = op in (0xA6, 0xA7, 0xAE, 0xAF), op <= 0xA7 or op in (0xAC, 0xAD)
    ss, data, high = pre.count(0x36), sum(pre.count(x) for x in (0x26, 0x2E, 0x3E)), pre.count(0x64) + pre.count(0x65)
    if 0xF0 in pre: return 'LOCK', 'LOCK on a string instruction'
    if 0xF2 in pre and 0xF3 in pre: return 'string', 'REP and REPNE together'
    if 0xF2 in pre and not compare: return 'string', 'REPNE on movs/stos/lods'
    if 0x67 in pre: return 'string', 'address-size prefix'
    if high and (not source or ss + data + high != 1): return 'FS/GS', 'FS/GS on a string instruction without a source, or with another prefix'
    if source and ss and not high: return 'FS/GS', 'SS naming the DS:RSI source'
    return 'admitted', ''

def lockable(k, op, modrm):
    """I3 (fexc_slice1.inc `Lockable`, SDM Vol.2 LOCK): the memory-destination forms of add/or/adc/sbb/and/sub/xor, group 1
    but cmp, xchg, not/neg, inc/dec, bts/btr/btc, cmpxchg, xadd, cmpxchg8b/16b."""
    if modrm is None or modrm >> 6 == 3: return False
    reg = (modrm >> 3) & 7
    if k == 'p':
        return (op < 0x38 and op & 7 <= 1) or (op in (0x80, 0x81, 0x83) and reg != 7) or op in (0x86, 0x87) or \
               (op in (0xF6, 0xF7) and reg in (2, 3)) or (op in (0xFE, 0xFF) and reg <= 1)
    return k == '0f' and (op in (0xAB, 0xB3, 0xBB, 0xB0, 0xB1, 0xC0, 0xC1) or (op == 0xBA and reg >= 5) or (op == 0xC7 and reg == 1))

def sse_form(m, pfx, op, modrm):
    """fexc_slice1.inc Sse() (I5a): ALLOW, or (UNS, why) for a refused SSE-map form, or None when the opcode is no SSE form
    (the integer rules decide). m: 1 = 0f, 5 = 0f38, 6 = 0f3a; pfx: the mandatory prefix (F3/F2 win over 66)."""
    mod, reg = modrm >> 6, (modrm >> 3) & 7
    form, reg_only, mem_only = None, False, False
    if m == 1 and pfx == 0:
        if op in (0x10, 0x11): form = 'u'
        elif op in (0x12, 0x13, 0x16, 0x17, 0x2E, 0x2F, 0x5A): form = 's'
        elif op in (0x14, 0x15, 0x28, 0x29, 0x2B, 0x50, 0xC2, 0xC6) or 0x51 <= op <= 0x5F: form = 'a'
        mem_only, reg_only = op in (0x13, 0x17, 0x2B), op == 0x50
    elif m == 1 and pfx == 0x66:
        if op in (0x10, 0x11): form = 'u'
        elif op in (0x12, 0x13, 0x16, 0x17, 0x2E, 0x2F, 0x6E, 0x7E, 0xD6, 0xC4): form = 's'
        elif (op in (0x14, 0x15, 0x28, 0x29, 0x2B, 0x50, 0x51, 0x70, 0x74, 0x75, 0x76, 0x7C, 0x7D, 0x7F, 0xC2, 0xC5, 0xC6, 0xD7, 0xE7)
              or 0x54 <= op <= 0x6F or 0xD0 <= op <= 0xD5 or 0xD8 <= op <= 0xE6 or 0xE8 <= op <= 0xEF or 0xF1 <= op <= 0xF6 or 0xF8 <= op <= 0xFE):
            form = 'a'
        elif op in (0x71, 0x72, 0x73) and mod == 3 and (reg in (2, 4, 6) or (op == 0x73 and reg in (3, 7))) and not (op == 0x73 and reg == 4):
            form = 'a'
        mem_only, reg_only = op in (0x13, 0x17, 0x2B, 0xE7), op in (0x50, 0xC5, 0xD7)
    elif m == 1 and pfx == 0xF3:
        if op in (0x6F, 0x7F): form = 'u'
        elif op in (0x12, 0x16, 0x5B, 0x70): form = 'a'
        elif op in (0x10, 0x11, 0x2A, 0x2C, 0x2D, 0x51, 0x52, 0x53, 0x58, 0x59, 0x5A, 0x5C, 0x5D, 0x5E, 0x5F, 0x7E, 0xC2, 0xE6): form = 's'
    elif m == 1 and pfx == 0xF2:
        if op == 0xF0: form = 'u'
        elif op in (0x70, 0x7C, 0x7D, 0xD0, 0xE6): form = 'a'
        elif op in (0x10, 0x11, 0x12, 0x2A, 0x2C, 0x2D, 0x51, 0x58, 0x59, 0x5A, 0x5C, 0x5D, 0x5E, 0x5F, 0xC2): form = 's'
        mem_only = op == 0xF0
    elif m == 5 and pfx == 0x66:
        if 0x20 <= op <= 0x25 or 0x30 <= op <= 0x35: form = 's'
        elif op <= 0x0B or op in (0x10, 0x14, 0x15, 0x17, 0x1C, 0x1D, 0x1E, 0x28, 0x29, 0x2A, 0x2B) or 0x37 <= op <= 0x41: form = 'a'
        mem_only = op == 0x2A
    elif m == 6 and pfx == 0x66:
        if op in (0x0A, 0x0B, 0x14, 0x15, 0x16, 0x17, 0x20, 0x21, 0x22): form = 's'
        elif 0x08 <= op <= 0x0F or 0x40 <= op <= 0x42: form = 'a'
    if form is None:
        if m == 6 and pfx == 0x66 and 0x60 <= op <= 0x63: return UNS, 'pcmpestri/m, pcmpistri/m (FEX host helper)'
        if m == 1 and pfx == 0x66 and op == 0xF7: return UNS, 'maskmovdqu'
        if (m == 5 and pfx == 0x66 and op >= 0xDB) or (m == 6 and pfx == 0x66 and op in (0x44, 0xDF)): return UNS, 'AES/PCLMULQDQ'
        if ((m == 1 and pfx in (0, 0x66) and (op in (0x2A, 0x2C, 0x2D) or (pfx == 0 and (0x60 <= op <= 0x7F or op in (0xC4, 0xC5) or op >= 0xD0))))
                or (m == 1 and pfx in (0xF3, 0xF2) and op == 0xD6) or (m in (5, 6) and pfx == 0)):
            return UNS, 'MMX register form (chunk I6)'
        return None
    if (reg_only and mod != 3) or (mem_only and mod == 3): return UNS, 'SSE form with an operand x86 makes #UD'
    return ALLOW

def x87_form(op, m):
    """fexc_slice1.inc X87 (I6): (ALLOW|SYS|UNS, why)."""
    mod, reg = m >> 6, (m >> 3) & 7
    ok, res = (ALLOW, ''), (SYS, 'x87 control/state')
    helper, alias = (UNS, 'transcendental/remainder/scale (FEX host helper)'), (UNS, 'undocumented or reserved x87 encoding')
    if mod != 3:
        if op in (0xD8, 0xDA, 0xDC, 0xDE): return ok
        if op == 0xD9: return alias if reg == 1 else res if 4 <= reg <= 6 else ok
        if op == 0xDB: return alias if reg in (4, 6) else res if reg in (5, 7) else ok
        if op == 0xDD: return alias if reg == 5 else res if reg in (4, 6) else ok
        return (UNS, 'fbld/fbstp (FEX host helper)') if reg in (4, 6) else ok
    if op == 0xD8: return ok
    if op == 0xD9:
        if reg <= 1 or m == 0xD0 or m in (0xE0, 0xE1, 0xE4, 0xE5, 0xF6, 0xF7, 0xFA, 0xFC) or 0xE8 <= m <= 0xEE: return ok
        if m == 0xF4: return UNS, 'fxtract (FEX: wrong for a denormal double)'
        return helper if m in (0xF0, 0xF1, 0xF2, 0xF3, 0xF5, 0xF8, 0xF9, 0xFB, 0xFD, 0xFE, 0xFF) else alias
    if op == 0xDA: return ok if reg <= 3 or m == 0xE9 else alias
    if op == 0xDB:
        if reg <= 3 or reg in (5, 6): return ok
        if m in (0xE2, 0xE3): return res
        return (UNS, 'feni/fdisi/fsetpm') if m in (0xE0, 0xE1, 0xE4) else alias
    if op == 0xDC: return alias if reg in (2, 3) else ok
    if op == 0xDD: return alias if reg == 1 or reg >= 6 else ok
    if op == 0xDE: return alias if reg == 2 or (reg == 3 and m != 0xD9) else ok
    return ok if m == 0xE0 or reg in (5, 6) else alias

def classify(b, mnem):
    """-> (class, why). Classes: admitted SSE AVX x87 LOCK string system BMI FS/GS small-int other bad"""
    p = parse(b)
    if p is None or mnem.startswith('(bad)') or mnem.startswith('.byte'): return 'bad', ''
    pre, k, op, modrm = p['pre'], p['kind'], p['op'], p['modrm']
    base = mnem.split()[-1] if mnem else ''
    if k == 'vex':
        v = vex(b)
        if v and vex_allowed(v, b): return segment(b, k, p['op']) or ('admitted', '')
        return ('BMI' if base in BMI or base[:-1] in BMI else 'AVX'), 'vex/evex'
    if k == 'xop': return 'other', 'xop'
    if k == 'p' and (0xA4 <= op <= 0xA7 or 0xAA <= op <= 0xAF): return string(leading(b), op)     # I2
    # I5a: SSE..SSE4.2 (fexc_slice1.inc Sse), CRC32, LDMXCSR/STMXCSR (system: the resident's transactions)
    if k in ('0f', '0f38', '0f3a') and modrm is not None:
        lead = leading(b)
        p66, pf2, pf3 = 0x66 in lead, 0xF2 in lead, 0xF3 in lead
        if k == '0f38' and op in (0xF0, 0xF1) and pf2 and not pf3:
            if 0x67 in lead: return 'other', 'address-size prefix on an admitted opcode'
            return segment(b, k, op) or ('admitted', '')
        if k == '0f' and op == 0xAE and modrm >> 6 != 3 and (modrm >> 3) & 7 in (2, 3) and not (p66 or pf2 or pf3): return 'system', 'ldmxcsr/stmxcsr'
        v = sse_form({'0f': 1, '0f38': 5, '0f3a': 6}[k], 0xF3 if pf3 else 0xF2 if pf2 else 0x66 if p66 else 0, op, modrm)
        if v == ALLOW:
            if (pf2 and pf3) or ((pf2 or pf3) and p66): return 'SSE', 'several mandatory prefixes'
            if 0xF0 in lead: return 'LOCK', 'LOCK on a form that is not lockable (#UD)'
            if 0x67 in lead: return 'other', 'address-size prefix on an admitted opcode'
            if k == '0f3a' and op == 0x21 and modrm >> 6 != 3 and (b[-1] & 0xF) & (1 << (b[-1] >> 4 & 3)):
                return 'SSE', 'insertps m32 zeroing its inserted element'
            return segment(b, k, op) or ('admitted', '')
        if v is not None: return 'SSE', v[1]
    # I6: x87 (fexc_slice1.inc X87, series23-i6): admitted forms, the resident's (system), refused ones (class x87, why)
    if k == 'p' and ((0xD8 <= op <= 0xDF and modrm is not None) or op == 0x9B):
        v, why = (ALLOW, '') if op == 0x9B else x87_form(op, modrm)
        if v == SYS: return 'system', 'x87 control/state (the resident\'s)'
        if v != ALLOW: return 'x87', why
        if 0xF0 in pre: return 'LOCK', 'LOCK on a form that is not lockable (#UD)'
        if pre & {0x66, 0xF2, 0xF3}: return 'x87', '66/F2/F3 prefix on an x87 form'
        seg = segment(b, k, op)
        if seg: return seg
        if 0x67 in pre: return 'other', 'address-size prefix on an admitted opcode'
        return 'admitted', ''
    if k == 'p': v, br = primary(op, modrm)
    elif k == '0f' and op in (0xB8, 0xBC, 0xBD) and last_mandatory(b) == 0xF3 and 0xF2 not in pre: v, br = ALLOW, False   # I1
    elif k == '0f': v, br = two(op, not (0xF2 in pre or 0xF3 in pre), modrm)
    elif k == '0f38': v, br = (SYS if 0x80 <= op <= 0x82 else UNS), False
    else: v, br = UNS, False
    plainmod = modrm
    # I3: cmpxchg8b/16b m (fexc: one caspal, locked or not; no 66/F2/F3)
    if k == '0f' and op == 0xC7 and modrm is not None and (modrm >> 3) & 7 == 1 and modrm >> 6 != 3:
        if pre & {0x66, 0xF2, 0xF3}: return 'LOCK', 'prefix on cmpxchg8b/16b'
        return segment(b, k, op) or ('admitted', '')
    # fences and endbr64 are admitted whatever the opcode table says
    if k == '0f' and op == 0xAE and modrm in (0xE8, 0xF0, 0xF8) and not (pre & {0x66, 0xF2, 0xF3}):
        return segment(b, k, op) or ('admitted', '')
    if v != ALLOW:
        if k == '0f' and op == 0x1E and modrm == 0xFA and 0xF3 in pre and not (pre & {0xF0, 0xF2, 0x66, 0x67, 0x64, 0x65}): return 'admitted', ''
    else:
        if 0xF0 in pre and not lockable(k, op, modrm): return 'LOCK', 'LOCK on a form that is not lockable (#UD)'   # I3
        if 0xF2 in pre: return 'other', 'REPNE/bnd prefix on an admitted opcode'
        if 0xF3 in pre and not (k == 'p' and (op == 0xC3 or (op == 0x90 and not (p['rex'] & 1)))) and not (k == '0f' and op in (0xB8, 0xBC, 0xBD)):
            return 'other', 'REP prefix on an admitted opcode'
        seg = segment(b, k, op)      # I7: FS/GS overrides admitted on memory operands
        if seg: return seg
        if 0x67 in pre: return 'other', 'address-size prefix on an admitted opcode'
        if br and 0x66 in pre: return 'other', 'operand-size prefix on a branch'
        if k == '0f' and 0xC8 <= op <= 0xCF and 0x66 in pre and not p['rex'] & 8: return 'other', '16-bit bswap'
        if k == '0f' and op in (0xA4, 0xA5, 0xAC, 0xAD) and 0x66 in pre: return 'small-int', '16-bit shld/shrd'
        if k == 'p' and op in (0x9C, 0xC8, 0xC9) and 0x66 in pre: return 'small-int', 'operand-size prefix on pushf/enter/leave'
        if k == 'p' and op == 0xC8 and enter_level(b) != 0: return 'small-int', 'enter with a nesting level'
        return 'admitted', ''
    if k == 'p' and op == 0xCC: return 'int3', 'alignment padding / breakpoint'
    if v == SYS: return 'system', ''
    # refused: say what it is
    if k == 'p' and (0xD8 <= op <= 0xDF or op == 0x9B): return 'x87', ''
    if k == '0f' and op == 0xAE:
        r = (modrm >> 3) & 7
        return ('x87' if r <= 1 else 'SSE' if r in (2, 3) else 'other'), 'fxsave/ldmxcsr/xsave group'
    if k == '0f' and op in (0xB8, 0xBC, 0xBD) and 0xF3 in pre: return 'BMI', 'popcnt/tzcnt/lzcnt'
    if k in ('0f38', '0f3a') and not (k == '0f38' and op in (0xF0, 0xF1, 0xF5, 0xF6, 0xF8, 0xF9, 0xFA, 0xFB, 0xFC)): return 'SSE', 'incl. AES/SHA/PCLMUL'
    if k == '0f' and op in SSE2 and not (op == 0x7E and 0 and False): return 'SSE', ''
    if 0xF0 in pre: return 'LOCK', ''
    if k == 'p' and (0xA4 <= op <= 0xA7 or 0xAA <= op <= 0xAF): return 'string', ''
    if k == 'p' and op in SMALL1 or k == '0f' and op in SMALL2 and op != 0xC7: return 'small-int', ''
    if k == '0f' and op == 0xC7 and (modrm >> 3) & 7 == 1: return 'small-int', 'cmpxchg8b/16b'
    return 'other', ''

def last_mandatory(b):
    """The last of 66/F2/F3 before the opcode (FEX's LastEscapePrefix)"""
    last = None
    for x in b:
        if x in (0x66, 0xF2, 0xF3): last = x
        elif x not in LEGACY and not 0x40 <= x <= 0x4F: break
    return last

def enter_level(b):
    i = 0
    while b[i] in LEGACY or 0x40 <= b[i] <= 0x4F: i += 1
    return b[i + 3] & 0x1F if i + 3 < len(b) else 0

PFX = {'lock', 'rep', 'repe', 'repne', 'repz', 'repnz', 'notrack', 'bnd', 'data16', 'cs', 'ds', 'es', 'ss', 'fs', 'gs', 'addr32', 'xacquire', 'xrelease', 'rex', 'rex.W', 'rex.R', 'rex.X', 'rex.B', 'rex.WR', 'rex.WB', 'rex.WX'}
REG = re.compile(r'%([a-z]+)\d*')
def form(text):
    """'movq\\t%rax, 8(%rbx)' -> ('movq', 'r,m'); prefixes folded into the mnemonic."""
    text = text.split('#')[0].strip()
    parts = text.split('\t'); parts = [x for x in parts if x != '']
    words = []
    while parts and parts[0].split()[0] in PFX and len(parts) > 1:
        words.append(parts.pop(0))
    if not parts: return text, ''
    mn = ' '.join(words + [parts[0]]); ops = parts[1] if len(parts) > 1 else ''
    out, depth, cur = [], 0, ''
    for ch in ops:
        if ch == '(': depth += 1
        if ch == ')': depth -= 1
        if ch == ',' and depth == 0: out.append(cur.strip()); cur = ''
        else: cur += ch
    if cur.strip(): out.append(cur.strip())
    def shape(o):
        o = o.split(' <')[0].strip(); star = '*' if o.startswith('*') else ''; o = o.lstrip('*')
        if o.startswith('$'): return star + 'i'
        if '(' in o or ':' in o or re.match(r'^-?0x[0-9a-f]+$', o) and not star and mn.split()[-1][0] not in 'jc' : return star + ('m' if '(' in o or ':' in o else 'a')
        m = REG.match(o)
        if m:
            n = m.group(1); return star + ('x' if n == 'xmm' else 'y' if n == 'ymm' else 'z' if n == 'zmm' else n if n in ('mm', 'st', 'k', 'cr', 'dr') else 'sr' if n in ('cs', 'ds', 'es', 'ss', 'fs', 'gs') else 'r')
        return star + 'rel'
    return mn, ','.join(shape(o) for o in out)

def wrap(code, path, is64, base=0):
    """minimal ELF with one executable .text, for objdump on raw bytes"""
    shstr = b'\0.text\0.shstrtab\0'
    shoff = (64 + len(code) + len(shstr) + 7) & ~7
    if is64:
        eh = b'\x7fELF' + bytes([2, 1, 1, 0]) + bytes(8) + struct.pack('<HHIQQQIHHHHHH', 1, 62, 1, 0, 0, shoff, 0, 64, 0, 0, 64, 3, 2)
        sh = lambda n, t, f, a, o, s: struct.pack('<IIQQQQIIQQ', n, t, f, a, o, s, 0, 0, 1, 0)
    else:
        eh = b'\x7fELF' + bytes([1, 1, 1, 0]) + bytes(8) + struct.pack('<HHIIIIIHHHHHH', 1, 3, 1, 0, 0, shoff, 0, 52, 0, 0, 40, 3, 2)
        eh += bytes(64 - len(eh))
        sh = lambda n, t, f, a, o, s: struct.pack('<IIIIIIIIII', n, t, f, a, o, s, 0, 0, 1, 0)
    secs = sh(0, 0, 0, 0, 0, 0) + sh(1, 1, 6, base, 64, len(code)) + sh(7, 3, 0, 0, 64 + len(code), len(shstr))
    open(path, 'wb').write(eh + code + shstr + bytes(shoff - 64 - len(code) - len(shstr)) + secs)

def sweep(path):
    """objdump -d one ELF -> {(section, class, mnemonic, form): [count, first_addr, why]}, per-section insn totals"""
    agg, tot = {}, collections.Counter()
    pr = subprocess.Popen(['objdump', '-d', path], stdout=subprocess.PIPE, text=True, errors='replace')
    sec, pend = '?', None
    for ln in pr.stdout:
        if ln.startswith('Disassembly of section '): sec = ln.split()[3].rstrip(':'); continue
        if '\t' not in ln or ':' not in ln: continue
        pre, txt = ln.split('\t', 1)
        addr, hx = pre.split(':', 1)
        hx = hx.strip()
        if not txt.strip() or not re.fullmatch(r'(?:[0-9a-f]{2} ?)+', hx): continue  # raw-bytes-only continuation
        b = bytes.fromhex(hx.replace(' ', ''))
        addr = addr.strip()
        # llvm-objdump prints some prefixes (lock, data16, cs, ...) as an instruction of their own: glue to the next
        if txt.strip() in PFX:
            if pend is None: pend = [addr, b'', '']
            pend[1] += b; pend[2] += txt.strip() + '\t'
            continue
        if pend is not None: addr, b, txt, pend = pend[0], pend[1] + b, pend[2] + txt, None
        mn, fm = form(txt)
        cls, why = classify(b, mn)
        k = (sec, cls, mn, fm)
        e = agg.get(k)
        if e is None: agg[k] = [1, addr, why]
        else: e[0] += 1
        tot[sec] += 1
    pr.wait()
    return [[*k, *v] for k, v in agg.items()]

def job(a):
    name, path, group = a
    return name, group, sweep(path)

def selftest():
    h = bytes.fromhex
    cases = [('f390', 'pause', 'admitted'), ('0faee8', 'lfence', 'admitted'), ('f0488708', 'lock xchg', 'admitted'), ('f04887c1', 'lock xchg reg', 'LOCK'),
             ('f3a4', 'rep movsb', 'admitted'), ('f348ab', 'rep stos', 'admitted'), ('f2ae', 'repnz scas', 'admitted'),
             ('f2a4', 'repnz movsb', 'string'), ('67f3a4', 'addr32 rep movsb', 'string'), ('64f348a5', 'rep movs %fs:', 'admitted'),
             ('64aa', 'stos %fs', 'FS/GS'), ('36ac', 'lods %ss:', 'FS/GS'), ('f30f1efa', 'endbr64', 'admitted'), ('64488b042528000000', 'mov %fs:', 'admitted'),
             ('6548ff0425f0ff0000', 'incq %gs:', 'admitted'), ('644889c8', 'fs mov reg', 'FS/GS'), ('642e488b00', 'fs cs mov', 'FS/GS'),
             ('36488b03', 'ss mov (%rbx)', 'FS/GS'), ('36488b0424', 'ss mov (%rsp)', 'admitted'), ('3e488b4500', 'ds mov (%rbp)', 'FS/GS'),
             ('3e48ff24c500000000', 'notrack jmp *tbl', 'admitted'), ('65c4e2f0f200', 'andn %gs:', 'admitted'), ('f065480fc108', 'lock xadd %gs:', 'admitted'),
             ('65c3', 'gs ret', 'FS/GS'), ('3e4c8b2424', 'ds mov (%rsp),%r12', 'FS/GS'), ('3e498b0424', 'ds mov (%r12)', 'admitted'),
             ('0f22d8', 'mov %cr3', 'system'), ('0f28c1', 'movaps', 'admitted'), ('0f6fc1', 'movq mm', 'SSE'), ('660f3a63c100', 'pcmpistri', 'SSE'), ('0fae10', 'ldmxcsr', 'system'), ('f20f38f1c1', 'crc32', 'admitted'), ('66f30f10c1', 'movss 66', 'SSE'), ('660f73f804', 'pslldq', 'admitted'), ('c5f877', 'vzeroupper', 'AVX'), ('d9c0', 'fld', 'admitted'), ('d9fe', 'fsin', 'x87'), ('dbe3', 'fninit', 'system'),
             ('f3480fb8c1', 'popcnt', 'admitted'), ('f0480fb10a', 'lock cmpxchg', 'admitted'), ('f0480fc70e', 'lock cmpxchg16b', 'admitted'),
             ('f04801c8', 'lock add reg', 'LOCK'), ('f0488908', 'lock mov', 'LOCK'), ('f2f0480108', 'xacquire lock add', 'other'),
             ('f0f71b', 'lock neg', 'admitted'), ('f0480fba2b05', 'lock bts', 'admitted'), ('f0480fba2305', 'lock bt', 'LOCK'), ('480fa4c10c', 'shld', 'admitted'), ('f2c3', 'bnd ret', 'other'),
             ('fc', 'cld', 'admitted'), ('9c', 'pushfq', 'admitted'), ('9d', 'popfq', 'system'), ('c8100001', 'enter', 'small-int'), ('c9', 'leave', 'admitted'),
             ('480fc70e', 'cmpxchg16b', 'admitted'), ('66480fc70e', 'o16 cmpxchg16b', 'LOCK'), ('0f0d08', 'prefetchw', 'small-int'), ('0f1808', 'prefetcht0', 'admitted'), ('660fa4c10c', 'shldw', 'small-int'),
             ('c4e2fbf5c1', 'pdep', 'BMI'), ('c4e27cf2c1', 'andn', 'BMI'), ('c4e278f2c1', 'andn', 'admitted'), ('f3480fbc00', 'tzcnt', 'admitted'),
             ('f3c3', 'rep ret', 'admitted'), ('c3', 'ret', 'admitted'), ('cc', 'int3', 'int3'), ('3effe0', 'notrack jmp', 'admitted'), ('0f05', 'syscall', 'system'),
             ('c5f8f7', 'bmi?', 'AVX'), ('c4e260f7c1', 'shlx', 'admitted'), ('c4e2f8f7c1', 'shlxq', 'admitted')]
    for hx, mn, want in cases:
        got = classify(h(hx), mn if mn != 'shlx' else 'shlxq')[0]
        if mn in ('popcnt', 'pdep', 'andn', 'tzcnt'): got = classify(h(hx), mn)[0]
        assert got == want, (mn, got, want)
    assert form('movq\t%rax, 8(%rbx,%rcx,4)') == ('movq', 'r,m') and form('callq\t0x1fba2 <.text+0x1fba2>') == ('callq', 'rel')
    assert form('lock\t\tcmpxchgl\t%ecx, (%rdi)') == ('lock cmpxchgl', 'r,m') and form('movl\t$0x0, %eax') == ('movl', 'i,r')
    print('selftest ok')

def main(raw, out_md):
    x = os.path.join(raw, 'x'); w = os.path.join(raw, 'wrap'); os.makedirs(w, exist_ok=True)
    head = open(f'{x}/head.bin', 'rb').read(); tail = open(f'{x}/tail.bin', 'rb').read()
    # decompressor tail: cut where the first long printable run (rodata strings) starts
    m = re.search(rb'[\x20-\x7e]{24,}', tail[0x400:])
    cut = 0x400 + m.start() if m else len(tail)
    jobs = []
    for n, code, is64, base, grp in (('dec head 32-bit [0,0x200)', head[:0x200], False, 0, 'decompressor'),
                                     ('dec head 64-bit [0x200,end)', head[0x200:], True, 0x200, 'decompressor'),
                                     (f'dec tail 64-bit, code part [0,{cut:#x})', tail[:cut], True, 0, 'decompressor')):
        p = f'{w}/{abs(hash(n))}.elf'; wrap(code, p, is64, base); jobs.append((n, p, grp))
    jobs.append(('vmlinux', f'{x}/vmlinux.elf', 'vmlinux'))
    chain = ('busybox', 'ld-linux', 'libc.so', 'libm.so')
    for f in sorted(os.listdir(f'{x}/initrd')):
        if f == 'init': continue
        g = 'initrd:init chain (busybox, ld.so, libc, libm)' if any(c in f for c in chain) else 'initrd:other executables' if '.so' not in f else 'initrd:other libraries'
        jobs.append((f.replace('__', '/'), f'{x}/initrd/{f}', g))
    res = collections.defaultdict(list)
    with Pool(6) as pool:
        for name, grp, rows in pool.imap_unordered(job, jobs, chunksize=1):
            res[(grp, name)] = rows
            print('done', name, sum(r[4] for r in rows), flush=True)
    json.dump([[g, n, rows] for (g, n), rows in res.items()], open(os.path.join(raw, 'census.json'), 'w'))
    open(os.path.join(raw, 'tail_cut.txt'), 'w').write(f'{cut:#x} of {len(tail):#x}\n')

if __name__ == '__main__':
    if sys.argv[1] == '--selftest': selftest()
    else: main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
