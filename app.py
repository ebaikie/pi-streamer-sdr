#!/usr/bin/env python3
"""Pi Streamer SDR — RTL-SDR to Icecast streaming server.

Pipeline: rtl_fm → sox (EQ + compand gate) → pv (buffer) → ffmpeg (MP3) → Icecast

rtl_fm runs with squelch OFF (-l 0) so the byte stream is continuous.
The sox compand is the effective squelch — it attenuates idle noise to near-
silence without stopping the stream. pv buffers brief upstream stalls.
"""

import json as jsonlib
import os
import random
import subprocess
import threading
import time
from flask import Flask, render_template, jsonify, request
import socket
from urllib.request import urlopen

app = Flask(__name__)

USB_HUB  = "1-1"
USB_PORT = "5"

RTL_FREQUENCY   = os.environ.get("RTL_FREQUENCY", "164.750M")
RTL_MODULATION  = os.environ.get("RTL_MODULATION", "fm")
RTL_GAIN        = os.environ.get("RTL_GAIN", "40")
RTL_PPM         = os.environ.get("RTL_PPM", "0")
RTL_SAMPLE_RATE = int(os.environ.get("RTL_SAMPLE_RATE", "22050"))
ICECAST_HOST    = os.environ.get("ICECAST_HOST", "localhost")
ICECAST_PORT    = int(os.environ.get("ICECAST_PORT", "8000"))
ICECAST_SOURCE_PASSWORD = os.environ.get("ICECAST_SOURCE_PASSWORD", "hackme")
WEB_UI_PORT     = int(os.environ.get("WEB_UI_PORT", "5080"))
INSTALL_DIR     = os.path.dirname(os.path.abspath(__file__))
STATE_FILE      = os.path.join(INSTALL_DIR, "tuning_state.json")

pipeline_lock = threading.Lock()
state = {
    "running": False, "proc": None, "monitor_thread": None,
    "signal_level": 0.0, "peak_level": 0.0, "error": None, "last_cmd": "",
    "sdr_present": None, "fast_death_count": 0, "proc_start_time": None,
}

tuning = {
    "bitrate": 96, "gate_threshold": 0, "vol_boost": 0,
    "eq_low_cut": 300, "eq_high_cut": 3500, "eq_speech_boost": 3,
    "frequency": RTL_FREQUENCY, "modulation": RTL_MODULATION,
    "gain": int(RTL_GAIN), "ppm": int(RTL_PPM),
    "rtl_squelch": 40,
    "noise_level": 4,
    "presets": [],
}

scan_state = {
    "active": False,
    "thread": None,
    "sweep": [],       # [[hz, dbfs], ...] — latest sweep
    "peak_hold": {},   # {hz_int: dbfs_float} — max per bin, never decays
    "scan_error": None,
    "sweep_count": 0,
}

def save_tuning():
    try:
        with open(STATE_FILE, "w") as f:
            jsonlib.dump(tuning, f, indent=2)
    except Exception as e:
        print(f"[STREAM] Failed to save tuning: {e}", flush=True)

def load_tuning():
    try:
        with open(STATE_FILE) as f:
            saved = jsonlib.load(f)
        for k, v in saved.items():
            if k in tuning:
                tuning[k] = v
        print(f"[STREAM] Loaded tuning: freq={tuning['frequency']} "
              f"mod={tuning['modulation']} gain={tuning['gain']}", flush=True)
    except FileNotFoundError:
        print("[STREAM] No saved tuning, using defaults", flush=True)
    except Exception as e:
        print(f"[STREAM] Failed to load tuning: {e}", flush=True)

