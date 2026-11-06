#!/usr/bin/env python3
"""Exercise retained success and failed-child cleanup without Docker or downloads."""
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix='natemu-setup-check-') as temp:
    root = Path(temp)
    docker = root / 'docker'
    docker.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args=sys.argv[1:]
if args[:2]==['image','inspect']:
    print(json.dumps([{'Architecture':'arm64','Os':'linux'}]))
elif args[0]=='start' and os.environ.get('FAIL_CHILD'):
    sys.exit(3)
elif args[0]=='inspect':
    print(json.dumps([{'State':{'Running':False,'ExitCode':0,'OOMKilled':False}}]))
elif args[0]=='commit':
    print('sha256:synthetic')
elif args[0]=='save':
    pathlib.Path(args[2]).write_bytes(b'synthetic-image')
''')
    docker.chmod(0o755)
    env = dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'], NATEMU_RAW_ROOT=str(root/'raw'))
    for name, fail in (('success', False), ('failure', True)):
        case_env = dict(env)
        if fail:
            case_env['FAIL_CHILD'] = '1'
        else:
            case_env.pop('FAIL_CHILD', None)
        result = subprocess.run([sys.executable, str(HERE/'setup.py'), '--name', name], env=case_env, capture_output=True, timeout=30)
        record = json.loads((root/'raw'/name/'manifest.json').read_text())
        assert (result.returncode != 0) == fail, result.stderr
        assert record['status'] == ('failed' if fail else 'toolchain_prepared_no_generator_built')
        assert record['commands'][-1]['argv'][1:4] == ['rm', '--force', '--volumes']
        assert (root/'raw'/name/'SHA256SUMS').exists()
        repeated = subprocess.run([sys.executable, str(HERE/'setup.py'), '--name', name], env=case_env, capture_output=True, timeout=30)
        assert repeated.returncode != 0
    print('PASS: success retention, failed-child cleanup, and overwrite refusal')

# Execute the actual setup admission expression, including rejection controls.
program = shlex.split(next(line for line in (HERE/'setup.sh').read_text().splitlines() if line.startswith('awk ')))[1]
for text, expected in (("'https://example.invalid/a' a.deb 100 SHA256:x\n", 0),
                       ("'https://example.invalid/a' a.deb 734003201 SHA256:x\n", 1),
                       ("'https://example.invalid/a' a.deb unknown SHA256:x\n", 1),
                       ('', 1)):
    result = subprocess.run(['awk', program], input=text, text=True, capture_output=True, timeout=5)
    assert result.returncode == expected, (text, result.stdout, result.stderr)
print('PASS: actual package-size gate accepts bounded input and rejects excess/malformed/empty input')
