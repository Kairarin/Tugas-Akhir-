"""
Flask IDS Server v6.1 (RF + XGBOOST + IDS STATE MACHINE)
==========================================================
TAMBAHAN dari v6.0:
  [NEW] IDS State Machine: persistence, hysteresis, cooldown, recovery
  [NEW] Temporary Status per-baris
  [NEW] Final Status berbasis state machine
  [NEW] Terminal dashboard box layout per baris
  [NEW] Full text label (INTERVAL, LATENCY, JITTER, PACKET RATE)
  [NEW] CSV tambahan kolom: temporary_status, final_status, attack_counter, recovery_counter

TIDAK DIUBAH:
  arsitektur, Flask routes, async CSV, queue, threading,
  Waitress, health endpoint, /status, /setlabel,
  ML inference workflow, rolling stats, model loading,
  ESP32 payload structure
"""

from flask import Flask, request, jsonify
import pandas as pd
import numpy as np
import os, time, queue, threading, csv, logging
from datetime import datetime
from collections import deque
import joblib
import warnings

warnings.filterwarnings("ignore", category=UserWarning)

logging.getLogger("werkzeug").setLevel(logging.ERROR)
logging.getLogger("waitress").setLevel(logging.ERROR)

app = Flask(__name__)

# ─── CONFIG ──────────────────────────────────────────────
CSV_FILE      = "dataset_realtime_log.csv"
PORT          = 5000
QUEUE_MAXSIZE = 1000
_ROLLING_N    = 200

current_label = os.environ.get("LABEL_MODE", "normal")

CSV_COLUMNS = [
    "timestamp_unix", "timestamp_esp", "device_id",
    "interval_ms", "latency_ms", "jitter_ms", "packet_rate",
    "label_aktual", "prediksi_rf", "prediksi_xgb",
    "temporary_status", "final_status",
    "attack_counter", "recovery_counter"
]

FEATURE_COLS = ["interval_ms", "latency_ms", "jitter_ms", "packet_rate"]

# ─── ANSI ────────────────────────────────────────────────
R   = "\033[0m"
BO  = "\033[1m"
GR  = "\033[92m"
YE  = "\033[93m"
RE  = "\033[91m"
CY  = "\033[96m"
BL  = "\033[94m"
MA  = "\033[95m"
WHI = "\033[97m"

# ─── LOAD MODEL ──────────────────────────────────────────
print(f"\n{CY}{'='*60}{R}")
print(f"{BO}  MEMUAT MODEL MACHINE LEARNING...{R}")
print(f"{CY}{'='*60}{R}")
try:
    model_rf  = joblib.load("model_rf.pkl")
    model_xgb = joblib.load("model_xgb.pkl")
    encoder   = joblib.load("label_encoder.pkl")
    ML_READY  = True
    print(f"  {GR}✅ Random Forest : Siap!{R}")
    print(f"  {GR}✅ XGBoost       : Siap!{R}")
    print(f"  {GR}✅ Label Encoder : Siap!{R}")
except Exception as e:
    model_rf = model_xgb = encoder = None
    ML_READY = False
    print(f"  {RE}❌ Gagal memuat model: {e}{R}")
    print(f"  {YE}   Pastikan model_rf.pkl, model_xgb.pkl, label_encoder.pkl ada.{R}")
print(f"{CY}{'='*60}{R}\n")

# ─── IN-MEMORY COUNTERS ───────────────────────────────────
_counters      = {"total": 0, "normal": 0, "dos": 0}
_counters_lock = threading.Lock()

# Counter khusus tampilan terminal realtime (output final status)
_display_counters = {"total": 0, "normal": 0, "dos": 0}
_display_lock     = threading.Lock()

_rolling = {
    "normal": {k: deque(maxlen=_ROLLING_N) for k in FEATURE_COLS},
    "dos"   : {k: deque(maxlen=_ROLLING_N) for k in FEATURE_COLS},
}
_rolling_lock = threading.Lock()

# ─── ASYNC CSV WRITER ────────────────────────────────────
write_queue = queue.Queue(maxsize=QUEUE_MAXSIZE)

def csv_writer_thread():
    while True:
        try:
            row = write_queue.get(timeout=2)
            if row is None:
                break
            file_exists = os.path.exists(CSV_FILE)
            with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                if not file_exists:
                    writer.writeheader()
                writer.writerow(row)
            write_queue.task_done()
        except queue.Empty:
            continue
        except Exception:
            pass

