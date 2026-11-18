from testkit import host


def test_parse_features():
    assert host.parse_sysctl('hw.optional.arm.FEAT_AFP: 0\nhw.optional.arm.FEAT_SB: 1\nhw.optional.arm.caps: 5\n') == \
        {'afp': False, 'sb': True}


def test_detect_real_machine():
    h = host.detect()
    assert h.p_cores >= 1 and h.e_cores >= 0
    assert len(h.machine_id) == 36
    assert 'afp' in h.features
    assert h.os_build and h.chip.startswith('Apple')
