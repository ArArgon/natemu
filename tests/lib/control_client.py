"""A small client for natemu's control socket (docs/control-api.md): JSON-RPC 2.0, one compact JSON value per line.

    vm = Control.connect(path)          # waits for natemu to make the socket
    vm.call('query_status')             # the result, or raises RpcError with the error's code
    vm.notify('quit')                   # no answer is ever sent
    vm.wait_stopped()                   # the `stopped` notification's reason, then the connection ends

Lines that are not the answer being waited for (notifications, a batch's array) are kept in `notifications`; `stopped`
is the only notification natemu sends today. Standard library only.
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import json
import socket
import time

PROTOCOL = 1


class RpcError(Exception):
    """An error response: `code` (for example -32001), `message` and `data` as natemu sent them."""
    def __init__(self, error: dict):
        super().__init__(f'{error.get("code")} {error.get("message")} {error.get("data", "")}'.strip())
        self.code, self.message, self.data = error.get('code'), error.get('message'), error.get('data')


class Closed(ConnectionError):
    """natemu closed the connection (the run ended) before the answer came."""


class Control:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.lines = sock.makefile('r', encoding='utf-8', newline='\n')
        self.next_id = 0
        self.notifications = []         # every message that was not an awaited answer, in arrival order

    @classmethod
    def connect(cls, path, timeout: float = 60.0, alive=lambda: True) -> 'Control':
        """Connects to the socket at `path`, retrying while it is missing or not yet listening (natemu makes it just
        before the guest starts) for at most `timeout` seconds, or until `alive()` is false (natemu exited)."""
        end = time.monotonic() + timeout
        while True:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(str(path))
                return cls(sock)
            except (FileNotFoundError, ConnectionRefusedError):
                sock.close()
                if time.monotonic() >= end or not alive(): raise
                time.sleep(0.05)

    def close(self):
        """Disconnects: natemu lets go of every key and button this client held."""
        try: self.sock.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        self.lines.close()
        self.sock.close()

    def _send(self, message: dict):
        self.sock.sendall((json.dumps(message, separators=(',', ':')) + '\n').encode())

    def read(self):
        """The next message natemu sent (a dict, or a list for a batch's answer); raises Closed at the end."""
        line = ''
        while not line.strip():
            line = self.lines.readline()
            if not line: raise Closed('natemu closed the control connection')
        return json.loads(line)

    def call(self, method: str, params=None):
        """Sends a request and returns its result; raises RpcError for an error answer, Closed if none comes."""
        self.next_id += 1
        request = {'jsonrpc': '2.0', 'id': self.next_id, 'method': method}
        if params is not None: request['params'] = params       # never "params": null (that is -32600)
        self._send(request)
        while True:
            message = self.read()
            if isinstance(message, dict) and 'id' in message and message['id'] == self.next_id:
                if 'error' in message: raise RpcError(message['error'])
                return message['result']
            self.notifications.append(message)

    def notify(self, method: str, params=None):
        """Sends a notification: carried out, never answered."""
        request = {'jsonrpc': '2.0', 'method': method}
        if params is not None: request['params'] = params
        self._send(request)

    def stopped(self):
        """The reason of a `stopped` notification already received, or None."""
        for m in self.notifications:
            if isinstance(m, dict) and m.get('method') == 'stopped': return (m.get('params') or {}).get('reason')
        return None

    def wait_stopped(self, timeout: float = 60.0):
        """Reads until the `stopped` notification and the end of the connection; its reason, or None if the
        connection ended (or `timeout` passed) without one."""
        self.sock.settimeout(timeout)
        try:
            while True: self.notifications.append(self.read())
        except (Closed, OSError):           # OSError: timeout, or reset
            pass
        finally:
            self.sock.settimeout(None)
        return self.stopped()

    def send_input(self, events: list):
        return self.call('send_input', {'events': events})


def key(code: str, pressed: bool) -> dict:
    return {'type': 'key', 'code': code, 'pressed': pressed}


def tap(code: str) -> list:
    """A key's press and release."""
    return [key(code, True), key(code, False)]


# type_text: US layout, characters to key codes (docs/control-api.md: a client maps characters to codes).
_PLAIN = {' ': 'Space', '\n': 'Enter', '\t': 'Tab', '-': 'Minus', '=': 'Equal', '[': 'BracketLeft', ']': 'BracketRight',
          '\\': 'Backslash', ';': 'Semicolon', "'": 'Quote', '`': 'Backquote', ',': 'Comma', '.': 'Period', '/': 'Slash'}
_SHIFTED = {'_': 'Minus', '+': 'Equal', '{': 'BracketLeft', '}': 'BracketRight', '|': 'Backslash', ':': 'Semicolon',
            '"': 'Quote', '~': 'Backquote', '<': 'Comma', '>': 'Period', '?': 'Slash', '!': 'Digit1', '@': 'Digit2',
            '#': 'Digit3', '$': 'Digit4', '%': 'Digit5', '^': 'Digit6', '&': 'Digit7', '*': 'Digit8', '(': 'Digit9',
            ')': 'Digit0'}


def text_events(text: str) -> list:
    """The key events that type `text` on a US layout (ShiftLeft around shifted characters)."""
    events = []
    for ch in text:
        if 'a' <= ch <= 'z': events += tap('Key' + ch.upper())
        elif 'A' <= ch <= 'Z': events += [key('ShiftLeft', True), *tap('Key' + ch), key('ShiftLeft', False)]
        elif '0' <= ch <= '9': events += tap('Digit' + ch)
        elif ch in _PLAIN: events += tap(_PLAIN[ch])
        elif ch in _SHIFTED: events += [key('ShiftLeft', True), *tap(_SHIFTED[ch]), key('ShiftLeft', False)]
        else: raise ValueError(f'{ch!r} has no key on a US layout')
    return events


def type_text(vm: Control, text: str):
    return vm.send_input(text_events(text))
