# pi-fan — intake/exhaust/heatsink cooling fans for pi-sdr-gisborne

Standalone from the SDR pipeline in `../app.py`. That app power-cycles the
*USB* port the RTL-SDR dongle sits on (`uhubctl`) when the dongle needs a
reset — since the Pi 3B only exposes one switchable USB power group, toggling
it would also kill the dongle. The fans are instead driven from GPIO pins,
so they can run continuously with zero interaction with the SDR USB power
logic.

## Hardware

- MOSFET switching modules, labelled SIG / VCC / GND (generic low-side
  logic-level modules — SIG is the gate control input) — one per fan
- 2-wire DC fans, 5V, rated 0.2A each: intake, exhaust, and (new,
  2026-08-05) a heatsink fan. All three are grouped together physically
  on the board per Eli's design — as of 2026-08-05 **none are physically
  wired to their configured pins**, including exhaust, whose earlier
  working wiring on GPIO13 is being relocated alongside the other two
  rather than left in place. Rewiring is a few days out; see GPIO pins
  section below.
- All fans share the Pi's own 5V PSU as their power source. **Important:**
  the fans' actual power draw does **not** route through the Pi board
  itself — only each MOSFET module's VCC/GND/SIG (control side) connects
  to the Pi. The fan supply side taps the PSU output directly. This rules
  out "current through the Pi's own PCB traces sagging under CPU/USB load"
  as a mechanism, but does **not** rule out the shared PSU itself sagging
  or having poor transient response under combined load (Pi + SDR dongle
  re-inits + fan startup inrush, all still pulling from one supply) — see
  Troubleshooting history below.

## Wiring

None of the three below are physically wired yet (2026-08-05) — this is
the target wiring for Eli's upcoming rewiring session, deployed in
software ahead of time so it's just a wiring job when he gets to it.

```
Intake fan:
  Pi 5V   (physical pin 2 or 4)  ──────────────►  Intake module VCC
  Pi GND  (physical pin 6, etc.) ──────────────►  Intake module GND
  Pi GPIO17 (physical pin 11)    ──────────────►  Intake module SIG

Exhaust fan (previously working on GPIO13/physical pin 33 — being
relocated here, not left in place, since all three are grouped together):
  Pi 5V   (physical pin 2 or 4)  ──────────────►  Exhaust module VCC
  Pi GND  (physical pin 6, etc.) ──────────────►  Exhaust module GND
  Pi GPIO27 (physical pin 13)    ──────────────►  Exhaust module SIG

Heatsink fan:
  Pi 5V   (physical pin 2 or 4)  ──────────────►  Heatsink module VCC
  Pi GND  (physical pin 6, etc.) ──────────────►  Heatsink module GND
  Pi GPIO22 (physical pin 15)    ──────────────►  Heatsink module SIG

Each fan (power side, NOT through the Pi):
  PSU (+)         ─────────────────────────────► Fan (+)
  Fan (−)         ─────────────────────────────► Module OUT/drain
  Module GND ───────────────────────────────────► PSU (−)  [common ground]
```

Each MOSFET is low-side: it switches its fan's return path to ground, not
the positive rail. Each fan's supply ground and the Pi's GND **must** be
tied together (through the module) — the GPIO signal is only meaningful
relative to a shared ground reference.

## GPIO pins: BCM 17 (intake) / BCM 27 (exhaust) / BCM 22 (heatsink)

**2026-08-05 rewire** (intake moved off BCM18, exhaust moved off BCM13,
heatsink added) — ahead of a physical rewiring Eli's doing in the next
few days, grouping all three fans together on the board; the software
side was finished and deployed first so it's just a wiring job when he
gets to it. Since these are plain digital on/off outputs (no PWM), any
GPIO works functionally. **All three default to disabled** until Eli
confirms each is actually wired — including exhaust, whose previous
working connection on GPIO13 is being relocated, not left in place.

**Went through two corrections the same day, worth recording precisely:**
1. First pass used BCM11 (intake) and BCM15 (heatsink), from a
   physical-pin/BCM-number mixup on Eli's end.
2. Second pass corrected those to BCM17/BCM22, but initially kept exhaust
   on its existing BCM13 — Eli then clarified all three fans are being
   grouped together physically, so exhaust needed to move too, landing on
   the final BCM17/BCM27/BCM22 above.

