// The CF table's check (tools/fexc/test_cf_table.py): FexcCfTable (shim/fexc_hook.h), the vector that holds the dispatcher's
// CF inversion per guest offset, answers every lookup as the std::map it replaced would: a value set for a key is the last
// one set for it, a key never set answers true. Random rounds of sets in increasing order (the dispatcher's), in any order
// and with many repeated keys, each with lookups between the sets (as a retried attempt makes) and at the end. Prints the
// number of lookups compared and exits 0, or names the first difference and exits 1.
#include <fexc_hook.h>
#include <cstdio>
#include <map>
#include <random>

int main() {
  std::mt19937_64 rng(1);
  long checks = 0;
  const auto same = [&](FexcCfTable& t, const std::map<uint64_t, bool>& m, uint64_t q) {
    const auto it = m.find(q);
    ++checks;
    return t.get(q) == (it == m.end() ? true : it->second);
  };
  for (int round = 0; round < 20000; ++round) {
    FexcCfTable t;
    std::map<uint64_t, bool> m;
    const int n = int(rng() % 40), mode = int(rng() % 3);   // increasing keys, random ones, many repeated ones
    uint64_t k = 0;
    for (int i = 0; i < n; ++i) {
      k = mode == 0 ? k + 1 + rng() % 8 : rng() % (mode == 1 ? 64 : 16);
      const bool v = rng() & 1;
      t[k] = v;
      m[k] = v;
      if (rng() % 7 == 0 && !same(t, m, rng() % 70)) {
        std::printf("difference between sets: round %d mode %d\n", round, mode);
        return 1;
      }
    }
    for (int j = 0; j < 80; ++j) {
      const uint64_t q = j < 70 ? uint64_t(j) : rng() % 70;   // every key in order, then random ones
      if (!same(t, m, q)) {
        std::printf("difference: round %d mode %d key %llu\n", round, mode, (unsigned long long)q);
        return 1;
      }
    }
    t.clear();   // a new request's table answers true everywhere
    if (!t.get(k)) {
      std::printf("difference after clear: round %d\n", round);
      return 1;
    }
  }
  std::printf("ok %ld lookups\n", checks);
}
