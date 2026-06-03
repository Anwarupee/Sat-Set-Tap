/*
  gate_node.ino v4 — ESP32 + LoRa + PN532 + Servo SG90

  Alur:
    1. Tap KTP → kirim TAP_REQUEST via LoRa
    2. Tunggu RESP_OK atau RESP_DENY dari RPi (via Gateway)
    3. RESP_OK  → servo buka 3 detik lalu tutup otomatis
    4. RESP_DENY → LED merah sebentar, gate tetap tutup

  Wiring Servo SG90:
    Merah  → 5V (VIN ESP32, BUKAN 3.3V)
    Coklat → GND
    Kuning → GPIO 13

  Wiring LoRa & PN532 sama seperti sebelumnya.
*/

#include <SPI.h>
#include <LoRa.h>
#include <Wire.h>
#include <Adafruit_PN532.h>
#include <ESP32Servo.h>   // Library Manager: cari "ESP32Servo"

// ── Pin LoRa ──────────────────────────────────────────────────
#define LORA_SS    5
#define LORA_RST   14
#define LORA_DIO0  2

// ── Pin PN532 I2C ─────────────────────────────────────────────
#define SDA_PIN 21
#define SCL_PIN 22

// ── Pin Servo & LED ───────────────────────────────────────────
#define SERVO_PIN     13
#define SERVO_OPEN    90    // derajat posisi buka
#define SERVO_CLOSE   0     // derajat posisi tutup
#define SERVO_OPEN_MS 3000  // buka 3 detik
#define LED_GREEN     25
#define LED_RED       26

// ── Konfigurasi ───────────────────────────────────────────────
#define GATE_ID       1
#define LORA_FREQ     433E6
#define LORA_SF       9
#define LORA_BW       125E3
#define LORA_CR       5
#define LORA_SW       0x12
#define MAX_HOP       3
#define RESP_TIMEOUT  5000  // max tunggu response: 5 detik
#define MAX_RETRY     2     // maks retry kalau timeout

// ── Command codes ─────────────────────────────────────────────
#define CMD_TAP_REQ   0x01
#define CMD_RESP_OK   0x02
#define CMD_RESP_DENY 0x03
#define CMD_LOCKDOWN  0x05  // RPi: kunci gate
#define CMD_UNLOCK    0x06  // RPi: buka kunci
#define CMD_HEARTBEAT 0x07  // Gate -> RPi: heartbeat

#define HEARTBEAT_INTERVAL 30000  // 30 detik

Adafruit_PN532 nfc(SDA_PIN, SCL_PIN);
Servo gateServo;

bool waitingResponse = false;
bool isLocked         = false;  // true = gate sedang lockdown
unsigned long tapSentAt = 0;
unsigned long lastHeartbeat = 0;
int  retryCount  = 0;
String lastPacket = "";


uint16_t crc16(uint8_t *data, uint8_t len) {
  uint16_t crc = 0xFFFF;
  for (uint8_t i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (uint8_t j = 0; j < 8; j++)
      crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : crc << 1;
  }
  return crc;
}


String buildHeartbeatPacket() {
  // Format: CMD + GATE_ID + isLocked + 00 + 0000 + CRC
  uint8_t pkt[12] = {0};
  pkt[0] = CMD_HEARTBEAT;
  pkt[1] = GATE_ID;
  pkt[2] = isLocked ? 0x01 : 0x00;  // status gate
  // byte 3-5 padding 0x00
  pkt[6] = 0x00;  // hop count = 0 (heartbeat tidak di-relay)
  pkt[7] = 0x00;
  uint16_t ts = (millis() / 1000) % 65535;
  pkt[8]  = (ts >> 8) & 0xFF;
  pkt[9]  =  ts & 0xFF;
  uint16_t crc = crc16(pkt, 10);
  pkt[10] = (crc >> 8) & 0xFF;
  pkt[11] =  crc & 0xFF;
  String hex = "";
  for (uint8_t i = 0; i < 12; i++) {
    if (pkt[i] < 0x10) hex += "0";
    hex += String(pkt[i], HEX);
  }
  hex.toUpperCase();
  return hex;
}