BCM11 and BCM15 (the first-pass pins) each doubled as an alternate
peripheral function (SPI0_SCLK, UART0_RXD) and were checked carefully
before use. BCM17, BCM22, and BCM27 are all general-purpose with **no**
default alternate function, so there was less to check — but each was
checked anyway rather than assumed clean just because earlier rounds
found nothing: nothing in `/boot/firmware/config.txt` references any of
the three, and live `gpioinfo` showed each as a plain unclaimed `input`
before reassigning it. Same tool used throughout the troubleshooting
history below — live kernel line-consumer state is the most authoritative
source, more so than static config inspection alone. After the exhaust
move, `gpioinfo` also confirmed GPIO13 properly released back to a plain
unclaimed input.

## Design history: PWM → plain on/off

**v1 (this deployment) uses plain digital GPIO on/off, not PWM.** Speed
control turned out not to be needed, so it was dropped in favour of the
simplest thing that works: `gpiozero.DigitalOutputDevice`, steady HIGH/LOW,
no switching frequency to reason about at all.

This was actually the second design used here — worth recording briefly in
case PWM ever needs to come back:

1. **First attempt: `pigpio`.** Doesn't work on this OS — `pigpiod` isn't
   packaged for Raspberry Pi OS on Debian trixie, only the client library
   is (confirmed via `apt-cache policy` on the live Pi).
2. **Second attempt: kernel sysfs PWM**, via `dtoverlay=pwm-2chan` in
   `/boot/firmware/config.txt` and `/sys/class/pwm/pwmchipN/pwmM/`. This
   worked correctly at the electrical level (verified repeatedly: correct
   frequency/duty cycle held steady, confirmed via direct sysfs readback,
   completely unaffected by everything that turned out to actually be
   wrong — see Troubleshooting history). It required disabling the Pi's
   onboard analog audio (`dtparam=audio=off`) since `snd_bcm2835` claims
   the same PWM hardware block.
3. **Current: plain digital GPIO**, once PWM was confirmed to have never
   been the problem and speed control wasn't actually a requirement. The
   `pwm-2chan` overlay was removed from config.txt (needs a reboot to
   release GPIO18/19 back from PWM ALT-function to plain GPIO). `audio=off`
   was left in place — this Pi doesn't need the onboard jack, and leaving
   it off avoids `snd_bcm2835` ever contending for GPIO18 again in future.

**Note on process lifetime:** the switch to `gpiozero`'s GPIO
character-device backend (`lgpio`) changes how the systemd service has to
be shaped. The sysfs PWM approach could `Type=oneshot` — the kernel's
`pwmchip` held the signal after the script exited. A plain GPIO chardev
line is released (and floats) when its requesting process exits, so
`fan_control.py` now runs as a persistent `Type=simple` process that holds
the lines open and blocks, rather than a fire-and-forget script.

## Troubleshooting history (2026-08-05)

Both fans went through several rounds of "control signal confirmed present
(MOSFET LED lit / correct PWM proven via sysfs readback), fan not
spinning or stopping intermittently" — across a MOSFET board swap on
intake and multiple reboots. Ruled out along the way, all confirmed live
on the deployed Pi:

- Onboard analog audio contending for GPIO18/19 — ruled out (module loaded
  but never bound/probed once `audio=off` was set; no ALSA card, no
  consumer shown in `gpioinfo`)
- Pi-level under-voltage/throttling — ruled out (`vcgencmd get_throttled`
  consistently `0x0`, no event ever recorded since boot)
- PWM signal itself (frequency, duty cycle, service/config errors) — ruled
  out repeatedly; readback from `/sys/class/pwm/` always matched exactly
  what was commanded, including through a period where the fans had
  stopped entirely with the signal provably unchanged
- Systemd service misbehaving / not applying config on boot — ruled out;
  logs consistently clean across multiple boots

**Also ruled out (after switching PWM → plain on/off):** duty-cycle level
as a variable at all. Even at a flat, unwavering 100% digital HIGH (no PWM
switching whatsoever), the fans were reported jittery, noisy, and slow to
start — worse than a level that had run cleanly before. That happening
with no PWM in the loop rules out anything duty-cycle- or
switching-frequency-related definitively.