threading.Thread(target=csv_writer_thread, daemon=True).start()

# ═══════════════════════════════════════════════════════════
#  IDS STATE MACHINE — GLOBAL VARIABLES
# ═══════════════════════════════════════════════════════════
# State yang mungkin:
#   "NORMAL"              → sistem aman
#   "UNDER_MONITORING"    → ada indikasi tapi belum konfirm
#   "CONFIRM_DOS_ATTACK"  → threshold tercapai, tampil SEKALI
#   "DOS_ATTACK"          → serangan berlangsung
#   "RECOVERY_MONITORING" → serangan berhenti, masih dipantau

IDS_STATE            = "NORMAL"
IDS_ATTACK_COUNTER   = 0     # hitung berapa kali DOS/SUSPICIOUS berturut-turut
IDS_RECOVERY_COUNTER = 0     # hitung berapa kali NORMAL berturut-turut saat recovery
IDS_CONFIRM_SHOWN    = False  # CONFIRM_DOS_ATTACK hanya muncul sekali

# Threshold (bisa disesuaikan)
ATTACK_CONFIRM_THRESHOLD  = 10   # >= 10 hit berturut-turut → CONFIRM DOS
RECOVERY_NORMAL_THRESHOLD = 5    # >= 5 normal berturut-turut → kembali NORMAL

ids_lock = threading.Lock()

# ─── HELPER ML ───────────────────────────────────────────
def normalize_label(x):
    if x is None:
        return "unknown"
    if isinstance(x, (np.integer, np.int64, int)):
        try:
            return str(encoder.inverse_transform([int(x)])[0]).strip().lower()
        except Exception:
            return str(x)
    return str(x).strip().lower()

def predict_rf(X_df):
    return normalize_label(model_rf.predict(X_df)[0])

def predict_xgb(X_df):
    return normalize_label(model_xgb.predict(X_df)[0])

# ─── TEMPORARY STATUS ────────────────────────────────────
def get_temporary_status(rf_label, xgb_label):
    """
    Dihitung dari satu baris inferensi.
    Return: string status
    """
    if rf_label == "normal" and xgb_label == "normal":
        return "NORMAL"
    elif rf_label == "dos" and xgb_label == "dos":
        return "DOS INDICATED"
    else:
        return "SUSPICIOUS"

# ─── IDS STATE MACHINE ───────────────────────────────────
def update_ids_state(temp_status):
    """
    Memperbarui IDS global state berdasarkan temporary_status.
    Menggunakan persistence, hysteresis, dan recovery logic.
    Return: final_status string
    """
    global IDS_STATE, IDS_ATTACK_COUNTER, IDS_RECOVERY_COUNTER, IDS_CONFIRM_SHOWN

    with ids_lock:

        if temp_status == "NORMAL":
            # ── Jalur NORMAL ──────────────────────────────
            if IDS_STATE == "NORMAL":
                # Sudah normal, tetap normal
                IDS_ATTACK_COUNTER = 0
                return "NORMAL"

            elif IDS_STATE == "UNDER_MONITORING":
                # Kembali normal sebelum konfirmasi → reset
                IDS_ATTACK_COUNTER = 0
                IDS_STATE = "NORMAL"
                return "NORMAL"

            elif IDS_STATE in ("CONFIRM_DOS_ATTACK", "DOS_ATTACK"):
                # Mulai recovery monitoring
                IDS_STATE = "RECOVERY_MONITORING"
                IDS_RECOVERY_COUNTER = 1
                IDS_ATTACK_COUNTER   = 0
                return "RECOVERY MONITORING"

            elif IDS_STATE == "RECOVERY_MONITORING":
                IDS_RECOVERY_COUNTER += 1
                if IDS_RECOVERY_COUNTER >= RECOVERY_NORMAL_THRESHOLD:
                    # Cukup normal berturut-turut → benar-benar aman
                    IDS_STATE            = "NORMAL"
                    IDS_ATTACK_COUNTER   = 0
                    IDS_RECOVERY_COUNTER = 0
                    IDS_CONFIRM_SHOWN    = False
                    return "NORMAL"
                else:
                    return "RECOVERY MONITORING"

        else:
            # temp_status adalah "DOS INDICATED" atau "SUSPICIOUS"
            IDS_RECOVERY_COUNTER = 0   # reset recovery karena masih ada indikasi

            if IDS_STATE == "NORMAL":
                IDS_STATE          = "UNDER_MONITORING"
                IDS_ATTACK_COUNTER = 1
                return "UNDER MONITORING"

            elif IDS_STATE == "UNDER_MONITORING":
                IDS_ATTACK_COUNTER += 1
                if IDS_ATTACK_COUNTER >= ATTACK_CONFIRM_THRESHOLD:
                    if not IDS_CONFIRM_SHOWN:
                        # Tampilkan CONFIRM hanya sekali
                        IDS_STATE         = "CONFIRM_DOS_ATTACK"
                        IDS_CONFIRM_SHOWN = True
                        return "CONFIRMED DOS ATTACK"
                    else:
                        IDS_STATE = "DOS_ATTACK"
                        return "DOS ATTACK"
                else:
                    return "UNDER MONITORING"

            elif IDS_STATE == "CONFIRM_DOS_ATTACK":
                # Setelah konfirmasi tampil, langsung ke DOS_ATTACK
                IDS_STATE          = "DOS_ATTACK"
                IDS_ATTACK_COUNTER += 1
                return "DOS ATTACK"

            elif IDS_STATE == "DOS_ATTACK":
                IDS_ATTACK_COUNTER += 1
                return "DOS ATTACK"

            elif IDS_STATE == "RECOVERY_MONITORING":
                # Masih ada serangan saat recovery → balik ke DOS_ATTACK
                IDS_STATE            = "DOS_ATTACK"
                IDS_RECOVERY_COUNTER = 0
                IDS_ATTACK_COUNTER  += 1
                return "DOS ATTACK"

    return "NORMAL"

