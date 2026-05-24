"""
debug_lora.py — Script debug, jalankan di RPi.
Akan print SEMUA sinyal yang diterima LoRa, termasuk yang corrupt.

Usage:
  cd ~/folder
  python3 debug_lora.py

Sambil jalan, minta temanmu tap kartu di ESP32.
Kalau ada sinyal masuk, pasti muncul di sini.
"""

import sys
import os
import time

# Path setup
_HERE    = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "receiver"))
sys.path.insert(0, _HERE)

try:
    from SX127x.LoRa import LoRa
    from SX127x.board_config import BOARD
except ImportError:
    print("ERROR: SX127x tidak ditemukan di", _HERE)
    sys.exit(1)

# ── Samakan parameter dengan ESP32 ───────────────────────────
FREQ      = 433      # MHz
SF        = 7
BW_INDEX  = 6        # index 6 = 125 kHz
CR        = 5        # 4/5
SYNCWORD  = 0x12

print(f"=== LoRa Debug Receiver ===")
print(f"Freq: {FREQ} MHz | SF{SF} | BW125kHz | CR4/{CR} | SW:0x{SYNCWORD:02X}")
print(f"Menunggu sinyal... (Ctrl+C untuk berhenti)")

BOARD.setup()
BOARD.reset()


class DebugLoRa(LoRa):
    def __init__(self):
        super().__init__(verbose=False)

        self.set_pa_config(pa_select=1)
        self.set_freq(FREQ)
        self.set_spreading_factor(SF)
        self.set_bw(BW_INDEX)
        self.set_coding_rate(CR)
        self.set_sync_word(SYNCWORD)
        self.set_rx_crc(False)   # False dulu biar terima apapun meski CRC error
        self.set_mode(0x85)      # RXCONT

        print(f"[OK] LoRa init selesai")
        print(f"     Freq aktual : {self.get_freq()} MHz")
        print(f"     RSSI awal   : {self.get_rssi_value()} dBm")
        print(f"     Mode        : {self.get_mode()}")
        print()

    def on_rx_done(self):
        rssi    = self.get_pkt_rssi_value()
        snr     = self.get_pkt_snr_value()
        payload = self.read_payload(nocheck=True)
        raw     = bytes(payload)

        print(f"[RX] Sinyal diterima!")
        print(f"     RSSI    : {rssi} dBm")
        print(f"     SNR     : {snr} dB")
        print(f"     Length  : {len(raw)} bytes")
        print(f"     Raw hex : {raw.hex().upper()}")

        try:
            decoded = raw.decode("ascii", errors="replace").strip()
            print(f"     ASCII   : '{decoded}'")
        except Exception:
            pass

        print()
        self.set_mode(0x85)  # kembali RXCONT

    def on_crc_error(self):
        rssi = self.get_pkt_rssi_value()
        print(f"[CRC ERR] Ada sinyal tapi CRC error — RSSI: {rssi} dBm")
        print(f"          Kemungkinan: frekuensi hampir cocok tapi ada beda parameter")
        print()
        self.set_mode(0x85)

    def on_valid_header(self):
        print(f"[HEADER] Header valid diterima — packet sedang masuk...")


lora = DebugLoRa()

try:
    while True:
        time.sleep(0.05)
except KeyboardInterrupt:
    print("\nDebug berhenti.")
finally:
    BOARD.teardown()
