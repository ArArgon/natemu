def applies(suite, case, variant, met):
    if case == 'c1' and variant.get('engine.fp') == 'fast' and variant.get('host.afp') is False:
        return 'nan-results'
    return None
