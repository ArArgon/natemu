"""A small client for natemu's control socket (docs/control-api.md): JSON-RPC 2.0, one compact JSON value per line.

    vm = Control.connect(path)          # waits for natemu to make the socket
    vm.call('query_status')             # the result, or raises RpcError with the error's code
    vm.notify('quit')                   # no answer is ever sent
    vm.wait_stopped()                   # the `stopped` notification's reason, then the connection ends
    vm.snapshot()                       # (width, height, PNG bytes)
    fb, fd = vm.framebuffer()           # the mode and the framebuffer's descriptor (map it read-only)

Lines that are not the answer being waited for (notifications such as `stopped` and `display_changed`, a batch's
array) are kept in `notifications`. Every read is a `recvmsg` (socket.recv_fds), so a descriptor natemu passes with
an answer (`framebuffer`'s, as SCM_RIGHTS) is kept: the descriptors received while a call waits for its answer are
that answer's. Standard library only.
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import base64
import json
import os
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
    MAX_FDS = 16                        # descriptors taken per recvmsg (a batch of framebuffer calls has one each)

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buffer = b''
        self.fds = []                   # descriptors received and not yet given to an answer
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
        self.sock.close()
        for fd in self.fds: os.close(fd)
        self.fds = []

    def _send(self, message: dict):
        self.sock.sendall((json.dumps(message, separators=(',', ':')) + '\n').encode())

    def _line(self) -> bytes:
        """The next line natemu sent (without its newline), reading with recvmsg; raises Closed at the end."""
        while b'\n' not in self.buffer:
            data, fds, _, _ = socket.recv_fds(self.sock, 1 << 20, self.MAX_FDS)
            self.fds += fds
            if not data: raise Closed('natemu closed the control connection')
            self.buffer += data
        line, _, self.buffer = self.buffer.partition(b'\n')
        return line

    def read(self):
        """The next message natemu sent (a dict, or a list for a batch's answer); raises Closed at the end."""
        line = b''
        while not line.strip(): line = self._line()
        return json.loads(line)

    def take_fds(self) -> list:
        """The descriptors received so far and not yet taken (the caller closes them)."""
        fds, self.fds = self.fds, []
        return fds

    def call(self, method: str, params=None, timeout: float | None = None):
        """Sends a request and returns its result; raises RpcError for an error answer, Closed if none comes, and
        TimeoutError if `timeout` seconds pass first (the connection is then unusable: close it)."""
        self.next_id += 1
        request = {'jsonrpc': '2.0', 'id': self.next_id, 'method': method}
        if params is not None: request['params'] = params       # never "params": null (that is -32600)
        self.sock.settimeout(timeout)
        try:
            self._send(request)
            while True:
                message = self.read()
                if isinstance(message, dict) and 'id' in message and message['id'] == self.next_id:
                    if 'error' in message: raise RpcError(message['error'])
                    return message['result']
                self.notifications.append(message)
        except socket.timeout as e:
            raise TimeoutError(f'no answer to {method} within {timeout} s') from e
        finally:
            if self.sock.fileno() >= 0: self.sock.settimeout(None)

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

    def send_input(self, events: list, timeout: float | None = None):
        return self.call('send_input', {'events': events}, timeout)

    def snapshot(self, timeout: float | None = None) -> tuple:
        """The guest's picture: (width, height, the PNG file's bytes)."""
        r = self.call('snapshot', {'format': 'png'}, timeout)
        return r['width'], r['height'], base64.b64decode(r['png'], validate=True)

    def framebuffer(self, timeout: float | None = None) -> tuple:
        """The mode (width, height, stride, offset, format, size) and the framebuffer's descriptor, which the caller
        maps read-only (mmap.mmap(fd, size, prot=mmap.PROT_READ)) and closes. Descriptors left over from earlier
        answers are closed first, so the one returned came with this answer."""
        for fd in self.take_fds(): os.close(fd)
        r = self.call('framebuffer', None, timeout)
        fds = self.take_fds()
        if len(fds) != 1:
            for fd in fds: os.close(fd)
            raise RuntimeError(f'framebuffer: {len(fds)} descriptors came with the answer, expected 1')
        return r, fds[0]

    def display_changes(self) -> list:
        """The `display_changed` notifications' params received so far, in order."""
        return [m.get('params') for m in self.notifications if isinstance(m, dict) and m.get('method') == 'display_changed']


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


# Characters per send_input in type_text. natemu passes keys on as fast as the guest reads them, and a Linux evdev reader
# (a display server) keeps only 64 events, about 10 keys, so a long string in one send_input can lose keys there
# (docs/control-api.md, Flow control). tests/devices/run_control.py sends 6 keys per part for the same reason.
TYPE_TEXT_CHARS = 6


def type_text(vm: Control, text: str, chars: int = TYPE_TEXT_CHARS, pause: float = 0.05):
    """Types `text` on a US layout, `chars` characters per send_input with `pause` seconds between parts, so a guest
    program reading evdev gets to read each part before the next arrives."""
    for at in range(0, len(text), chars):
        if at: time.sleep(pause)
        vm.send_input(text_events(text[at:at + chars]))
