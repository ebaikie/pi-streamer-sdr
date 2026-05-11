#!/usr/bin/env python3
"""
Sit between rtl_fm (squelch on) and sox/ffmpeg.
When rtl_fm stops writing (squelch closed), inject low-level white noise
so the Icecast stream stays alive and listeners hear noise, not silence.

A background thread reads stdin continuously so we never block on the
pipe. The main loop outputs at a fixed clock rate (chunk_ms per tick),
picking real data from the buffer when available, otherwise noise.

Hold time: once rtl_fm starts writing, keep draining real data for
hold_s seconds after the last byte before switching to noise injection.
This prevents rapid squelch flutter from causing clicks mid-transmission.

Usage:
  rtl_fm -l N ... | python3 silence_inject.py [rate [amplitude [hold_s]]] | sox ...
"""
import sys, time, struct, random, threading, collections

rate      = int(sys.argv[1])   if len(sys.argv) > 1 else 22050
amplitude = float(sys.argv[2]) if len(sys.argv) > 2 else 0.03
hold_s    = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5

chunk_ms      = 20
chunk_samples = rate * chunk_ms // 1000
chunk_bytes   = chunk_samples * 2  # 16-bit mono

# Pre-generate 5s of white noise (wrapped / cycled)
noise_len = rate * 5
max_amp   = int(amplitude * 32767)
noise_data = struct.pack(f'{noise_len}h',
    *[max(-32767, min(32767, int(random.gauss(0, max_amp)))) for _ in range(noise_len)])

buf      = collections.deque()
buf_lock = threading.Lock()
eof      = threading.Event()

def reader():
    stdin = sys.stdin.buffer
    while True:
        data = stdin.read1(chunk_bytes * 4)
        if not data:
            eof.set()
            break
        with buf_lock:
            buf.append(data)
            # Cap buffer at ~1s to avoid unbounded growth
            while len(buf) > 50:
                buf.popleft()

threading.Thread(target=reader, daemon=True).start()

stdout    = sys.stdout.buffer
noise_pos = 0
last_data = 0.0
interval  = chunk_ms / 1000.0
next_t    = time.monotonic()

while not eof.is_set():
    wait = next_t - time.monotonic()
    if wait > 0:
        time.sleep(wait)

    chunk = None
    now = time.monotonic()

    with buf_lock:
        if buf:
            raw = bytearray()
            while buf and len(raw) < chunk_bytes:
                raw.extend(buf.popleft())
            chunk = bytes(raw[:chunk_bytes])
            if len(raw) > chunk_bytes:
                buf.appendleft(raw[chunk_bytes:])
            last_data = now

    if chunk is None and (now - last_data) < hold_s:
        # In hold window after last real data — write zeros to preserve timing
        chunk = bytes(chunk_bytes)

    if chunk is None:
        # Squelch has been closed long enough — inject white noise
        end = noise_pos + chunk_bytes
        if end <= len(noise_data):
            chunk = noise_data[noise_pos:end]
        else:
            chunk = noise_data[noise_pos:] + noise_data[:end - len(noise_data)]
        noise_pos = end % len(noise_data)

    stdout.write(chunk)
    stdout.flush()
    next_t += interval
