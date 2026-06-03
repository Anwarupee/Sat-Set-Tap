/*
  registration_reader.ino v3
  Arduino Uno + 1x PN532 (I2C)

  PN532 — I2C:
    DIP Switch: SW1=ON, SW2=OFF
    SDA → A4
    SCL → A5
    VCC → 3.3V, GND → GND

  LED feedback:
    LED Hijau → D8 (terdaftar baru)
    LED Merah → D9 (sudah ada / error)

  Output Serial ke RPi (9600 baud):
    UID:<hex8char>\n  ← kartu terdeteksi
    READY\n           ← siap
*/

#include <Wire.h>
#include <Adafruit_PN532.h>

// ── PN532 via I2C (SDA=A4, SCL=A5) ────────────────────────────
Adafruit_PN532 nfc;

// ── LED ───────────────────────────────────────────────────────
#define LED_OK  8
#define LED_ERR 9

bool nfc_ready = false;

#define SCAN_COOLDOWN 2000
unsigned long lastScan = 0;


String uidToHex(uint8_t *uid, uint8_t len) {
  String hex = "";
  for (uint8_t i = 0; i < min(len, (uint8_t)4); i++) {
    if (uid[i] < 0x10) hex += "0";
    hex += String(uid[i], HEX);
  }
  hex.toUpperCase();
  return hex;
}

void ledOK() {
  digitalWrite(LED_OK, HIGH); delay(400); digitalWrite(LED_OK, LOW);
}

void ledERR() {
  for (int i = 0; i < 3; i++) {
    digitalWrite(LED_ERR, HIGH); delay(120);
    digitalWrite(LED_ERR, LOW);  delay(120);
  }
}

void waitResponse() {
  unsigned long start = millis();
  while (millis() - start < 2000) {
    if (Serial.available()) {
      String resp = Serial.readStringUntil('\n');
      resp.trim();
      if (resp == "OK")  { ledOK();  return; }
      if (resp == "DUP") { ledERR(); return; }
    }
  }
  ledERR();  // timeout
}

void scanAndSend(Adafruit_PN532 &nfc, unsigned long &lastScan, const char* label) {
  if (millis() - lastScan < SCAN_COOLDOWN) return;

  uint8_t uid[7] = {0};
  uint8_t uidLen = 0;

  if (nfc.readPassiveTargetID(PN532_MIFARE_ISO14443A, uid, &uidLen, 100)) {
    String hex = uidToHex(uid, uidLen);
    
    // Kirim data utama ke RPi
    Serial.print("UID:");
    Serial.println(hex);
    Serial.flush();
    
    lastScan = millis();

    // Debugging output (Sudah diperbaiki agar tidak error di Uno)
    Serial.print("# [");
    Serial.print(label);
    Serial.print("] UID: ");
    Serial.println(hex);

    waitResponse();
  }
}


void setup() {
  Serial.begin(9600);
  delay(500);

  pinMode(LED_OK,  OUTPUT);
  pinMode(LED_ERR, OUTPUT);

  // Test LED
  digitalWrite(LED_OK,  HIGH); delay(200); digitalWrite(LED_OK,  LOW);
  digitalWrite(LED_ERR, HIGH); delay(200); digitalWrite(LED_ERR, LOW);

  // Init PN532 (I2C)
  nfc.begin();
  if (nfc.getFirmwareVersion()) {
    nfc.SAMConfig();
    nfc_ready = true;
    Serial.println("# PN532 OK (I2C)");
  } else {
    Serial.println("# PN532 GAGAL — cek DIP switch SW1=ON SW2=OFF");
  }

  Serial.println("READY");
}


void loop() {
  if (nfc_ready) scanAndSend(nfc, lastScan, "Reader");
}