def build_rtl_fm_args():
    gain = tuning.get("gain", int(RTL_GAIN))
    args = [
        "rtl_fm",
        "-f", str(tuning.get("frequency", RTL_FREQUENCY)),
        "-M", str(tuning.get("modulation", RTL_MODULATION)),
        "-s", str(RTL_SAMPLE_RATE),
        "-l", str(tuning.get("rtl_squelch", 0)),  # 0=off; silence_inject.py keeps stream alive when squelch > 0
        "-p", str(tuning.get("ppm", int(RTL_PPM))),
    ]
    if gain != -1:  # -1 = AGC (omit -g flag)
        args += ["-g", str(gain)]
    return args

def _parse_center_hz(freq_str):
    s = str(freq_str).upper().replace("M", "").replace("HZ", "").strip()
    n = float(s)
    return int(n * 1_000_000) if n < 1_000_000 else int(n)

def build_rtl_power_args(span_hz=800_000, step_hz=10_000, integration_secs=2):
    center = _parse_center_hz(tuning.get("frequency", RTL_FREQUENCY))
    low_hz  = center - span_hz // 2
    high_hz = center + span_hz // 2
    gain = tuning.get("gain", int(RTL_GAIN))
    ppm  = tuning.get("ppm", int(RTL_PPM))
    args = [
        "rtl_power",
        "-f", f"{low_hz}:{high_hz}:{step_hz}",
        "-i", str(integration_secs),
        "-p", str(ppm),
        "-1",
    ]
    if gain != -1:
        args += ["-g", str(gain)]
    args.append("-")  # write CSV to stdout
    return args

def parse_rtl_power_csv(text):
    points = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            hz_low  = int(parts[2])
            hz_step = float(parts[4])
            powers  = [float(x) for x in parts[6:] if x.strip()]
            for i, dbfs in enumerate(powers):
                center = int(hz_low + (i + 0.5) * hz_step)
                points.append((center, dbfs))
        except (ValueError, IndexError):
            continue
    return sorted(points, key=lambda x: x[0])

def scan_loop():
    print("[SCAN] Scan loop started", flush=True)
    while scan_state["active"]:
        args = build_rtl_power_args()
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                err = (result.stderr[:200] if result.stderr else "unknown error").strip()
                scan_state["scan_error"] = f"rtl_power exit {result.returncode}: {err}"
                print(f"[SCAN] rtl_power error: {err}", flush=True)
                time.sleep(1)
                continue
            points = parse_rtl_power_csv(result.stdout)
            if not points:
                scan_state["scan_error"] = "No data from rtl_power"
                time.sleep(1)
                continue
            scan_state["scan_error"] = None
            scan_state["sweep"] = [[hz, dbfs] for hz, dbfs in points]
            scan_state["sweep_count"] += 1
            ph = scan_state["peak_hold"]
            for hz, dbfs in points:
                if hz not in ph or dbfs > ph[hz]:
                    ph[hz] = dbfs
        except subprocess.TimeoutExpired:
            scan_state["scan_error"] = "rtl_power timed out"
            print("[SCAN] rtl_power timed out", flush=True)
            time.sleep(1)
        except FileNotFoundError:
            scan_state["scan_error"] = "rtl_power not found"
            scan_state["active"] = False
            print("[SCAN] rtl_power not found — is rtl-sdr installed?", flush=True)
        except Exception as e:
            scan_state["scan_error"] = str(e)
            print(f"[SCAN] Exception: {e}", flush=True)
            time.sleep(1)
    print("[SCAN] Scan loop ended", flush=True)

def start_scan():
    with pipeline_lock:
        if scan_state["active"]:
            return {"ok": False, "error": "Scan already active"}
        _stop_pipeline_locked()
        scan_state["active"]      = True
        scan_state["sweep"]       = []
        scan_state["scan_error"]  = None
        scan_state["sweep_count"] = 0
        t = threading.Thread(target=scan_loop, daemon=True)
        t.start()
        scan_state["thread"] = t
        return {"ok": True}

def stop_scan():
    with pipeline_lock:
        scan_state["active"] = False
        scan_state["thread"] = None
        subprocess.run(["pkill", "-9", "rtl_power"], capture_output=True)
        time.sleep(0.5)
        return {"ok": True}

