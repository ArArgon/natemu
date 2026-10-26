#!/usr/bin/env python3
"""Bounded x86-TSO reference for the finite I4-V ordinary-WB litmus profiles.

Contract: INV-03-04 claim40 / retained Intel 253668-092US section11.2.2.
Each symbolic location is a distinct aligned32-bit ordinary-WB RAM word.
Instructions issue in guest order. Stores enter a per-CPU FIFO; independent
flush steps make its oldest store globally visible. Loads forward the youngest
same-location buffered store, otherwise read the one shared coherent memory.
MFENCE drains the issuing buffer. Implicit-locked XCHG drains that buffer and
has one globally ordered indivisible memory/register effect. No other CPU's
buffer is drained merely because a fence or XCHG executes.

This is an explicit finite reference, not herd, a universal x86 proof, an Arm
model, a probability model, or a model of guest translation/device effects.
Native emitter/runtime admission and local LFENCE completion are separate
obligations. There are no runtime, producer, or implementation-model imports.
"""

from collections import deque
from dataclasses import dataclass
from functools import lru_cache


def require(condition, message):
    if not condition:
        raise ValueError(message)


def word(value, name='word'):
    require(type(value) is int and 0 <= value < 1 << 32, name + ' must be u32')
    return value


@dataclass(frozen=True)
class Litmus:
    name: str
    # Operations: ('load', destination, address), ('store', address, value),
    # ('fence',), ('xchg', destination, address, input_value). Values are u32
    # constants or names of registers written earlier by the same participant.
    programs: tuple
    initial: tuple
    outputs: tuple


PROFILES = {
    'sb': Litmus('sb',
                 ((('store', 'x', 1), ('load', 'r0', 'y')),
                  (('store', 'y', 1), ('load', 'r1', 'x'))),
                 (('x', 0), ('y', 0)), ((0, 'r0'), (1, 'r1'))),
    'mp': Litmus('mp',
                 ((('store', 'x', 1), ('store', 'y', 1)),
                  (('load', 'flag', 'y'), ('load', 'data', 'x'))),
                 (('x', 0), ('y', 0)), ((1, 'flag'), (1, 'data'))),
    'lb': Litmus('lb',
                 ((('load', 'r0', 'x'), ('store', 'y', 1)),
                  (('load', 'r1', 'y'), ('store', 'x', 1))),
                 (('x', 0), ('y', 0)), ((0, 'r0'), (1, 'r1'))),
    'wrc': Litmus('wrc',
                  ((('store', 'x', 1),),
                   (('load', 'r0', 'x'), ('store', 'y', 1)),
                   (('load', 'r1', 'y'), ('load', 'r2', 'x'))),
                  (('x', 0), ('y', 0)), ((1, 'r0'), (2, 'r1'), (2, 'r2'))),
    'iriw': Litmus('iriw',
                   ((('store', 'x', 1),), (('store', 'y', 1),),
                    (('load', 'r0', 'x'), ('load', 'r1', 'y')),
                    (('load', 'r2', 'y'), ('load', 'r3', 'x'))),
                   (('x', 0), ('y', 0)), ((2, 'r0'), (2, 'r1'), (3, 'r2'), (3, 'r3'))),
    'sb-mfence': Litmus('sb-mfence',
                        ((('store', 'x', 1), ('fence',), ('load', 'r0', 'y')),
                         (('store', 'y', 1), ('fence',), ('load', 'r1', 'x'))),
                        (('x', 0), ('y', 0)), ((0, 'r0'), (1, 'r1'))),
    'sb-xchg': Litmus('sb-xchg',
                      ((('store', 'x', 1), ('xchg', 'old0', 'd0', 1), ('load', 'r0', 'y')),
                       (('store', 'y', 1), ('xchg', 'old1', 'd1', 2), ('load', 'r1', 'x'))),
                      (('d0', 0), ('d1', 0), ('x', 0), ('y', 0)), ((0, 'r0'), (1, 'r1'))),
}


def profile(test):
    if type(test) is str:
        require(test in PROFILES, 'unknown finite ordering profile')
        return PROFILES[test]
    require(type(test) is Litmus, 'expected a finite Litmus declaration')
    return test


