# The desktop profile's keyboard where nothing outside the guest types (the KVM reference, de.input=uinput;
# tests/bench/desktop/driver.sh): a virtual keyboard made through /dev/uinput, which udev and KWin's libinput take as
# any other, typing the lines written to FIFO: "key CODE" (a tap; codes as natemu's control socket names them:
# MetaLeft, Enter, KeyA ...) and "text STRING" (lowercase letters, digits, space). Under natemu the runner types the
# same lines on natemu's PS/2 keyboard instead.
import fcntl, os, struct, sys, time

EV_SYN, EV_KEY = 0, 1
UI_SET_EVBIT, UI_SET_KEYBIT, UI_DEV_SETUP, UI_DEV_CREATE = 0x40045564, 0x40045565, 0x405C5503, 0x5501
LETTERS = dict(zip('qwertyuiop', range(16, 26))) | dict(zip('asdfghjkl', range(30, 39))) | dict(zip('zxcvbnm', range(44, 51)))
CODES = {'MetaLeft': 125, 'Enter': 28, 'Escape': 1, 'Space': 57, 'Backspace': 14, 'Tab': 15, 'AltLeft': 56,
         **{f'Key{c.upper()}': k for c, k in LETTERS.items()}, **{f'Digit{d}': 2 + (int(d) - 1) % 10 for d in '1234567890'}}


def emit(fd, typ, code, value):
    os.write(fd, struct.pack('llHHi', 0, 0, typ, code, value))


def tap(fd, code):
    for v in (1, 0):
        emit(fd, EV_KEY, code, v); emit(fd, EV_SYN, 0, 0); time.sleep(0.02)


def main(fifo):
    fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    for c in set(CODES.values()): fcntl.ioctl(fd, UI_SET_KEYBIT, c)
    fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack('HHHH80sI', 0x06, 0x1234, 0x5678, 1, b'desktop-profile keyboard', 0))
    fcntl.ioctl(fd, UI_DEV_CREATE)
    print('DE-UINPUT ready', flush=True)
    while True:
        with open(fifo) as f:
            for line in f:
                w = line.split()
                if not w: continue
                if w[0] == 'key' and len(w) > 1 and w[1] in CODES: tap(fd, CODES[w[1]])
                elif w[0] == 'text':
                    for ch in ' '.join(w[1:]):
                        name = 'Space' if ch == ' ' else f'Digit{ch}' if ch.isdigit() else f'Key{ch.upper()}'
                        if name in CODES: tap(fd, CODES[name])


if __name__ == '__main__':
    main(sys.argv[1])
