import sys, math, time, _thread
print('maxsize', sys.maxsize, sys.byteorder, sys.version_info[:2])
print(2**200 % 1000007, pow(3, 10**5, 2**61 - 1), math.factorial(30))
print(hash(2**62 + 12345), int(1e18), (1 << 63) - 1, -(1 << 63) // 7, divmod(-(10**19), 12345))
print(repr(0.1 + 0.2), repr(1 / 3), 1e16 + 2, float('2.5'), 7.0 // 2, 2.0**10, 1e308 * 10)
print(round(sum(i ** 0.5 for i in range(1, 2000)), 6), round(math.fsum(1 / i for i in range(1, 5000)), 9))
print('%.12g %.12g %.12g' % (math.sin(1), math.exp(1), math.log(10)), math.pi, math.floor(-2.5), int(-2.5), round(2.5), round(3.5))
print(sorted({3: 'c', 1: 'a', 2: 'b'}.items()), 'abc'.upper() * 2, '%5.2f|%-6s|%04d' % (3.14159, 'ab', 42), [x * x for x in range(8)][::-2])
big = 12345678901234567890123456789
print(big * big, big // 987654321, big.bit_length(), float(big), int(float(2**70)))
print(bytes(range(5)).hex(), int.from_bytes(b'\xff' * 8, 'little'), (2**64 - 1).to_bytes(8, 'big').hex())
t = time.time_ns()
print('time_ns', abs(t - time.time() * 1e9) < 2e9, t > 2**53, time.monotonic() >= 0)
lock = _thread.allocate_lock(); done = _thread.allocate_lock(); done.acquire(); count = [0, 0]
def work():
    for _ in range(5000):
        with lock: count[0] += 1
    with lock:
        count[1] += 1
        if count[1] == 4: done.release()
for _ in range(4): _thread.start_new_thread(work, ())
print('threads', done.acquire(timeout=120), count)
held = _thread.allocate_lock(); held.acquire(); t0 = time.monotonic()
print('timeout', held.acquire(timeout=0.05), 0.04 < time.monotonic() - t0 < 5)