def validate(test):
    require(type(test.name) is str and bool(test.name), 'named litmus')
    require(type(test.programs) is tuple and 1 <= len(test.programs) <= 4, 'one to four participants')
    require(type(test.initial) is tuple and 1 <= len(test.initial) <= 8, 'one to eight RAM words')
    memory = dict(test.initial)
    require(len(memory) == len(test.initial), 'unique RAM word names')
    for name, value in test.initial:
        require(type(name) is str and bool(name), 'named RAM word')
        word(value, 'initial memory')
    registers = []
    require(sum(map(len, test.programs)) <= 24, 'at most24 finite guest operations')
    for program in test.programs:
        require(type(program) is tuple and bool(program), 'nonempty participant program')
        defined = set()
        for op in program:
            require(type(op) is tuple and bool(op), 'tuple operation')
            kind = op[0]
            require(kind in ('load', 'store', 'fence', 'xchg'), 'unsupported operation')
            require(len(op) == {'load': 3, 'store': 3, 'fence': 1, 'xchg': 4}[kind], 'operation shape')
            if kind == 'fence':
                continue
            address = op[1] if kind == 'store' else op[2]
            require(address in memory, 'operation addresses declared RAM word')
            if kind in ('store', 'xchg'):
                value = op[-1]
                if type(value) is str:
                    require(value in defined, 'no uninitialized register dependency')
                else:
                    word(value, 'store/exchange input')
            if kind in ('load', 'xchg'):
                require(type(op[1]) is str and bool(op[1]), 'named result register')
                defined.add(op[1])
        registers.append(tuple(sorted(defined)))
    require(type(test.outputs) is tuple and bool(test.outputs), 'explicit outcome register order')
    require(len(set(test.outputs)) == len(test.outputs), 'unique outcome registers')
    for cpu, reg in test.outputs:
        require(type(cpu) is int and 0 <= cpu < len(registers) and reg in registers[cpu], 'valid outcome register')
    return tuple(sorted(memory)), tuple(registers)


@dataclass(frozen=True)
class State:
    pcs: tuple
    buffers: tuple
    memory: tuple
    registers: tuple


def initial_state(test):
    addresses, registers = validate(test)
    memory = dict(test.initial)
    return State((0,) * len(test.programs), ((),) * len(test.programs),
                 tuple(memory[a] for a in addresses),
                 tuple((0,) * len(names) for names in registers))


def replace_at(items, index, value):
    return items[:index] + (value,) + items[index + 1:]


def _transitions(test, state, addresses, register_names):
    """One FIFO visibility event or one in-order guest instruction per edge."""
    for cpu, buffer in enumerate(state.buffers):
        if buffer:
            address, value = buffer[0]
            yield ('flush', cpu), State(state.pcs, replace_at(state.buffers, cpu, buffer[1:]),
                                        replace_at(state.memory, address, value), state.registers)
    for cpu, program in enumerate(test.programs):
        pc = state.pcs[cpu]
        if pc == len(program):
            continue
        op = program[pc]
        kind = op[0]
        buffer = state.buffers[cpu]
        if kind in ('fence', 'xchg') and buffer:
            continue
        memory, buffers, registers = state.memory, state.buffers, state.registers
        if kind != 'fence':
            address = addresses.index(op[1] if kind == 'store' else op[2])
            if kind in ('store', 'xchg'):
                value = (registers[cpu][register_names[cpu].index(op[-1])]
                         if type(op[-1]) is str else op[-1])
            if kind == 'store':
                buffers = replace_at(buffers, cpu, buffer + ((address, value),))
            else:
                observed = memory[address]
                if kind == 'load':
                    for pending_address, pending_value in reversed(buffer):
                        if pending_address == address:
                            observed = pending_value
                            break
                else:
                    memory = replace_at(memory, address, value)
                index = register_names[cpu].index(op[1])
                registers = replace_at(registers, cpu, replace_at(registers[cpu], index, observed))
        yield ('execute', cpu), State(replace_at(state.pcs, cpu, pc + 1), buffers, memory, registers)


@dataclass
class Enumeration:
    outcomes: frozenset
    states: int
    transitions: int
    terminal_states: int
    # One witness is sufficient to show possibility; no schedule frequencies.
    witnesses: dict


def enumerate_outcomes(test, max_states=250000):
    test = profile(test)
    addresses, registers = validate(test)
    require(type(max_states) is int and max_states > 0, 'positive state bound')
    start = initial_state(test)
    pending = deque([start])
    parents = {start: None}
    witnesses = {}
    edges = terminals = 0
    while pending:
        current = pending.popleft()
        if all(pc == len(program) for pc, program in zip(current.pcs, test.programs)) and not any(current.buffers):
            terminals += 1
            outcome = tuple(current.registers[cpu][registers[cpu].index(reg)] for cpu, reg in test.outputs)
            if outcome not in witnesses:
                path, at = [], current
                while parents[at] is not None:
                    previous, action = parents[at]
                    path.append(action)
                    at = previous
                witnesses[outcome] = tuple(reversed(path))
        for action, successor in _transitions(test, current, addresses, registers):
            edges += 1
            if successor not in parents:
                require(len(parents) < max_states, 'reference state bound exceeded; no partial allowed-set result')
                parents[successor] = current, action
                pending.append(successor)
    require(witnesses, 'finite model has no complete execution')
    return Enumeration(frozenset(witnesses), len(parents), edges, terminals, witnesses)


