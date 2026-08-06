#!/usr/bin/env python3
"""On/off fan controller for pi-sdr-gisborne (Pi 3B) — intake, exhaust, heatsink.

Drives each MOSFET module's SIG pin as a plain digital GPIO output,
independent of USB port power — does not touch the SDR dongle's USB port.
No PWM: speed control isn't needed, so this just holds each pin HIGH or LOW.

Runs as a PERSISTENT process, not a one-shot script. Raspberry Pi OS's
modern GPIO character-device model (used by gpiozero/lgpio here) releases
a line — floating it — when the requesting process exits; unlike the
earlier PWM design (where the kernel's pwmchip held the signal after the
script exited), a plain digital output only stays driven for as long as
this process keeps the line open. So the systemd unit runs this as
Type=simple and it blocks forever once set, releasing cleanly on SIGTERM.

GPIO pins (2026-08-05 rewire, corrected twice same day — the first pass
used BCM11/BCM15 from a physical-pin/BCM mixup; the second pass initially
kept exhaust on BCM13 before Eli confirmed all three are being grouped
together physically, moving exhaust too): BCM17 (intake), BCM27 (exhaust —
moved off BCM13; the physical wiring for this one was previously done and
working, but is being relocated along with the other two), BCM22
(heatsink, new). GPIO17, GPIO22, and GPIO27 are all general-purpose with
no default alternate peripheral function — confirmed via config.txt
(nothing references any of the three) and live `gpioinfo` (all showed as
unclaimed plain inputs before reassigning).

None of the three pins have their physical wiring done yet as of this
software update — rewiring is a few days out — so all three default to
**disabled** until Eli confirms each is actually connected. This is
different from the previous rewire, where intake/exhaust were already
wired and defaulted on; now nothing should be assumed live.

Usage:
    fan_control.py                        # daemon mode: apply config, hold, block
    fan_control.py --fan intake --off     # manual override, also holds until Ctrl-C/killed
    fan_control.py --fan all --on
"""

import argparse
import os
import signal
import sys

from gpiozero import DigitalOutputDevice

FANS = {
    "intake": {
        "gpio": int(os.environ.get("FAN_INTAKE_GPIO", "17")),
        "default_on": os.environ.get("FAN_INTAKE_ENABLED", "0") == "1",
    },
    "exhaust": {
        "gpio": int(os.environ.get("FAN_EXHAUST_GPIO", "27")),
        "default_on": os.environ.get("FAN_EXHAUST_ENABLED", "0") == "1",
    },
    "heatsink": {
        "gpio": int(os.environ.get("FAN_HEATSINK_GPIO", "22")),
        "default_on": os.environ.get("FAN_HEATSINK_ENABLED", "0") == "1",
    },
}

devices = {}


def apply(name, gpio, turn_on):
    dev = DigitalOutputDevice(gpio, initial_value=turn_on)
    devices[name] = dev
    print(f"[FAN] {name}: GPIO{gpio} {'ON' if turn_on else 'OFF'}", flush=True)


def shutdown(signum, frame):
    for name, dev in devices.items():
        dev.off()
        dev.close()
        print(f"[FAN] {name}: OFF (shutdown)", flush=True)
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fan", choices=[*FANS.keys(), "all"], default="all", help="which fan to control (default: all)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--on", action="store_true", help="turn on")
    group.add_argument("--off", action="store_true", help="turn off")
    args = parser.parse_args()

    explicit = True if args.on else (False if args.off else None)
    targets = FANS.keys() if args.fan == "all" else [args.fan]

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    for name in targets:
        turn_on = explicit if explicit is not None else FANS[name]["default_on"]
        apply(name, FANS[name]["gpio"], turn_on)

    signal.pause()  # hold the lines open — see module docstring


if __name__ == "__main__":
    main()