# ─── WARNA / ICON PER STATUS ─────────────────────────────
def color_temp(status):
    m = {
        "NORMAL"        : f"{GR}🟢 NORMAL{R}",
        "DOS INDICATED" : f"{RE}🔴 DOS INDICATED{R}",
        "SUSPICIOUS"    : f"{YE}🟡 SUSPICIOUS{R}",
    }
    return m.get(status, f"{YE}{status}{R}")

def color_final(status):
    m = {
        "NORMAL"                : f"{GR}🟢 NORMAL{R}",
        "UNDER MONITORING"      : f"{YE}🟡UNDER MONITORING{R}",
        "CONFIRMED DOS ATTACK"  : f"{RE}{BO}🚨 CONFIRMED DOS ATTACK{R}",
        "CONFIRM DOS ATTACK"    : f"{RE}{BO}🚨 CONFIRMED DOS ATTACK{R}",  # alias aman
        "DOS ATTACK"            : f"{RE}{BO}🔴 DOS ATTACK IN PROGRESS{R}",
        "RECOVERY MONITORING"   : f"{CY}🔄 RECOVERY MONITORING{R}",
        "ML NOT READY"          : f"{MA}❌ ML NOT READY{R}",
    }
    return m.get(status, f"{YE}{status}{R}")

def color_rf(label):
    if label == "normal": return f"{BL} {GR}🟢 NORMAL{R}"
    if label == "dos"   : return f"{BL} {RE}🔴 DOS{R}"
    return f"{BL} {YE}🟡 ?{R}"

def color_xgb(label):
    if label == "normal": return f"{CY} {GR}🟢 NORMAL{R}"
    if label == "dos"   : return f"{CY} {RE}🔴 DOS{R}"
    return f"{CY} {YE}🟡 ?{R}"


# ─── TERMINAL DASHBOARD ───────────────────────────────────
# Layout 3 kolom:
#   KIRI  : Telemetry + Machine Learning + State Machine
#   TENGAH: IDS Health
#   KANAN : IDS Detection Stats
# Tujuannya agar output terminal tetap rapi, informatif, dan tidak
# mengubah alur server maupun logika deteksi.

def _strip_ansi(s):
    import re
    return re.sub(r'\x1b\[[0-9;]*m', '', str(s))

def _vlen(s):
    from wcwidth import wcswidth
    width = wcswidth(_strip_ansi(s))
    return width if width >= 0 else len(_strip_ansi(s))

