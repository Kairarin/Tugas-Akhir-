/************************************************************
  ESP32 IDS v6.0 (v5 + NETWORK QUALITY MONITORING)
  Tambahan dari v5.0:
  [NEW] Rolling window 100 request untuk:
    - http_success_rate  : % request berhasil
    - communication_loss : % request gagal (app-level)
    - timeout_rate       : % request timeout
    - network_health     : indeks gabungan (0-100)
  Formula: 0.50 x success + 0.30 x (100-timeout) + 0.20 x (100-loss)
  Tidak diubah: semua kode v5 identik
************************************************************/

#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"

#include <WiFi.h>
#include <WiFiClient.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <DHT.h>

// === PIN & SENSOR ===
#define DHTPIN  4
#define DHTTYPE DHT11
DHT dht(DHTPIN, DHTTYPE);

// === WIFI ===
char ssid[] = "QuietQuarters";
char pass[] = "yuzarzidan89";

// === FLASK SERVER ===
const char* FLASK_URL  = "http://192.168.1.19:5000/data";
const char* HEALTH_URL = "http://192.168.1.19:5000/health";
const char* DEVICE_ID  = "ESP32-IDS-01";

// === HTTP SERVER ESP32 ===
WebServer espServer(80);

// === KONFIGURASI PENGUKURAN ===
#define PROBE_COUNT       3
#define PROBE_DELAY_MS    25
#define PROBE_TIMEOUT_MS  1500
#define POST_TIMEOUT_MS   2000

// === PACKET RATE COUNTER ===
volatile unsigned long pingCount = 0;

// === STATE GLOBAL ===
unsigned long lastSendTime = 0;
unsigned long nextDelay    = 500;

// === RESOURCE MONITORING ===
volatile unsigned long idleCountBaseline = 0;
volatile unsigned long idleCountCurrent  = 0;
volatile bool          measuringIdle     = false;
uint32_t totalHeapSize = 0;

#define DHT_READ_EVERY  5
int   dhtCycleCounter = 0;
float lastSuhu        = 0.0f;
float lastKelembaban  = 0.0f;

// === STRUCT HASIL PENGUKURAN JARINGAN ===
struct NetworkState {
  unsigned long latency_ms;
  unsigned long jitter_ms;
  bool          valid;
};


// =============================================================
// [TAMBAHAN v6] ROLLING WINDOW NETWORK QUALITY
// Ring buffer 100 request: 0=fail, 1=success, 2=timeout
// =============================================================
#define NET_WINDOW 100
static uint8_t netResults[NET_WINDOW];
static int     netHead  = 0;
static int     netCount = 0;

void netRecord(uint8_t result) {
  netResults[netHead] = result;
  netHead = (netHead + 1) % NET_WINDOW;
  if (netCount < NET_WINDOW) netCount++;
}

void calcNetQuality(float &successRate, float &commLoss,
                    float &timeoutRate, float &networkHealth) {
  if (netCount == 0) {
    successRate = 100.0f; commLoss = 0.0f;
    timeoutRate = 0.0f;   networkHealth = 100.0f;
    return;
  }
  int wins = 0, timeouts = 0;
  for (int i = 0; i < netCount; i++) {
    if (netResults[i] == 1) wins++;
    if (netResults[i] == 2) timeouts++;
  }
  float total   = (float)netCount;
  successRate   = roundf((wins / total) * 100.0f * 10.0f) / 10.0f;
  commLoss      = roundf(((netCount - wins) / total) * 100.0f * 10.0f) / 10.0f;
  timeoutRate   = roundf((timeouts / total) * 100.0f * 10.0f) / 10.0f;
  float health  = 0.50f * successRate
                + 0.30f * (100.0f - timeoutRate)
                + 0.20f * (100.0f - commLoss);
  if (health < 0.0f)   health = 0.0f;
  if (health > 100.0f) health = 100.0f;
  networkHealth = roundf(health * 10.0f) / 10.0f;
}
// =============================================================

// === HANDLER /ping === (identik v5)
void handlePing() {
  pingCount++;
  espServer.send(200, "text/plain", "pong");
}

void handleNotFound() {
  espServer.send(404, "text/plain", "Not Found");
}

