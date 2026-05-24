/*
  relay_node.ino v3 — ESP32 + LoRa (tanpa PN532)

  Fungsi:
    1. Dengerin packet dari gate via LoRa
    2. Cek seen list — sudah pernah di-forward? skip
    3. Kurangi hop_count, hitung ulang CRC
    4. Forward ke LoRa berikutnya
    5. Auto flush seen list setiap FLUSH_INTERVAL_MS (10 menit)

  Failover:
    Dua relay dipasang di posisi yang sama.
    Keduanya dengerin dan forward secara independen.
    Gateway akan terima dari relay manapun yang sampai duluan.
    Duplikat di-handle oleh gateway dan RPi.

  Wiring: sama dengan gate_node (LoRa saja, tidak perlu PN532)
*/

#include <SPI.h>
#include <LoRa.h>

#define LORA_SS    5
#define LORA_RST   14
#define LORA_DIO0  2

// ── Konfigurasi — HARUS sama dengan gate_node dan gateway ────
#define LORA_FREQ  433E6
#define LORA_SF    9
#define LORA_BW    125E3
#define LORA_CR    5
#define LORA_SW    0x12

// ── Konfigurasi — UBAH SESUAI NODE ───────────────────────────
// Relay Node : RELAY_ID = 1 (Relay A) atau 2 (Relay B)
#define RELAY_ID          1               // Ganti: Relay A=1, Relay B=2
#define FLUSH_INTERVAL_MS (10 * 60000UL) // 10 menit dalam ms
#define SEEN_LIST_SIZE    50              // max UID di seen list
#define TX_DELAY_MS       30             // jeda sebelum forward (anti-collision)
                                          // Relay A=50ms, Relay B=100ms

// ── Seen list — simpan UID yang sudah di-forward ──────────────
// Key: UID 4 bytes (uint32), Value: timestamp forward
struct SeenEntry {
  uint32_t uid;
  uint32_t ts;
};

SeenEntry seenList[SEEN_LIST_SIZE];
int seenCount = 0;
unsigned long lastFlush = 0;


// ── CRC-16 — sama persis dengan gate_node ────────────────────
uint16_t crc16(uint8_t *data, uint8_t len) {
  uint16_t crc = 0xFFFF;
  for (uint8_t i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (uint8_t j = 0; j < 8; j++)
      crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : crc << 1;
  }
  return crc;
}


// ── Helper: hex string -> bytes ───────────────────────────────
bool hexToBytes(String hex, uint8_t *out, uint8_t len) {
  if (hex.length() != len * 2) return false;
  for (uint8_t i = 0; i < len; i++) {
    String byteStr = hex.substring(i*2, i*2+2);
    // Validasi karakter hex
    for (int j = 0; j < 2; j++) {
      char c = byteStr[j];
      if (!((c>='0'&&c<='9')||(c>='A'&&c<='F')||(c>='a'&&c<='f')))
        return false;
    }
    out[i] = (uint8_t)strtol(byteStr.c_str(), NULL, 16);
  }
  return true;
}


// ── Seen list management ──────────────────────────────────────
uint32_t uidToUint32(uint8_t *uid) {
  return ((uint32_t)uid[0] << 24) | ((uint32_t)uid[1] << 16) |
         ((uint32_t)uid[2] << 8)  |  (uint32_t)uid[3];
}

bool isInSeenList(uint32_t uid) {
  for (int i = 0; i < seenCount; i++)
    if (seenList[i].uid == uid) return true;
  return false;
}

void addToSeenList(uint32_t uid) {
  if (seenCount < SEEN_LIST_SIZE) {
    seenList[seenCount++] = {uid, (uint32_t)millis()};
  } else {
    // List penuh — geser (FIFO, buang yang paling lama)
    for (int i = 0; i < SEEN_LIST_SIZE - 1; i++)
      seenList[i] = seenList[i+1];
    seenList[SEEN_LIST_SIZE-1] = {uid, (uint32_t)millis()};
  }
}

void flushSeenList() {
  Serial.printf("[FLUSH] Seen list dikosongkan (%d entries)\n", seenCount);
  seenCount = 0;
  memset(seenList, 0, sizeof(seenList));
  lastFlush = millis();
}


