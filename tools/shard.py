"""`--shard I/N` for runners whose cases are independent: shard I of N (1-based) takes every Nth item. The union of the N
shards is the unsplit run (the suite's sharded steps rely on it). One-off checks: probes runs them on the last shard, k1 runs its step-count checks on shard 1."""
def parse(s):
    i, n = map(int, s.split('/'))
    assert 1 <= i <= n, f'--shard {s}: want I/N with 1 <= I <= N'
    return i, n


def add(ap):
    ap.add_argument('--shard', type=parse, default=(1, 1), metavar='I/N', help='run only shard I of N of the independent cases (1-based)')


def take(items, spec): return items[spec[0] - 1::spec[1]]
def last(spec): return spec[0] == spec[1]      # the shard that also runs the one-off checks