**Leading theory: shared PSU, not the Pi's own rail.** `pi-streamer`'s SDR
pipeline was observed auto-restarting every ~30 seconds continuously
("Icecast mount missing for 20s"), each restart killing and relaunching
4-5 processes including a fresh RTL-SDR USB re-init. Both fans share the
Pi's own PSU as their power source — but critically, their power draw does
**not** route through the Pi board itself, only each MOSFET's control side
(VCC/GND/SIG) does (see Hardware section above). So the mechanism isn't IR
drop through the Pi's own traces — it's the shared PSU itself potentially
sagging or having poor transient response under combined load (Pi CPU +
SDR dongle re-init + fan startup inrush, all still pulling from one
supply). A Tuya smart plug on the Pi's mains input (HA entity
`sensor.ev_charger_power`, confirmed exclusively measuring this Pi) showed
~4.6W with the SDR connected vs. ~3.4W without, consistent with
expectations — but only 3-4 data points across 2 hours of HA history,
nowhere near fine-grained enough to catch or rule out a brief spike during
a single ~30s restart cycle. Doesn't confirm the theory, but doesn't
contradict it either.

**Decided fix (2026-08-05): dedicated second PSU for the fans**, isolating
them from the Pi/SDR's transient load entirely. If stalling continues even
once isolated on its own supply, that would point back at the fans/MOSFETs
themselves rather than the PSU — a clean next test once the hardware's in
place.

**Update 2026-08-06:** the ~30s `pi-streamer` restart cadence (previously
noted here as deliberately deprioritized) was properly investigated once
Eli reconnected the SDR live and it became time-sensitive. Two separate
causes: a genuine USB power drop on the dongle (confirmed independent of
the app — still relevant to the PSU theory above) plus a misconfigured
`rtl_squelch`/`gate_threshold` that was the actual dominant cause of the
tight cadence, fixed live. Full writeup in `~/tv/claude-notes/pi-streamer-sdr.md`'s
"~30s Restart-Cadence" section.

**Note:** the 2026-08-05 GPIO rewire (final: intake → BCM17, exhaust →
BCM27, heatsink new on BCM22 — see GPIO pins section for the full
correction history) is unrelated to this investigation — it's prep for
the physical PSU change and grouping all three fans together on the
board, not a fix for the stalling itself. Software/electrical correctness
of the new pins was verified the same way as everything else here (see
GPIO pins section above and the deploy log), but the underlying
reliability question is still open until the new PSU is actually in and
tested. All three fans are physically disconnected as of this rewire —
including exhaust, which worked fine on its old pin — so nothing here
should be assumed live until Eli confirms the physical work is done.

## Files

- `fan_control.py` — persistent daemon; sets all three fans' GPIO state
  from config (or `--fan {intake,exhaust,heatsink,all}`/`--on`/`--off` for
  a manual override) and holds.
- `pi-fan.conf` — `FAN_INTAKE_GPIO`/`FAN_EXHAUST_GPIO`/`FAN_HEATSINK_GPIO`
  pin numbers and `FAN_INTAKE_ENABLED`/`FAN_EXHAUST_ENABLED`/
  `FAN_HEATSINK_ENABLED` (1/0) — **all three default to `0`** as of the
  2026-08-05 rewire, since none are physically connected to their
  configured pins right now. Same `EnvironmentFile` pattern as
  `../pi-streamer.conf`.
- `pi-fan.service` — `Type=simple`, runs as the unprivileged `pi` user
  (member of the `gpio` group — no root needed for GPIO chardev access).
- Also controllable from the pi-streamer-sdr web UI's "Cooling Fans" panel
  (`/api/fan/status`, `/api/fan/set` in `../app.py`) — same config-edit +
  `systemctl restart pi-fan` mechanism, just triggered from a button.

## Deploy

```bash
# On the Pi (pi-sdr-ts) — only needed once, to drop the now-unused pwm-2chan overlay:
sudo sed -i '/dtoverlay=pwm-2chan/d' /boot/firmware/config.txt
sudo reboot

# Then:
sudo mkdir -p /opt/pi-fan
# from your machine:
scp fan_control.py pi-fan.conf pi-sdr-ts:/tmp/
ssh pi-sdr-ts "sudo cp /tmp/fan_control.py /tmp/pi-fan.conf /opt/pi-fan/ && sudo chown pi:pi /opt/pi-fan/*"

sudo cp pi-fan.service /etc/systemd/system/pi-fan.service
sudo systemctl daemon-reload
sudo systemctl enable --now pi-fan

# Verify
sudo systemctl status pi-fan
journalctl -u pi-fan -f
```

To change state later: edit `FAN_INTAKE_ENABLED` / `FAN_EXHAUST_ENABLED` in
`/opt/pi-fan/pi-fan.conf`, then `sudo systemctl restart pi-fan`.
