"""The correctness family's rule (spec R2.13): libm32's accurate-mode variant runs only with AFP absent or hidden, where
accurate mode differs from fast mode (docs/floating-point.md); with AFP, fast mode is already exact."""


def applies(suite, case, variant, met):
    if suite == 'libm32' and variant.get('engine.fp') == 'accurate' and variant.get('host.afp') is not False:
        return 'accurate mode changes nothing on a host with AFP (docs/floating-point.md)'
    return None
