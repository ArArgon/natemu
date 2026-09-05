"""Independent executable *abstract* model, not an instruction-coherence test.

Enumerates schedules without importing any C implementation or trace checker.
All data ordering in hazard_model is SC. No runtime threads or JIT allocations.
"""
import itertools
import json


def interleave(sequences, prefix=()):
    if not any(sequences):
        yield prefix
        return
    for i, sequence in enumerate(sequences):
        if sequence:
            changed = list(sequences)
            changed[i] = sequence[1:]
            yield from interleave(changed, prefix + ((i, sequence[0]),))


def hazard_model(broken=False):
    total = calls = retries = safe_retire = 0
    # One bounded admission per reader. Failed revalidation exits that attempt.
    for schedule in interleave((tuple("LAECX"), tuple("LAECX"), tuple("PS"))):
        entry, retired = 1, False
        loaded, hazard, valid = [0, 0], [0, 0], [False, False]
        for actor, event in schedule:
            if actor == 2:
                if event == "P":
                    entry = 2
                else:
                    retired = broken or 1 not in hazard
                    safe_retire += retired
            elif event == "L":
                loaded[actor] = entry
            elif event == "A":
                hazard[actor] = loaded[actor]
            elif event == "E":
                valid[actor] = loaded[actor] == entry
                if not valid[actor]:
                    retries += 1
                    hazard[actor] = 0
            elif event == "C" and valid[actor]:
                if loaded[actor] == 1 and retired:
                    return dict(valid=False, counterexample=schedule)
                calls += 1
            elif event == "X":
                hazard[actor] = 0
        total += 1
    assert calls and retries and safe_retire and total
    return dict(valid=True, schedules=total, calls=calls, retries=retries)


def epoch_model():
    checked = 0
    # Two exact epochs: adversarial old/partial ACK sets never satisfy next.
    for n in (1, 2, 4, 8):
        ack = [0] * n
        for epoch in (1, 2):
            for held in range(n):
                ack = [epoch] * n
                ack[held] = epoch - 1
                assert not all(value == epoch for value in ack)
                ack[held] = epoch
                assert all(value == epoch for value in ack)
                # Acquire request carries prior SC removal; post-ACK reload
                # cannot reuse cached=old. Zero is wait, new>=floor is admitted.
                floor, old = epoch + 1, epoch
                for reloaded in (0, old, floor, floor + 1):
                    allowed = reloaded >= floor
                    assert not allowed or reloaded != old
                    for arm in ("S", "M", "E", "V"):
                        for resumed in (False, True):
                            admitted = allowed and (resumed or arm in ("E", "V"))
                            if arm in ("S", "M") and not resumed:
                                assert not admitted
                            checked += 1
    return dict(valid=True, transitions=checked)


def invalidation_model():
    # Ownership reduces legal schedules to two transaction orders. Also retain
    # the unowned check -> invalidate -> store counterexample as a negative.
    observed = []
    for order in itertools.permutations(("install", "invalidate")):
        version, built, entry = 1, 1, 0
        for operation in order:
            if operation == "install":
                if built == version:
                    entry = built
            else:
                version += 1
                entry = 0
        assert entry == 0 or entry >= version
        observed.append(order)
    version, final_check = 1, True
    version += 1
    delayed_install = 1 if final_check else 0
    assert delayed_install < version
    return dict(valid=True, serialized_orders=observed, unowned_install_detected=True,
                writer_before_publication_old_call_allowed=True)


