#!/usr/bin/env python3
"""Retain single-instruction FEX address-size32/16 memory bodies."""
import argparse
from pathlib import Path
import probe_controls

probe_controls.SOURCES = {
    'copy': bytes.fromhex('89c1'),
    'store_ecx': bytes.fromhex('890b'),
    'store_edx': bytes.fromhex('8913'),
    'load_edx': bytes.fromhex('8b02'),
    'store_ecx16': bytes.fromhex('678908'),
    'store_edx16': bytes.fromhex('678910'),
    'load16': bytes.fromhex('678b00'),
}
if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory', required=True, type=Path)
    p.add_argument('--producer', required=True, type=Path)
    a = p.parse_args()
    probe_controls.probe(a.directory, a.producer, [0x2000, 0x2002, 0x2005], 32, ('probe_address.py',))