String buildTapPacket(uint8_t *uid, uint8_t uidLen) {
  uint8_t pkt[12] = {0};
  pkt[0] = CMD_TAP_REQ;
  pkt[1] = GATE_ID;
  for (uint8_t i = 0; i < 4; i++)
    pkt[2+i] = (i < uidLen) ? uid[i] : 0x00;
  pkt[6] = MAX_HOP;
  pkt[7] = 0x00;
  uint16_t ts = (millis() / 1000) % 65535;
  pkt[8]  = (ts >> 8) & 0xFF;
  pkt[9]  =  ts & 0xFF;
  uint16_t crc = crc16(pkt, 10);
  pkt[10] = (crc >> 8) & 0xFF;
  pkt[11] =  crc & 0xFF;
  String hex = "";
  for (uint8_t i = 0; i < 12; i++) {
    if (pkt[i] < 0x10) hex += "0";
    hex += String(pkt[i], HEX);
  }
  hex.toUpperCase();
  return hex;
}


void openGate() {
  Serial.println("[SERVO] Buka — 3 detik");
  gateServo.write(SERVO_OPEN);
  digitalWrite(LED_GREEN, HIGH);
  digitalWrite(LED_RED,   LOW);
  delay(SERVO_OPEN_MS);
  gateServo.write(SERVO_CLOSE);
  digitalWrite(LED_GREEN, LOW);
  Serial.println("[SERVO] Tutup");
}

void lockdownFeedback() {
  // LED merah nyala terus saat lockdown
  digitalWrite(LED_RED, HIGH);
  gateServo.write(SERVO_CLOSE);
  Serial.println("[LOCK] Gate dalam mode LOCKDOWN");
}

void unlockFeedback() {
  digitalWrite(LED_RED, LOW);
  Serial.println("[LOCK] Gate dibuka kembali");
}

void denyGate() {
  Serial.println("[DENY] Akses ditolak");
  for (int i = 0; i < 3; i++) {
    digitalWrite(LED_RED, HIGH); delay(200);
    digitalWrite(LED_RED, LOW);  delay(200);
  }
}


void checkLoRaResponse() {
  int packetSize = LoRa.parsePacket();
  if (packetSize == 0) return;

  String payload = "";
  while (LoRa.available())
    payload += (char)LoRa.read();
  payload.trim();
  payload.toUpperCase();

  if (payload.length() != 24) return;

  uint8_t pkt[12];
  for (int i = 0; i < 12; i++)
    pkt[i] = (uint8_t)strtol(payload.substring(i*2, i*2+2).c_str(), NULL, 16);

  uint16_t crcRecv = ((uint16_t)pkt[10] << 8) | pkt[11];
  if (crc16(pkt, 10) != crcRecv) { Serial.println("[RX] CRC err"); return; }

  uint8_t cmd    = pkt[0];
  uint8_t gateId = pkt[1];

  // Abaikan packet yang bukan untuk gate ini
  if (gateId != GATE_ID) return;

  Serial.printf("[RX] cmd=0x%02X gate=%d\n", cmd, gateId);

  unsigned long latency = millis() - tapSentAt;
  Serial.printf("[LATENCY] gate=%d ms=%lu\n", GATE_ID, latency);
  if      (cmd == CMD_RESP_OK)   { waitingResponse = false; isLocked = false; lastPacket = ""; retryCount = 0; openGate(); }
  else if (cmd == CMD_RESP_DENY) { waitingResponse = false; lastPacket = ""; retryCount = 0; denyGate(); }
  else if (cmd == CMD_LOCKDOWN)  { isLocked = true;  lockdownFeedback(); }
  else if (cmd == CMD_UNLOCK)    { isLocked = false; unlockFeedback(); }
}