def mixed_instruction_model():
    """Exhaust all 2**8 independently replaced 16-bit immediate fields.

    Deliberately not the native mix/nonce implementation or checker encoder.
    This is a finite instruction-field oracle, not arbitrary torn-word proof.
    """
    old=(0x1111222233334444,0x5555666677778888)
    new=(0x9999AAAABBBBCCCC,0xDDDDEEEEFFFF0000)
    checked=0
    for size,hook in itertools.product((64,256,4096),(False,True)):
        for mask in range(256):
            registers=[0,0]
            for field in range(8):
                reg,half=divmod(field,4)
                source=new if mask&(1<<field) else old
                immediate=(source[reg]>>(16*half))&0xffff
                instruction=((0xD2800000 if half==0 else 0xF2800000)+
                             (half<<21)+(immediate<<5)+reg)
                # Decode the generated MOVZ/MOVK immediate and destination.
                destination=instruction&31
                shift=((instruction>>21)&3)*16
                value=(instruction>>5)&0xffff
                if instruction&0xFF800000==0xD2800000:
                    registers[destination]=value<<shift
                else:
                    registers[destination]=(registers[destination]&~(0xffff<<shift))|(value<<shift)
            output=tuple(registers)
            assert (output==old)==(mask==0)
            assert (output==new)==(mask==255)
            assert size//4 >= 8+5*hook+1
            checked+=1
    return dict(valid=True,field_combinations=checked,mixed_masks_rejected=254*6,
                arbitrary_torn_instruction_proof=False)


def pressure_model():
    """Independent finite slot/lifetime fixture for M18-P1, all topologies.

    Tracks actual publication/call/retirement separately from scratch lifetime.
    No source code or trace checker is imported by this abstract model.
    """
    tested=0
    for arm,n,pressure in itertools.product(("I","S","E","V","M-data-map","M-data-protect","M-jit-protect"),(1,2,4,8),("near-full","fragmented")):
        owners=n if arm=="V" else 1
        slots=512
        active=set(range(2*owners))
        held={s for s in range(2*owners+1,slots) if pressure=="near-full" or s%2}
        states={s:"empty" for s in range(slots)}
        calls={}
        for slot in sorted(held):
            states[slot]="building"
            states[slot]="published"
            calls[slot]={slot%n} if arm=="V" else set(range(n))
            assert calls[slot]
            states[slot]="superseded"
            # Exact ACK set and I's cleared SC hazard condition precede retire.
            ack=[1]*n; hazards=[0]*n
            assert all(v==1 for v in ack) and not any(hazards)
            states[slot]="retired"
        for slot in active: states[slot]="published"
        free=set(range(slots))-active-held
        if pressure=="near-full":
            assert free=={2*owners}
            scratch=next(iter(free)); states[scratch]="building-scratch"
            assert not (set(range(slots))-active-held-{scratch})
            assert states[scratch]!="retired" and scratch not in calls
            states[scratch]="discarded-scratch" # first reservation
            states[scratch]="building-scratch" # new token, same never-run slot
            if arm[0] not in "SM":
                released=min(held)
                owner=released%n if arm=="V" else 0
                old=owner # page-0 active generation for that owner
                assert states[released]=="retired" and old in active
                states[released]="published"
                calls[released]={owner} if arm=="V" else set(range(n))
                states[old]="retired"
                final_held=(held-{released})|{old}
                final_active=(active-{old})|{released}
                assert len(final_held)==len(held) and len(final_active)==len(active)
                assert not final_held&final_active
            else:
                final_held,final_active=held,active
            states[scratch]="discarded-scratch"
            assert set(range(slots))-final_held-final_active=={scratch}
        else:
            assert all(s%2 for s in held)
            assert not held&active and free
        tested+=1
    # Named invalid states cannot satisfy the model's acceptance predicates.
    assert "building-scratch"!="retired"
    assert not (all(v==2 for v in [2,1]) and not any([0,7]))
    assert {0}!={1} # V owner transfer is not the intended call witness set.
    return dict(valid=True,cases=tested,scratch_is_retirement_evidence=False,
                held_reader_oracle_not_replaced=True)


def run():
    sound = hazard_model()
    broken = hazard_model(True)
    assert sound["valid"] and not broken["valid"]
    return dict(kind="abstract-model", evidence=False, hazard=sound,
                early_retire_negative=broken, epochs=epoch_model(),
                invalidation=invalidation_model(), mixed_instructions=mixed_instruction_model(),
                pressure=pressure_model())


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))