def _pad(s, width):
    s = str(s)
    return s + " " * max(0, width - _vlen(s))

def _fit(s, width):
    s = str(s)
    if _vlen(s) <= width:
        return s
    plain = _strip_ansi(s)
    if width <= 1:
        return plain[:width]
    cut = max(0, width - 1)
    return plain[:cut] + "…"

def _bar(current, total, width=10, filled_char="█", empty_char="░"):
    total = max(1, int(total))
    current = max(0, min(int(current), total))
    filled = round(width * current / total)
    return f"[{filled_char * filled}{empty_char * (width - filled)}] {current}/{total}"

def print_ids_box(waktu, interval_ms, latency_ms, jitter_ms, packet_rate,
                  rf_label, xgb_label, temp_status, final_status_str,
                  attack_ctr, recovery_ctr, device_id="unknown"):
    """Cetak dashboard realtime 3 kolom yang lebih rapi dan profesional."""

    import shutil

    # Update counter tampilan output final
    with _display_lock:
        _display_counters["total"] += 1
        if str(final_status_str).strip().upper() == "NORMAL":
            _display_counters["normal"] += 1
        elif str(final_status_str).strip().upper() != "ML NOT READY":
            _display_counters["dos"] += 1

        out_total  = _display_counters["total"]
        out_normal = _display_counters["normal"]
        out_dos    = _display_counters["dos"]

    # Lebar dibuat tetap agar tampilan konsisten di terminal 120–140 kolom.
    left_w  = 60
    mid_w   = 28
    right_w = 31

    # Jika terminal sempit, kurangi sedikit tanpa mengubah struktur.
    term_w = shutil.get_terminal_size((129, 30)).columns
    total_needed = left_w + mid_w + right_w + 10
    if term_w < total_needed:
        spare = max(0, term_w - 10)
        ratio_sum = left_w + mid_w + right_w
        left_w  = max(48, round(spare * left_w / ratio_sum))
        mid_w   = max(24, round(spare * mid_w / ratio_sum))
        right_w = max(28, spare - left_w - mid_w)
        if right_w < 28:
            right_w = 28
            left_w = max(48, spare - mid_w - right_w)

    def top_border():
        print(f"╔{'═'*(left_w+2)}╦{'═'*(mid_w+2)}╦{'═'*(right_w+2)}╗")

    def mid_border():
        print(f"╠{'═'*(left_w+2)}╬{'═'*(mid_w+2)}╬{'═'*(right_w+2)}╣")

    def bot_border():
        print(f"╚{'═'*(left_w+2)}╩{'═'*(mid_w+2)}╩{'═'*(right_w+2)}╝")

    def row(a="", b="", c=""):
        print(
            f"║ {_pad(_fit(a, left_w), left_w)} ║ "
            f"{_pad(_fit(b, mid_w), mid_w)} ║ "
            f"{_pad(_fit(c, right_w), right_w)} ║"
        )

    # Header
    header_left  = f"{BO}{CY} STATEFUL IDS MONITOR{R}"
    header_mid = f"{CY}LAST UPDATE:{R} {WHI}{waktu}{R}"
    header_right = f"{YE}DEVICE:{R} {WHI}{device_id}{R}"

    sub_left  = f"{MA}LIVE TELEMETRY → ML → STATEFUL IDS{R}"

    # Panel kiri
    left_rows = [
        f"{BO}{CY}TELEMETRY{R}",
        f"{YE}├{R} Interval       : {WHI}{interval_ms} ms{R}",
        f"{YE}├{R} Latency        : {WHI}{latency_ms} ms{R}",
        f"{YE}├{R} Jitter         : {WHI}{jitter_ms} ms{R}",
        f"{YE}└{R} Packet Rate    : {WHI}{packet_rate:.1f} pps{R}",
        "",
        f"{BO}{CY}MACHINE LEARNING{R}",
        f"{YE}├{R} Random Forest  : {color_rf(rf_label)}",
        f"{YE}└{R} XGBoost        : {color_xgb(xgb_label)}",
        "",
        f"{BO}{CY}STATE MACHINE{R}",
        f"{YE}├{R} Temporary State : {color_temp(temp_status)}",
        f"{YE}├{R} Final State     : {color_final(final_status_str)}",
        f"{YE}├{R} Attack Counter  : {_bar(attack_ctr, ATTACK_CONFIRM_THRESHOLD, 10)}",
        f"{YE}└{R} Recovery Counter: {_bar(recovery_ctr, RECOVERY_NORMAL_THRESHOLD, 10)}",
    ]

    def health_row(label, value):
        return f"{YE}├{R} {label:<15}: {value}"

    mid_rows = [
        f"{BO}{CY}IDS HEALTH{R}",
        health_row(
            "ML Status",
            f"{GR}READY{R}" if ML_READY else f"{RE}NOT READY{R}"
        ),
        health_row(
            "Random Forest",
            f"{GR}ACTIVE{R}" if ML_READY else f"{RE}INACTIVE{R}"
        ),
        health_row(
            "XGBoost",
            f"{GR}ACTIVE{R}" if ML_READY else f"{RE}INACTIVE{R}"
        ),
        health_row(
            "CSV Writer",
            f"{GR}ACTIVE{R}"
        ),
    f"{YE}└{R} {'Server':<15}: {GR}ONLINE{R}",
]

    # Panel kanan
    normal_rate = (out_normal / out_total * 100.0) if out_total else 0.0
    dos_rate    = (out_dos / out_total * 100.0) if out_total else 0.0
    right_rows = [
        f"{BO}{CY}IDS DETECTION STATS{R}",
        f"{YE}├{R} Processed Data   : {WHI}{out_total}{R}",
        f"{YE}├{R} Detected Normal  : {GR}{out_normal}{R}",
        f"{YE}├{R} Detected DoS     : {RE}{out_dos}{R}",
        f"{YE}├{R} Normal Rate      : {WHI}{normal_rate:.1f}%{R}",
        f"{YE}└{R} DoS Rate         : {WHI}{dos_rate:.1f}%{R}",
    ]

    top_border()
    row(header_left, header_mid, header_right)
    row(sub_left)
    mid_border()

    max_rows = max(len(left_rows), len(mid_rows), len(right_rows))
    for i in range(max_rows):
        a = left_rows[i] if i < len(left_rows) else ""
        b = mid_rows[i] if i < len(mid_rows) else ""
        c = right_rows[i] if i < len(right_rows) else ""
        row(a, b, c)

    bot_border()