def replay_witness(test, actions):
    """Check that a reported possible outcome has a legal finite execution."""
    test = profile(test)
    addresses, registers = validate(test)
    state = initial_state(test)
    for action in actions:
        matching = [successor for label, successor in _transitions(test, state, addresses, registers) if label == action]
        require(len(matching) == 1, 'witness action is not a unique enabled transition')
        state = matching[0]
    require(all(pc == len(program) for pc, program in zip(state.pcs, test.programs)) and not any(state.buffers),
            'witness must finish guest instructions and outstanding stores')
    return tuple(state.registers[cpu][registers[cpu].index(reg)] for cpu, reg in test.outputs), state


@lru_cache(maxsize=7)
def allowed_outcomes(name):
    require(type(name) is str and name in PROFILES, 'known conformance profile')
    return enumerate_outcomes(name).outcomes


def check_outcome(name, outcome):
    test = profile(name)
    require(type(outcome) in (tuple, list) and len(outcome) == len(test.outputs), 'outcome shape')
    for value in outcome:
        word(value, 'observed result')
    allowed = allowed_outcomes(name) if type(name) is str else enumerate_outcomes(test).outcomes
    require(tuple(outcome) in allowed, 'forbidden outcome for ' + test.name + ': ' + repr(outcome))
    return tuple(outcome)


def check_histogram(name, histogram, expected_trials):
    """histogram = sequence of (outcome sequence, count); zero counts permitted."""
    require(type(expected_trials) is int and expected_trials > 0, 'positive declared trial count')
    require(type(histogram) in (tuple, list) and bool(histogram), 'nonempty outcome histogram')
    seen, total = set(), 0
    for item in histogram:
        require(type(item) in (tuple, list) and len(item) == 2, 'histogram row shape')
        outcome, count = item
        require(type(count) is int and count >= 0, 'nonnegative integer frequency')
        require(type(outcome) in (tuple, list), 'histogram outcome sequence')
        for value in outcome:
            word(value, 'histogram result')
        require(len(outcome) == len(profile(name).outputs), 'histogram outcome width')
        key = tuple(outcome)
        require(key not in seen, 'duplicate outcome row')
        seen.add(key)
        if count:
            check_outcome(name, key)
        total += count
    require(total == expected_trials, 'histogram must account for every declared trial')
    return total


def check_xchg_chain(tokens, returned, final, initial=0):
    """One aligned XCHG per owner; returns the only compatible owner order.

    tokens/returned are dictionaries keyed by the same integer owner identities.
    The private result-store recovery must retain the *first committed* returned
    value. Duplicate exchanges cannot be hidden by dropping owners or by a
    disconnected cycle that happens to contain the final memory value.
    """
    require(type(tokens) is dict and type(returned) is dict and 1 <= len(tokens) <= 4,
            'one to four declared exchange owners')
    require(tokens.keys() == returned.keys(), 'each owner supplies one returned value')
    require(all(type(cpu) is int and 0 <= cpu < 4 for cpu in tokens), 'bounded integer owner identities')
    word(initial, 'initial exchange word')
    word(final, 'final exchange word')
    for value in list(tokens.values()) + list(returned.values()):
        word(value, 'exchange token/result')
    require(initial == 0 and all(tokens.values()) and len(set(tokens.values())) == len(tokens),
            'zero initial sentinel and unique nonzero owner tokens')
    predecessor = {}
    for cpu, value in returned.items():
        require(value == initial or value in tokens.values(), 'returned value must have an actual predecessor')
        require(value not in predecessor, 'two owners cannot consume the same exchange predecessor')
        predecessor[value] = cpu
    current, order = initial, []
    while current in predecessor:
        cpu = predecessor[current]
        require(cpu not in order, 'exchange serialization cycle')
        order.append(cpu)
        current = tokens[cpu]
    require(len(order) == len(tokens), 'exchange chain must include every owner, without disconnected cycles')
    require(current == final, 'final memory must equal the last committed exchange token')
    return tuple(order)


if __name__ == '__main__':
    for name in PROFILES:
        result = enumerate_outcomes(name)
        print(f'{name}: {sorted(result.outcomes)}; {result.states} states/{result.transitions} edges')