// === measureNetworkState() === (identik v5)
NetworkState measureNetworkState() {
  unsigned long probes[PROBE_COUNT];
  int validCount = 0;

  for (int i = 0; i < PROBE_COUNT; i++) {
    HTTPClient hc;
    hc.begin(HEALTH_URL);
    hc.setTimeout(PROBE_TIMEOUT_MS);
    unsigned long t1   = millis();
    int           code = hc.GET();
    unsigned long t2   = millis();
    hc.end();
    if (code == 200) {
      probes[validCount++] = t2 - t1;
    }
    if (i < PROBE_COUNT - 1) {
      delay(PROBE_DELAY_MS);
    }
  }

  NetworkState result = {0, 0, false};
  if (validCount < 2) return result;

  unsigned long sumLatency = 0;
  for (int i = 0; i < validCount; i++) sumLatency += probes[i];
  result.latency_ms = sumLatency / validCount;

  unsigned long totalVariation = 0;
  for (int i = 1; i < validCount; i++) {
    unsigned long diff = (probes[i] > probes[i-1])
                         ? probes[i] - probes[i-1]
                         : probes[i-1] - probes[i];
    totalVariation += diff;
  }
  result.jitter_ms = totalVariation / (validCount - 1);
  result.valid     = true;
  return result;
}

// === postToFlask() === (DIMODIFIKASI v6: return bool + catat ke ring buffer)
bool postToFlask(const String& payload, bool &isTimeout) {
  isTimeout = false;

  // Percobaan pertama
  {
    HTTPClient http;
    http.begin(FLASK_URL);
    http.addHeader("Content-Type", "application/json");
    http.setTimeout(POST_TIMEOUT_MS);
    unsigned long t0   = millis();
    int           code = http.POST(payload);
    unsigned long dt   = millis() - t0;
    http.end();
    if (code == 200) return true;
    if (dt >= POST_TIMEOUT_MS) isTimeout = true;
  }

  // Retry 1x
  delay(100);
  {
    HTTPClient http;
    http.begin(FLASK_URL);
    http.addHeader("Content-Type", "application/json");
    http.setTimeout(POST_TIMEOUT_MS);
    unsigned long t0   = millis();
    int           code = http.POST(payload);
    unsigned long dt   = millis() - t0;
    http.end();
    if (code == 200) return true;
    if (dt >= POST_TIMEOUT_MS) isTimeout = true;
  }
  return false;
}

// === kirimKeFlask() === (identik v5 + 3 tambahan kecil untuk v6)
void kirimKeFlask() {
  unsigned long now         = millis();
  unsigned long interval_ms = (lastSendTime == 0) ? 0 : (now - lastSendTime);
  lastSendTime = now;

  // Baca DHT11 (identik v5)
  dhtCycleCounter++;
  if (dhtCycleCounter >= DHT_READ_EVERY) {
    float s = dht.readTemperature();
    float h = dht.readHumidity();
    if (!isnan(s)) lastSuhu       = s;
    if (!isnan(h)) lastKelembaban = h;
    dhtCycleCounter = 0;
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WiFi] Putus, skip siklus ini.");
    pingCount = 0;
    nextDelay = random(300, 800);
    return;
  }

  NetworkState ns       = measureNetworkState();
  unsigned long latency_ms = ns.valid ? ns.latency_ms : 9999;
  unsigned long jitter_ms  = ns.valid ? ns.jitter_ms  : 0;

  // Packet rate (identik v5)
  float packet_rate = 0.0f;
  if (interval_ms > 0) {
    packet_rate = ((float)pingCount / interval_ms) * 1000.0f;
    float background_noise = random(1, 36) / 10.0f;
    packet_rate += background_noise;
  }
  pingCount = 0;

  // Resource (identik v5)
  uint32_t freeHeap  = ESP.getFreeHeap();
  float heapUsagePct = 0.0f;
  if (totalHeapSize > 0) {
    heapUsagePct = (1.0f - ((float)freeHeap / (float)totalHeapSize)) * 100.0f;
  }
  float cpuLoadPct = 0.0f;
  if (idleCountBaseline > 0 && idleCountCurrent <= idleCountBaseline) {
    float idleRatio = (float)idleCountCurrent / (float)idleCountBaseline;
    cpuLoadPct = (1.0f - idleRatio) * 100.0f;
    if (cpuLoadPct < 0.0f)   cpuLoadPct = 0.0f;
    if (cpuLoadPct > 100.0f) cpuLoadPct = 100.0f;
  }

  // [TAMBAHAN v6] Hitung network quality SEBELUM POST
  float nq_success = 0.0f, nq_loss = 0.0f, nq_timeout = 0.0f, nq_health = 0.0f;
  calcNetQuality(nq_success, nq_loss, nq_timeout, nq_health);

  // JSON payload (identik v5 + 4 field baru di akhir)
  StaticJsonDocument<640> doc;
  doc["device_id"]   = DEVICE_ID;
  doc["suhu"]        = lastSuhu;
  doc["kelembaban"]  = lastKelembaban;
  doc["timestamp"]   = now;
  doc["interval_ms"] = interval_ms;
  doc["latency_ms"]  = latency_ms;
  doc["jitter_ms"]   = jitter_ms;
  doc["packet_rate"] = packet_rate;
  doc["free_heap"]   = freeHeap;
  doc["heap_usage"]  = round(heapUsagePct * 10.0f) / 10.0f;
  doc["cpu_load"]    = round(cpuLoadPct   * 10.0f) / 10.0f;
  // [TAMBAHAN v6] 4 field network quality
  doc["http_success_rate"]  = nq_success;
  doc["communication_loss"] = nq_loss;
  doc["timeout_rate"]       = nq_timeout;
  doc["network_health"]     = nq_health;

  String payload;
  serializeJson(doc, payload);

  // [TAMBAHAN v6] Kirim dan catat hasilnya ke ring buffer
  bool isTimeout = false;
  bool postOK    = postToFlask(payload, isTimeout);
  if (postOK)        netRecord(1);
  else if (isTimeout) netRecord(2);
  else               netRecord(0);

  // Log Serial (identik v5 + tambah succ & health)
  if (postOK) {
    Serial.printf(
      "[OK] interval: %lu ms | latency: %lu ms | jitter: %lu ms | pps: %.1f | heap: %.1f%% | cpu: %.1f%% | succ: %.1f%% | health: %.1f%%\n",
      interval_ms, latency_ms, jitter_ms, packet_rate, heapUsagePct, cpuLoadPct,
      nq_success, nq_health
    );
  } else {
    Serial.printf(
      "[FAIL] interval: %lu ms | latency: %lu ms | pps: %.1f | heap: %.1f%% | succ: %.1f%% | health: %.1f%%\n",
      interval_ms, latency_ms, packet_rate, heapUsagePct,
      nq_success, nq_health
    );
  }

  nextDelay = random(300, 1001);
}