# ─── ENDPOINT /health ────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return "ok", 200

# ─── ENDPOINT /data ──────────────────────────────────────
@app.route("/data", methods=["POST"])
def terima_data():
    global current_label
    server_time = time.time()

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error"}), 400

    device_id   = data.get("device_id",   "unknown")
    esp_ts      = data.get("timestamp",    0)
    interval_ms = int(data.get("interval_ms",  0))
    latency_ms  = int(data.get("latency_ms",   0))
    jitter_ms   = int(data.get("jitter_ms",    0))
    packet_rate = float(data.get("packet_rate",  0.0))

    # ── ML Inference ─────────────────────────────────────
    pred_rf       = "unknown"
    pred_xgb      = "unknown"
    temp_status   = "UNKNOWN"
    final_st      = "ML NOT READY"

    if ML_READY:
        X_live = pd.DataFrame([{
            "interval_ms": interval_ms,
            "latency_ms" : latency_ms,
            "jitter_ms"  : jitter_ms,
            "packet_rate": packet_rate
        }], columns=FEATURE_COLS)

        pred_rf  = predict_rf(X_live)
        pred_xgb = predict_xgb(X_live)

        # Temporary status
        temp_status = get_temporary_status(pred_rf, pred_xgb)

        # Final status via state machine
        final_st = update_ids_state(temp_status)

    # ── Update counters ───────────────────────────────────
    with _counters_lock:
        _counters["total"] += 1
        if current_label in _counters:
            _counters[current_label] += 1

    lk = current_label if current_label in _rolling else "normal"
    with _rolling_lock:
        for k, v in zip(FEATURE_COLS,
                        [interval_ms, latency_ms, jitter_ms, packet_rate]):
            _rolling[lk][k].append(v)

    # ── Snapshot counter ──────────────────────────────────
    with ids_lock:
        atk_ctr = IDS_ATTACK_COUNTER
        rec_ctr = IDS_RECOVERY_COUNTER

    # ── Print box ─────────────────────────────────────────
    waktu = datetime.fromtimestamp(server_time).strftime("%H:%M:%S")
    print_ids_box(
        waktu, interval_ms, latency_ms, jitter_ms, packet_rate,
        pred_rf, pred_xgb, temp_status, final_st,
        atk_ctr, rec_ctr, device_id=device_id
    )

    # ── Async CSV write ───────────────────────────────────
    row = {
        "timestamp_unix"   : round(server_time, 3),
        "timestamp_esp"    : int(esp_ts),
        "device_id"        : str(device_id),
        "interval_ms"      : interval_ms,
        "latency_ms"       : latency_ms,
        "jitter_ms"        : jitter_ms,
        "packet_rate"      : round(packet_rate, 2),
        "label_aktual"     : current_label,
        "prediksi_rf"      : pred_rf,
        "prediksi_xgb"     : pred_xgb,
        "temporary_status" : temp_status,
        "final_status"     : final_st,
        "attack_counter"   : atk_ctr,
        "recovery_counter" : rec_ctr,
    }
    try:
        write_queue.put_nowait(row)
    except queue.Full:
        pass

    return jsonify({
        "status"           : "ok",
        "rf_detect"        : pred_rf,
        "xgb_detect"       : pred_xgb,
        "temporary_status" : temp_status,
        "final_status"     : final_st,
        "attack_counter"   : atk_ctr,
        "recovery_counter" : rec_ctr,
    }), 200