// ── Build packet dengan hop_count dikurangi 1 ────────────────
String decrementHopAndRebuild(uint8_t *pkt) {
  // Kurangi hop count
  pkt[6] = pkt[6] - 1;
  // Hitung ulang CRC dari byte 0-9
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


void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.printf("\n=== Relay Node v3 (ID: %d) ===\n", RELAY_ID);
  Serial.printf("Flush interval: 10 menit | TX delay: %dms\n\n", TX_DELAY_MS);

  LoRa.setPins(LORA_SS, LORA_RST, LORA_DIO0);
  if (!LoRa.begin(LORA_FREQ)) { Serial.println("[ERROR] LoRa!"); while(1); }
  LoRa.setSpreadingFactor(LORA_SF);
  LoRa.setSignalBandwidth(LORA_BW);
  LoRa.setCodingRate4(LORA_CR);
  LoRa.setSyncWord(LORA_SW);

  Serial.printf("[OK] LoRa 433MHz SF%d SW:0x%02X\n", LORA_SF, LORA_SW);
  Serial.println("[OK] Menunggu packet dari gate...\n");

  lastFlush = millis();
}


void loop() {
  // ── Auto flush seen list setiap 10 menit ─────────────────
  if (millis() - lastFlush >= FLUSH_INTERVAL_MS) {
    flushSeenList();
  }

  // ── Cek packet masuk ──────────────────────────────────────
  int packetSize = LoRa.parsePacket();
  if (packetSize == 0) return;

  String payload = "";
  while (LoRa.available())
    payload += (char)LoRa.read();
  payload.trim();
  payload.toUpperCase();

  int rssi = LoRa.packetRssi();

  // ── Validasi format: harus tepat 24 char hex ─────────────
  if (payload.length() != 24) {
    Serial.printf("[SKIP] Bukan 24 char: '%s'\n", payload.c_str());
    return;
  }

  // Parse ke bytes
  uint8_t pkt[12];
  if (!hexToBytes(payload, pkt, 12)) {
    Serial.printf("[SKIP] Non-hex: '%s'\n", payload.c_str());
    return;
  }

  // ── Validasi CRC ──────────────────────────────────────────
  uint16_t crcRecv = ((uint16_t)pkt[10] << 8) | pkt[11];
  uint16_t crcCalc = crc16(pkt, 10);
  if (crcRecv != crcCalc) {
    Serial.printf("[SKIP] CRC mismatch — recv:0x%04X calc:0x%04X\n",
                  crcRecv, crcCalc);
    return;
  }

  // ── Cek hop count ─────────────────────────────────────────
  uint8_t hopCount = pkt[6];
  uint8_t gateId   = pkt[1];
  uint32_t uid32   = uidToUint32(&pkt[2]);

  if (hopCount == 0) {
    Serial.printf("[SKIP] Hop=0, packet dari gate %d UID:%08X tidak di-relay\n",
                  gateId, uid32);
    return;
  }

  // ── Cek seen list ─────────────────────────────────────────
  if (isInSeenList(uid32)) {
    Serial.printf("[SKIP] UID %08X sudah di-forward (seen list: %d entries)\n",
                  uid32, seenCount);
    return;
  }

  // ── Forward ───────────────────────────────────────────────
  addToSeenList(uid32);
  String forwardPkt = decrementHopAndRebuild(pkt);

  // Delay sebelum TX untuk anti-collision antar relay
  // Relay A (ID=1): 50ms, Relay B (ID=2): 100ms
  delay(TX_DELAY_MS * RELAY_ID);

  LoRa.beginPacket();
  LoRa.print(forwardPkt);
  LoRa.endPacket();

  Serial.printf("[FWD] Gate:%d UID:%08X hop:%d->%d rssi:%d dBm\n",
                gateId, uid32, hopCount, hopCount-1, rssi);
  Serial.printf("      %s -> %s\n", payload.c_str(), forwardPkt.c_str());

  // Kembali ke mode receive
  LoRa.receive();
}
