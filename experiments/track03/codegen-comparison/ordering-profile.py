#!/usr/bin/env python3
"""Select a separately retained lowering profile; never claim to enable host TSO."""
from pathlib import Path
import sys


FEX_CONTEXT = '    CTX = FEXCore::Context::Context::CreateNewContext(HostFeatures);'
TCG_ORDER = '#define TCG_TARGET_DEFAULT_MO  0'
PATCHES = {
    'box64': [],  # Existing BOX64_DYNAREC_STRONGMEM option is passed by box64.sh.
    'fex': [('Source/Tools/CodeSizeValidation/Main.cpp', FEX_CONTEXT,
             FEX_CONTEXT + '\n    CTX->SetHardwareTSOSupport(true);\n'
             '    printf("NATEMU_ORDERING hardware-tso-dependent\\n");')],
    'tcg': [('tcg/aarch64/tcg-target-mo.h', TCG_ORDER,
             '#define TCG_TARGET_DEFAULT_MO (TCG_MO_ALL & ~TCG_MO_ST_LD)'),
            ('tcg/aarch64/tcg-target.c.inc',
             'profile=native-aarch64-software-ordering',
             'profile=hardware-tso-dependent')],
}


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('profile source anchor must match exactly once: ' + old)
    return text.replace(old, new, 1)


def self_check():
    for patches in PATCHES.values():
        for _, old, new in patches:
            assert replace_once('prefix\n'+old+'\nsuffix', old, new) == 'prefix\n'+new+'\nsuffix'
            for invalid in ('', old+'\n'+old):
                try:
                    replace_once(invalid, old, new)
                except ValueError:
                    pass
                else:
                    raise AssertionError('missing/duplicate anchor admitted')
    assert 'CF_PARALLEL' not in repr(PATCHES)
    print('PASS: exact-once profile anchors; missing/duplicate sources refused')


def main():
    if sys.argv[1:] == ['--self-check']:
        self_check()
        return
    if len(sys.argv) != 4 or sys.argv[1] not in PATCHES or sys.argv[3] not in ('0', '1'):
        raise SystemExit('usage: ordering-profile.py {box64|fex|tcg} ROOT {0|1}')
    candidate, root, enabled = sys.argv[1], Path(sys.argv[2]), sys.argv[3] == '1'
    # Validate every anchor before changing any source in this fresh build tree.
    changes = []
    if enabled:
        for name, old, new in PATCHES[candidate]:
            path = root/name
            changes.append((path, replace_once(path.read_text(), old, new)))
    for path, text in changes:
        path.write_text(text)
    print('NATEMU_ORDERING ' + ('hardware-tso-dependent' if enabled else 'software-ordering'))


if __name__ == '__main__':
    main()