# ─── ENDPOINT /setlabel ──────────────────────────────────
@app.route("/setlabel", methods=["POST"])
def set_label():
    global current_label
    data      = request.get_json(silent=True)
    new_label = data.get("label", "normal").strip().lower() if data else "normal"
    if new_label not in ["normal", "dos"]:
        return jsonify({"error": "Label harus 'normal' atau 'dos'"}), 400
    current_label = new_label
    color = GR if new_label == "normal" else RE
    print(f"\n{color}{BO}{'═'*50}{R}")
    print(f"{color}{BO}  📌 LABEL MODE → {new_label.upper()}{R}")
    print(f"{color}{BO}{'═'*50}{R}\n")
    return jsonify({"status": "ok", "label": current_label}), 200

# ─── ENDPOINT /status ────────────────────────────────────
@app.route("/status", methods=["GET"])
def status():
    with _counters_lock:
        total  = _counters["total"]
        normal = _counters["normal"]
        dos    = _counters["dos"]
    with ids_lock:
        ids_state = IDS_STATE
        atk_ctr   = IDS_ATTACK_COUNTER
        rec_ctr   = IDS_RECOVERY_COUNTER
    return jsonify({
        "server"           : "running",
        "ml_ready"         : ML_READY,
        "current_label"    : current_label,
        "total_data"       : total,
        "normal"           : normal,
        "dos"              : dos,
        "queue_pending"    : write_queue.qsize(),
        "ids_state"        : ids_state,
        "attack_counter"   : atk_ctr,
        "recovery_counter" : rec_ctr,
        "csv_file"         : CSV_FILE,
    }), 200

# ─── MAIN ────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"{CY}{'═'*60}{R}")
    print(f"{BO}  🛡️  EDGE IDS SERVER v6.1 — RF + XGBoost + State Machine{R}")
    print(f"  Port            : {PORT}")
    print(f"  Log file        : {CSV_FILE}")
    print(f"  ML Status       : {f'{GR}READY{R}' if ML_READY else f'{RE}NOT READY{R}'}")
    print(f"  Attack threshold: {ATTACK_CONFIRM_THRESHOLD} hit berturut-turut")
    print(f"  Recovery thresh : {RECOVERY_NORMAL_THRESHOLD} normal berturut-turut")
    print(f"  Fitur           : INTERVAL | LATENCY | JITTER | PACKET RATE")
    print(f"{CY}{'═'*60}{R}\n")

    try:
        from waitress import serve
        print(f"{GR}[Waitress] Listening 0.0.0.0:{PORT} — 4 threads{R}")
        print(f"{GR}[Waitress] Menunggu data dari ESP32...{R}\n")
        serve(app, host="0.0.0.0", port=PORT, threads=4, _quiet=True)
    except ImportError:
        print(f"{YE}[WARN] pip install waitress — pakai Flask dev server.{R}\n")
        import logging as _log
        _log.getLogger("werkzeug").setLevel(_log.ERROR)
        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