def build_sox_filter_args():
    thresh = int(tuning.get("gate_threshold", 3))
    low_cut = int(tuning.get("eq_low_cut", 200))
    high_cut = int(tuning.get("eq_high_cut", 3500))
    speech_boost = int(tuning.get("eq_speech_boost", 6))
    vol_boost = int(tuning.get("vol_boost", 0))
    sr = RTL_SAMPLE_RATE
    nyquist = sr // 2 - 100
    high_cut = min(high_cut, nyquist)
    effects = []
    if low_cut > 0:
        effects += ["highpass", str(low_cut)]
    if 0 < high_cut < nyquist + 100:
        effects += ["lowpass", str(high_cut)]
    if speech_boost > 0:
        effects += ["equalizer", "1500", "1.5q", f"+{speech_boost}"]
    if thresh > 0:
        knee = int(-70 + (thresh - 1) * 5.5)
        above = min(knee + 15, -5)
        tf = f"6:-inf,-inf,{knee},-inf,{above},{above},0,0"
        effects += ["compand", "0.01,0.3", tf, "0"]
    if vol_boost != 0:
        effects += ["gain", str(vol_boost)]
    if not effects:
        effects = ["vol", "1.0"]
    return [
        "sox",
        "-t", "raw", "-r", str(sr), "-e", "signed-integer", "-b", "16", "-c", "1", "-",
        "-t", "raw", "-r", str(sr), "-e", "signed-integer", "-b", "16", "-c", "1", "-",
        *effects,
    ]

def build_ffmpeg_args():
    icecast_url = (f"icecast://source:{ICECAST_SOURCE_PASSWORD}"
                   f"@{ICECAST_HOST}:{ICECAST_PORT}/scanner")
    return [
        "ffmpeg", "-hide_banner",
        "-f", "s16le", "-ar", str(RTL_SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
        "-codec:a", "libmp3lame", "-b:a", f"{int(tuning['bitrate'])}k",
        "-f", "mp3", "-content_type", "audio/mpeg",
        icecast_url,
    ]

def build_shell_command():
    rtl = " ".join(build_rtl_fm_args())
    sox = " ".join(build_sox_filter_args())
    buf = "pv -q -B 1m"
    ffm = " ".join(build_ffmpeg_args())
    kill = "pkill -9 rtl_fm; pkill -9 sox; pkill -9 pv; pkill -9 ffmpeg; sleep 1"
    if tuning.get("rtl_squelch", 0) > 0:
        amp = tuning.get("noise_level", 5) / 1000.0
        inject = f"python3 {INSTALL_DIR}/silence_inject.py {RTL_SAMPLE_RATE} {amp:.4f}"
        return f"{kill}; {rtl} | {inject} | {sox} | {buf} | {ffm}"
    return f"{kill}; {rtl} | {sox} | {buf} | {ffm}"

def poll_icecast_stats():
    try:
        url = f"http://{ICECAST_HOST}:{ICECAST_PORT}/status-json.xsl"
        with urlopen(url, timeout=2) as resp:
            data = jsonlib.loads(resp.read().decode())
        source = data.get("icestats", {}).get("source")
        if source is None:
            return 0.0
        sources = [source] if isinstance(source, dict) else source
        for s in sources:
            if "/scanner" in s.get("listenurl", ""):
                return 65.0
        return 0.0
    except Exception:
        return 0.0

def check_sdr_present():
    try:
        r = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5)
        out = r.stdout.lower()
        return any(s in out for s in ["0bda:2838", "0bda:2832", "rtl2838", "rtl2832"])
    except Exception:
        return None

def sdr_check_loop():
    while True:
        state["sdr_present"] = check_sdr_present()
        time.sleep(10)

