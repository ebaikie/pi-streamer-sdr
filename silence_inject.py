#!/usr/bin/env python3
"""
Sit between rtl_fm and sox/ffmpeg.
When rtl_fm squelch closes (no bytes out), inject silence so the downstream
pipe stays fed and the Icecast mount never drops.
Usage: rtl_fm ... | python3 silence_inject.py <sample_rate> | sox ...
"""
import sys
import select

rate = int(sys.argv[1]) if len(sys.argv) > 1 else 22050
chunk_size = 2 * rate * 50 // 1000  # 50 ms of 16-bit mono silence
silence = bytes(chunk_size)
timeout = 0.05

stdin = sys.stdin.buffer
stdout = sys.stdout.buffer

while True:
    ready, _, _ = select.select([stdin], [], [], timeout)
    if ready:
        data = stdin.read1(chunk_size)
        if not data:
            break
        stdout.write(data)
    else:
        stdout.write(silence)
    stdout.flush()
