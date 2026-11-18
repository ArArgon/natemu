import hashlib
import http.server
import threading

import pytest

from testkit import resources

BODY = b'resource body\n' * 100
PIN = hashlib.sha256(BODY).hexdigest()


@pytest.fixture(autouse=True)
def cache_root(monkeypatch, tmp_path):
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))


@pytest.fixture
def server():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/good':
                self.send_response(200); self.end_headers(); self.wfile.write(BODY)
            else:
                self.send_response(404); self.end_headers()
        def log_message(self, *a): pass
    s = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{s.server_address[1]}'
    s.shutdown()


def decl(urls, sha=PIN):
    return {'urls': urls, 'sha256': sha}


def test_fetch_verifies_pin(server):
    p = resources.fetch('r', decl([server + '/good']))
    assert p.read_bytes() == BODY
    with pytest.raises(resources.Unpreparable) as e:
        resources.fetch('bad', decl([server + '/good'], sha='0' * 64))
    assert PIN in str(e.value) and '0' * 64 in str(e.value)


def test_fetch_falls_back_to_second_mirror(server):
    assert resources.fetch('r2', decl([server + '/missing', server + '/good'])).read_bytes() == BODY


def test_fetch_no_network():
    with pytest.raises(resources.Unpreparable) as e:
        resources.fetch('r3', decl(['http://127.0.0.1:9/x']))
    assert 'http://127.0.0.1:9/x' in str(e.value)


def recipe_a(out, inputs):
    (out / 'x').write_text('a')


def recipe_b(out, inputs):
    (out / 'x').write_text('b')


def test_derived_rebuilds_when_recipe_changes(tmp_path):
    src = tmp_path / 'in.txt'; src.write_text('i')
    a = resources.derived('d', recipe_a, [src])
    b = resources.derived('d', recipe_b, [src])
    assert a != b and (a / 'x').read_text() == 'a' and (b / 'x').read_text() == 'b'
    assert resources.derived('d', recipe_a, [src]) == a


def test_declarations_file_parses():
    for name, d in resources.declarations().items():
        assert d['urls'] and len(d['sha256']) == 64, name
