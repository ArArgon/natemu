import http.server
import threading

import pytest

from testkit import cache, inputs, resources


@pytest.fixture
def server():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'not the pinned file\n')
        def log_message(self, *a): pass
    s = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{s.server_address[1]}'
    s.shutdown()


def test_wrong_pin_is_unpreparable(server, monkeypatch, tmp_path):
    """A download whose bytes do not match its pin stops the root with Unpreparable, and nothing is published."""
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    real = resources.declarations()
    monkeypatch.setattr(resources, 'declarations', lambda: {n: dict(d, urls=[f'{server}/{n}']) for n, d in real.items()})
    with pytest.raises(resources.Unpreparable) as e:
        inputs.root()
    assert 'pinned' in str(e.value)
    assert not (tmp_path / 'c' / 'input-root').exists()


def test_every_component_is_declared():
    names = set(resources.declarations())
    assert {'debian-netboot-linux', 'debian-netboot-initrd', 'busybox-static-deb', 'debian-kernel-deb', 'qemu-user-static'} <= names


@pytest.mark.slow
def test_root_builds_once_and_holds_exactly_the_inputs():
    r = inputs.root()
    with cache.peek():          # the second build finds every component and the root itself: anything rebuilt raises Miss
        assert inputs.root() == r
    for rel in inputs.PATHS:
        assert (r / rel).exists(), rel
    stray = [p for p in r.rglob('*') if p.is_file() and not any(p.relative_to(r).as_posix().startswith(rel) for rel in inputs.PATHS)]
    assert not stray
    assert not list(r.rglob('expected.json'))           # answers come from the locks, never from the input root
