/*
  gateway.ino v4 — Fix Serial read non-blocking + dedup
*/

#include <SPI.h>
#include <LoRa.h>

#define LORA_SS    5
#define LORA_RST   14
#define LORA_DIO0  2

#define LORA_FREQ  433E6
#define LORA_SF    9
#define LORA_BW    125E3
#define LORA_CR    5
#define LORA_SW    0x12

#define DEDUP_WINDOW_MS 3000
#define DEDUP_LIST_SIZE 20

struct SeenEntry { uint32_t uid; uint32_t ts; };
SeenEntry seenList[DEDUP_LIST_SIZE];
int seenCount = 0;

// ── Non-blocking Serial buffer ────────────────────────────────
String serialBuf = "";


uint16_t crc16(uint8_t *data, uint8_t len) {
  uint16_t crc = 0xFFFF;
  for (uint8_t i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (uint8_t j = 0; j < 8; j++)
      crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : crc << 1;
  }
  return crc;
}

bool isValidHex(String s, uint8_t len) {
  if ((uint8_t)s.length() != len) return false;
  for (uint8_t i = 0; i < len; i++) {
    char c = s[i];
    if (!((c>='0'&&c<='9')||(c>='A'&&c<='F')||(c>='a'&&c<='f')))
      return false;
  }
  return true;
}

uint32_t uidToUint32(uint8_t *uid) {
  return ((uint32_t)uid[0]<<24)|((uint32_t)uid[1]<<16)|
         ((uint32_t)uid[2]<<8)|(uint32_t)uid[3];
}

bool checkAndAddSeen(uint32_t uid) {
  uint32_t now = millis();
  int newCount = 0;
  for (int i = 0; i < seenCount; i++)
    if (now - seenList[i].ts < DEDUP_WINDOW_MS)
      seenList[newCount++] = seenList[i];
  seenCount = newCount;

  for (int i = 0; i < seenCount; i++)
    if (seenList[i].uid == uid) return true;

  if (seenCount < DEDUP_LIST_SIZE)
    seenList[seenCount++] = {uid, now};
  else {
    for (int i = 0; i < DEDUP_LIST_SIZE-1; i++) seenList[i] = seenList[i+1];
    seenList[DEDUP_LIST_SIZE-1] = {uid, now};
  }
  return false;
}

void processSerialLine(String line) {
  line.trim();
  line.toUpperCase();

  if (!line.startsWith("RESP:")) return;

  String hex = line.substring(5);
  Serial.printf("# [DBG] RESP diterima: '%s' len:%d\n", hex.c_str(), hex.length());

  if (!isValidHex(hex, 24)) {
    Serial.printf("# [SKIP] RESP hex invalid\n");
    return;
  }

  uint8_t pkt[12];
  for (int i = 0; i < 12; i++)
    pkt[i] = (uint8_t)strtol(hex.substring(i*2,i*2+2).c_str(), NULL, 16);

  uint16_t crcRecv = ((uint16_t)pkt[10]<<8)|pkt[11];
  if (crc16(pkt, 10) != crcRecv) {
    Serial.println("# [SKIP] RESP CRC err");
    return;
  }

  // Kirim via LoRa ke gate
  LoRa.beginPacket();
  LoRa.print(hex);
  LoRa.endPacket();
  LoRa.receive();

  Serial.printf("# [TX→Gate] cmd:0x%02X gate:%d UID:%02X%02X%02X%02X\n",
    pkt[0], pkt[1], pkt[2], pkt[3], pkt[4], pkt[5]);
}


void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("# === Gateway v4 ===");

  LoRa.setPins(LORA_SS, LORA_RST, LORA_DIO0);
  if (!LoRa.begin(LORA_FREQ)) { Serial.println("# [ERROR] LoRa!"); while(1); }
  LoRa.setSpreadingFactor(LORA_SF);
  LoRa.setSignalBandwidth(LORA_BW);
  LoRa.setCodingRate4(LORA_CR);
  LoRa.setSyncWord(LORA_SW);
  LoRa.receive();

  Serial.printf("# [OK] Gateway v4 433MHz SF%d\n", LORA_SF);
}


void loop() {
  // ── 1. Non-blocking Serial read ──────────────────────────
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      if (serialBuf.length() > 0) {
        processSerialLine(serialBuf);
        serialBuf = "";
      }
    } else if (c != '\r') {
      serialBuf += c;
      if (serialBuf.length() > 60) serialBuf = ""; // overflow guard
    }
  }

  // ── 2. Cek LoRa packet masuk ─────────────────────────────
  int packetSize = LoRa.parsePacket();
  if (packetSize == 0) return;

  String payload = "";
  while (LoRa.available())
    payload += (char)LoRa.read();
  payload.trim();
  payload.toUpperCase();

  int   rssi = LoRa.packetRssi();
  float snr  = LoRa.packetSnr();

  if (!isValidHex(payload, 24)) return;

  uint8_t pkt[12];
  for (int i = 0; i < 12; i++)
    pkt[i] = (uint8_t)strtol(payload.substring(i*2,i*2+2).c_str(), NULL, 16);

  uint16_t crcRecv = ((uint16_t)pkt[10]<<8)|pkt[11];
  if (crc16(pkt, 10) != crcRecv) return;

  // Hanya proses TAP_REQUEST
  if (pkt[0] != 0x01) return;

  uint32_t uid32 = uidToUint32(&pkt[2]);
  if (checkAndAddSeen(uid32)) {
    Serial.printf("# [DEDUP] UID:%08X skip\n", uid32);
    return;
  }

  Serial.print("DATA:");
  Serial.print(payload);
  Serial.print(":");
  Serial.println(rssi);

  Serial.printf("# [RX←LoRa] gate:%d uid:%08X hop:%d rssi:%d\n",
    pkt[1], uid32, pkt[6], rssi);
}
