from testkit.harness import CaseResult


class Echo:
    def cases(self, fam, suite): return ['c0', 'c1', 'c2']
    def requirements(self, fam, suite, variant): return []
    def run(self, batch, config): return {i.nodeid: CaseResult('passed') for i in batch}