def monitor_loop():
    decay = 0.9
    restart_count = 0
    mount_missing_count = 0
    MOUNT_MISSING_THRESHOLD = 20  # seconds before treating a missing Icecast mount as a hung pipeline
    heartbeat_counter = 0

    while state["running"]:
        time.sleep(1)
        heartbeat_counter += 1
        if heartbeat_counter % 300 == 0:
            print(f"[STREAM] Heartbeat: running, restarts={restart_count}", flush=True)

        proc_dead = state["proc"] and state["proc"].poll() is not None
        level = poll_icecast_stats()
        if level > 0:
            mount_missing_count = 0
            level += random.uniform(-15, 15)
            level = max(10.0, min(95.0, level))
        else:
            mount_missing_count += 1

        state["signal_level"] = round(level, 1)
        state["peak_level"] = max(level, state["peak_level"] * decay)

        needs_restart = False
        reason = ""
        if proc_dead:
            elapsed = time.time() - (state["proc_start_time"] or 0)
            if elapsed < 5:
                state["fast_death_count"] += 1
                if state["fast_death_count"] >= 3:
                    print(f"[STREAM] WARNING: SDR may be disconnected "
                          f"({state['fast_death_count']} rapid exits)", flush=True)
            else:
                state["fast_death_count"] = 0
            needs_restart = True
            err = ""
            try:
                err = (state["proc"].stderr.read().decode(errors="replace")[:200]
                       if state["proc"].stderr else "")
            except Exception:
                pass
            reason = f"Process exited: {err}" if err else "Process exited"
        elif mount_missing_count >= MOUNT_MISSING_THRESHOLD:
            needs_restart = True
            reason = f"Icecast mount missing for {mount_missing_count}s"

        if not needs_restart:
            continue

        restart_count += 1
        state["signal_level"] = 0
        state["peak_level"] = 0
        print(f"[STREAM] {reason}", flush=True)

        # Back off up to 30s after many failures — never give up permanently
        delay = min(30, 3 + (restart_count - 1) * 2)
        print(f"[STREAM] Auto-restart #{restart_count} in {delay}s...", flush=True)
        state["error"] = f"Restarting (#{restart_count})..."
        state["running"] = False
        time.sleep(delay)
        result = start_pipeline()
        if result.get("ok"):
            print("[STREAM] Auto-restart successful", flush=True)
            mount_missing_count = 0
            return
        else:
            print(f"[STREAM] Auto-restart failed: {result.get('error')}", flush=True)
            state["error"] = result.get("error")
            state["running"] = True
            mount_missing_count = 0
            time.sleep(5)

    state["signal_level"] = 0
    state["peak_level"] = 0

def usb_dongle_off():
    subprocess.run(["uhubctl", "-l", USB_HUB, "-p", USB_PORT, "-a", "off"], capture_output=True)
    time.sleep(2)

def usb_dongle_on():
    subprocess.run(["uhubctl", "-l", USB_HUB, "-p", USB_PORT, "-a", "on"], capture_output=True)
    time.sleep(3)  # allow USB enumeration

def usb_dongle_cycle():
    print("[STREAM] USB power cycling SDR dongle...", flush=True)
    usb_dongle_off()
    usb_dongle_on()

def kill_existing():
    subprocess.run(["pkill", "-9", "rtl_fm"],          capture_output=True)
    subprocess.run(["pkill", "-9", "sox"],             capture_output=True)
    subprocess.run(["pkill", "-9", "ffmpeg"],          capture_output=True)
    subprocess.run(["pkill", "-f", "silence_inject"],  capture_output=True)
    time.sleep(1)