void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== Gate Node v4 ===");

  pinMode(LED_GREEN, OUTPUT);
  pinMode(LED_RED,   OUTPUT);

  gateServo.attach(SERVO_PIN);
  gateServo.write(SERVO_CLOSE);
  delay(300);
  Serial.println("[OK] Servo ready");

  LoRa.setPins(LORA_SS, LORA_RST, LORA_DIO0);
  if (!LoRa.begin(LORA_FREQ)) { Serial.println("[ERROR] LoRa!"); while(1); }
  LoRa.setSpreadingFactor(LORA_SF);
  LoRa.setSignalBandwidth(LORA_BW);
  LoRa.setCodingRate4(LORA_CR);
  LoRa.setSyncWord(LORA_SW);
  LoRa.receive();
  Serial.printf("[OK] LoRa 433MHz SF%d\n", LORA_SF);

  nfc.begin();
  if (!nfc.getFirmwareVersion()) { Serial.println("[ERROR] PN532!"); while(1); }
  nfc.SAMConfig();
  lastHeartbeat = millis();
  Serial.printf("[OK] Gate %d siap — tempelkan kartu...\n\n", GATE_ID);
}


void loop() {
  // ── Heartbeat tiap 30 detik ──────────────────────────────
  if (millis() - lastHeartbeat >= HEARTBEAT_INTERVAL) {
    String hb = buildHeartbeatPacket();
    Serial.printf("[HB] %s\n", hb.c_str());
    LoRa.beginPacket();
    LoRa.print(hb);
    LoRa.endPacket();
    LoRa.receive();
    lastHeartbeat = millis();
  }

  // Timeout: retry atau deny kalau habis percobaan
  if (waitingResponse && (millis() - tapSentAt > RESP_TIMEOUT)) {
    if (retryCount < MAX_RETRY && lastPacket.length() == 24) {
      retryCount++;
      unsigned long backoff = random(100, 301) + (random(0, 200) * retryCount);
      Serial.printf("[RETRY %d/%d] Backoff %lums\n", retryCount, MAX_RETRY, backoff);
      delay(backoff);
      Serial.printf("[RETX] %s\n", lastPacket.c_str());
      LoRa.beginPacket();
      LoRa.print(lastPacket);
      LoRa.endPacket();
      LoRa.receive();
      tapSentAt = millis();
      Serial.println("[WAIT] Retry — menunggu response...");
    } else {
      Serial.println("[TIMEOUT] Tidak ada response");
      waitingResponse = false;
      lastPacket = "";
      retryCount = 0;
      denyGate();
    }
  }

  // Selagi nunggu response, cek LoRa terus
  if (waitingResponse) {
    checkLoRaResponse();
    return;
  }

  // Kalau gate sedang lockdown, tolak semua tap
  if (isLocked) {
    // Tetap dengerin LoRa untuk CMD_UNLOCK
    checkLoRaResponse();
    delay(100);
    return;
  }

  // Scan kartu (non-blocking, timeout 500ms)
  checkLoRaResponse();   // Cek LoRa sebelum scan NFC — tangkap LOCKDOWN/UNLOCK
  uint8_t uid[7] = {0};
  uint8_t uidLen = 0;
  if (!nfc.readPassiveTargetID(PN532_MIFARE_ISO14443A, uid, &uidLen, 500))
    return;

  // Kirim TAP_REQUEST — random jitter agar tidak tabrakan dengan gate lain
  String packet = buildTapPacket(uid, uidLen);
  delay(random(10, 81));
  Serial.printf("[TX] %s\n", packet.c_str());

  LoRa.beginPacket();
  LoRa.print(packet);
  LoRa.endPacket();
  LoRa.receive();

  lastPacket    = packet;
  waitingResponse = true;
  tapSentAt       = millis();
  retryCount      = 0;
  Serial.println("[WAIT] Menunggu response RPi...");
}
