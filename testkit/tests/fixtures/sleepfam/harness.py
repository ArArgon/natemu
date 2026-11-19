import json, os, pathlib, signal, time

from testkit.harness import CaseResult, Requirement
from testkit.resources import Unpreparable


class Sleep:
    def cases(self, fam, suite): return [f'c{i}' for i in range(suite.params['n'])]

    def requirements(self, fam, suite, variant):
        return [Requirement('resource', suite.params['resource'])] if 'resource' in suite.params else []

    def resolve(self, req):
        raise Unpreparable(req.name, 'http://127.0.0.1:9/x: connection refused')

    def run(self, batch, config):
        out = {}
        for it in batch:
            if it.case == it.suite.params.get('die'): os.kill(os.getpid(), signal.SIGKILL)
            if it.suite.params.get('vm'):          # a stand-in for natemu: its own session, stopped by campaign's handler
                import subprocess
                from testkit.harness import campaign
                p = subprocess.Popen(['sleep', '300'], start_new_session=True)
                campaign._LIVE.add(p); campaign._on_term(); campaign._record_group(p, 'sleep')
                pathlib.Path(os.environ['HOLD_PID']).write_text(str(p.pid))
            t0 = time.time(); time.sleep(it.suite.params['seconds']); t1 = time.time()
            if log := os.environ.get('SLEEP_LOG'):
                with open(log, 'a') as f:
                    f.write(json.dumps([t0, t1, it.suite.needs.exclusive, it.suite.needs.cores, os.environ.get('RUN_TAG')]) + '\n')
            out[it.nodeid] = CaseResult('passed')
        return out