def start_pipeline():
    with pipeline_lock:
        if state["running"]:
            proc_alive = state["proc"] and state["proc"].poll() is None
            if proc_alive:
                return {"ok": False, "error": "Already running"}
            print("[STREAM] Stale state, forcing cleanup...", flush=True)
            state["running"] = False

        # Always reap the old shell process to prevent zombie accumulation
        if state["proc"]:
            try:
                if state["proc"].poll() is None:
                    state["proc"].kill()
                state["proc"].wait(timeout=3)
            except Exception:
                pass
            state["proc"] = None

        state["error"] = None
        state["signal_level"] = 0
        state["peak_level"] = 0
        kill_existing()

        if state["fast_death_count"] >= 3:
            usb_dongle_cycle()
        else:
            usb_dongle_on()

        shell_cmd = build_shell_command()
        state["last_cmd"] = shell_cmd
        print(f"[STREAM] Command: {shell_cmd}", flush=True)

        try:
            proc = subprocess.Popen(shell_cmd, shell=True,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
            time.sleep(2)
            if proc.poll() is not None:
                err = proc.stderr.read().decode(errors="replace")
                return {"ok": False, "error": f"Pipeline exited: {err}"}

            state["proc"] = proc
            state["running"] = True
            state["proc_start_time"] = time.time()
            t = threading.Thread(target=monitor_loop, daemon=True)
            t.start()
            state["monitor_thread"] = t
            return {"ok": True, "cmd": shell_cmd}
        except Exception as e:
            kill_existing()
            return {"ok": False, "error": str(e)}

def _stop_pipeline_locked():
    """Stop pipeline without acquiring pipeline_lock (caller must hold it)."""
    state["running"] = False
    state["fast_death_count"] = 0
    if state["proc"]:
        try:
            state["proc"].kill()
            state["proc"].wait(timeout=3)
        except Exception:
            pass
    kill_existing()
    state["proc"] = None
    state["monitor_thread"] = None
    state["signal_level"] = 0
    state["peak_level"] = 0
    state["error"] = None

def stop_pipeline():
    with pipeline_lock:
        _stop_pipeline_locked()
        return {"ok": True}

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    for key in ("bitrate", "gate_threshold", "vol_boost", "rtl_squelch", "noise_level", "eq_low_cut", "eq_high_cut",
                "eq_speech_boost", "gain", "ppm"):
        if key in data:
            tuning[key] = int(data[key])
    for key in ("frequency", "modulation"):
        if key in data:
            tuning[key] = str(data[key]).strip()
    result = start_pipeline()
    if result.get("ok"):
        save_tuning()
    return jsonify(result)

@app.route("/api/stop", methods=["POST"])
def api_stop():
    return jsonify(stop_pipeline())

@app.route("/api/tune", methods=["POST"])
def api_tune():
    """Retune without a full stop/start from the UI — just update and restart."""
    data = request.get_json(silent=True) or {}
    was_running = state["running"]
    if "frequency" in data and str(data["frequency"]).strip() != str(tuning.get("frequency", "")):
        if scan_state["active"]:
            scan_state["peak_hold"].clear()
    for key in ("bitrate", "gate_threshold", "vol_boost", "rtl_squelch", "noise_level", "eq_low_cut", "eq_high_cut",
                "eq_speech_boost", "gain", "ppm"):
        if key in data:
            tuning[key] = int(data[key])
    for key in ("frequency", "modulation"):
        if key in data:
            tuning[key] = str(data[key]).strip()
    save_tuning()
    if not was_running:
        return jsonify({"ok": True, "restarted": False})
    stop_pipeline()
    time.sleep(0.5)
    result = start_pipeline()
    result["restarted"] = True
    return jsonify(result)

@app.route("/api/presets", methods=["GET"])
def api_presets_get():
    return jsonify(tuning.get("presets", []))

@app.route("/api/presets", methods=["POST"])
def api_presets_save():
    data = request.get_json(silent=True) or {}
    label = str(data.get("label", "")).strip()
    freq  = str(data.get("frequency", tuning.get("frequency", ""))).strip()
    if not label or not freq:
        return jsonify({"ok": False, "error": "label and frequency required"}), 400
    preset = {"label": label}
    for key in ("frequency", "modulation", "gain", "ppm", "rtl_squelch", "noise_level",
                "eq_low_cut", "eq_high_cut", "eq_speech_boost", "gate_threshold", "vol_boost", "bitrate"):
        if key in data:
            preset[key] = data[key]
        elif key in tuning:
            preset[key] = tuning[key]
    presets = [p for p in tuning.get("presets", []) if p.get("label") != label]
    presets.append(preset)
    tuning["presets"] = presets
    save_tuning()
    return jsonify({"ok": True, "presets": presets})

@app.route("/api/presets/<label>", methods=["DELETE"])
def api_presets_delete(label):
    tuning["presets"] = [p for p in tuning.get("presets", []) if p.get("label") != label]
    save_tuning()
    return jsonify({"ok": True, "presets": tuning["presets"]})

@app.route("/api/status")
def api_status():
    return jsonify({
        "running": state["running"],
        "signal_level": state["signal_level"],
        "peak_level": state["peak_level"],
        "error": state["error"],
        "tuning": tuning,
        "last_cmd": state["last_cmd"],
        "sdr_present": state["sdr_present"],
        "fast_death_count": state["fast_death_count"],
        "scan_active": scan_state["active"],
    })

@app.route("/api/scan/start", methods=["POST"])
def api_scan_start():
    return jsonify(start_scan())

@app.route("/api/scan/stop", methods=["POST"])
def api_scan_stop():
    return jsonify(stop_scan())

@app.route("/api/scan/resume", methods=["POST"])
def api_scan_resume():
    stop_scan()
    time.sleep(0.5)
    result = start_pipeline()
    return jsonify(result)

@app.route("/api/scan/data")
def api_scan_data():
    return jsonify({
        "active":      scan_state["active"],
        "sweep":       scan_state["sweep"],
        "peak_hold":   sorted(scan_state["peak_hold"].items()),
        "scan_error":  scan_state["scan_error"],
        "sweep_count": scan_state["sweep_count"],
        "center_hz":   _parse_center_hz(tuning.get("frequency", RTL_FREQUENCY)),
        "gain":        tuning.get("gain", int(RTL_GAIN)),
    })

@app.route("/api/scan/peak_reset", methods=["POST"])
def api_scan_peak_reset():
    scan_state["peak_hold"].clear()
    return jsonify({"ok": True})

@app.route("/api/scan/set_gain", methods=["POST"])
def api_scan_set_gain():
    data = request.get_json(silent=True) or {}
    if "gain" in data:
        tuning["gain"] = int(data["gain"])
    return jsonify({"ok": True, "gain": tuning["gain"]})

if __name__ == "__main__":
    print(f"[STREAM] Pi Streamer SDR starting", flush=True)
    print(f"[STREAM] Icecast: {ICECAST_HOST}:{ICECAST_PORT}", flush=True)
    print(f"[STREAM] Web UI: 0.0.0.0:{WEB_UI_PORT}", flush=True)
    load_tuning()
    print(f"[STREAM] Frequency: {tuning['frequency']} {tuning['modulation'].upper()} "
          f"gain={tuning['gain']} ppm={tuning['ppm']}", flush=True)

    def auto_start():
        usb_dongle_off()
        print("[STREAM] USB SDR port powered off (boot init)", flush=True)
        for attempt in range(15):
            try:
                with socket.create_connection((ICECAST_HOST, ICECAST_PORT), timeout=2):
                    break
            except OSError:
                print(f"[STREAM] Waiting for Icecast... ({attempt+1}/15)", flush=True)
                time.sleep(2)
        else:
            print("[STREAM] WARNING: Icecast not reachable, starting anyway", flush=True)
        time.sleep(1)
        print("[STREAM] Auto-starting pipeline...", flush=True)
        result = start_pipeline()
        if result.get("ok"):
            print("[STREAM] Auto-start successful", flush=True)
        else:
            print(f"[STREAM] Auto-start failed: {result.get('error')}", flush=True)

    threading.Thread(target=auto_start, daemon=True).start()
    threading.Thread(target=sdr_check_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=WEB_UI_PORT, debug=False)
