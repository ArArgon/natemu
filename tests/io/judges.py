"""Judges for the io family's runner cases (`judge = "judges:NAME"`): called with the case's output folder."""
import json
import pathlib


def must_fail(out):
    """A negative control: the run was given a deliberate corruption, so its result.json must say pass: false with at
    least one error. A passing corrupted run means the check cannot see corruption (the harness also calls this on
    exit 0, `judge_on_zero`)."""
    try:
        d = json.loads((pathlib.Path(out) / 'result.json').read_text())
    except (OSError, ValueError):
        d = {}
    if isinstance(d, dict) and d.get('pass') is False and d.get('errors'):
        return True, 'negative control failed as required'
    return False, 'the corrupted run passed: the check cannot see corruption'