// === SETUP === (identik v5)
void setup() {
  WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0);

  Serial.begin(115200);
  delay(500);

  randomSeed(analogRead(0));
  dht.begin();

  Serial.println("\n====================================");
  Serial.println("  ESP32 IDS v6.0 (NETWORK QUALITY)");
  Serial.println("====================================");
  Serial.printf("Flask  : %s\n", FLASK_URL);
  Serial.printf("Health : %s\n", HEALTH_URL);
  Serial.println("------------------------------------");

  Serial.printf("WiFi   : Connecting to %s ...\n", ssid);
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, pass);

  IPAddress local_IP(192,168,1,76);
  IPAddress gateway(192,168,1,1);
  IPAddress subnet(255,255,255,0);
  IPAddress primaryDNS(8,8,8,8);
  IPAddress secondaryDNS(1,1,1,1);
  if (!WiFi.config(local_IP, gateway, subnet, primaryDNS, secondaryDNS)) {
    Serial.println("Static IP Failed");
  }

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500);
    Serial.print(".");
    attempts++;
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("\n[ERROR] Gagal konek WiFi! Restart...");
    delay(3000);
    ESP.restart();
  }

  Serial.printf("\nESP32 IP : %s\n", WiFi.localIP().toString().c_str());
  Serial.println("WiFi     : Connected");

  espServer.on("/ping", HTTP_GET, handlePing);
  espServer.onNotFound(handleNotFound);
  espServer.begin();
  Serial.println("WebServer: Ready at /ping (DoS target)");
  Serial.println("====================================\n");

  delay(2000);
  lastSuhu       = dht.readTemperature();
  lastKelembaban = dht.readHumidity();
  if (isnan(lastSuhu))       lastSuhu       = 0.0f;
  if (isnan(lastKelembaban)) lastKelembaban = 0.0f;

  lastSendTime = 0;
  nextDelay    = random(300, 1001);

  // [TAMBAHAN v6] Init ring buffer
  memset(netResults, 1, sizeof(netResults));
  netHead  = 0;
  netCount = 0;

  // Resource baseline (identik v5)
  totalHeapSize = ESP.getHeapSize();
  Serial.printf("Total Heap   : %u bytes\n", totalHeapSize);
  Serial.printf("Free Heap    : %u bytes\n", ESP.getFreeHeap());
  Serial.println("Mengukur CPU baseline (500ms)...");
  unsigned long baselineStart = millis();
  unsigned long baselineCount = 0;
  while (millis() - baselineStart < 500) {
    espServer.handleClient();
    baselineCount++;
  }
  idleCountBaseline = baselineCount / 5;
  if (idleCountBaseline == 0) idleCountBaseline = 1;
  Serial.printf("CPU Baseline : %lu iter/100ms\n", idleCountBaseline);
  Serial.println("====================================\n");
}

// === LOOP === (identik v5)
static unsigned long idleWindowStart = 0;
static unsigned long idleCounter     = 0;

void loop() {
  espServer.handleClient();

  idleCounter++;
  unsigned long nowMs = millis();
  if (nowMs - idleWindowStart >= 100) {
    idleCountCurrent = idleCounter;
    idleCounter      = 0;
    idleWindowStart  = nowMs;
  }

  if (nowMs - lastSendTime >= nextDelay) {
    kirimKeFlask();
  }
}
