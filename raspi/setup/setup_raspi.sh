#!/bin/bash
# setup_raspi.sh — jalankan sekali di RPi
# Usage: bash setup_raspi.sh

echo "=== Gate System Setup — Raspberry Pi ==="
echo ""

# 1. Update & install Redis
echo "[1/4] Install Redis..."
sudo apt update -q
sudo apt install -y redis-server python3-pip python3-venv

# 2. Aktifkan Redis saat boot
echo "[2/4] Enable Redis service..."
sudo systemctl enable redis-server
sudo systemctl start redis-server

# Cek Redis jalan
if redis-cli ping | grep -q "PONG"; then
    echo "      ✓ Redis OK"
else
    echo "      ✗ Redis gagal start — cek: sudo systemctl status redis-server"
    exit 1
fi

# 3. Buat folder project
echo "[3/4] Setup project folder..."
mkdir -p ~/gate-system/receiver
mkdir -p ~/gate-system/mock
mkdir -p ~/gate-system/logs

# 4. Install Python deps
echo "[4/4] Install Python packages..."
pip3 install redis pyserial --break-system-packages

echo ""
echo "=== Setup selesai! ==="
echo ""
echo "Langkah selanjutnya:"
echo "  1. Copy file receiver/ ke ~/gate-system/receiver/"
echo "  2. Cek IP RPi:  hostname -I"
echo "  3. Test Redis:  redis-cli ping"
echo "  4. Run test:    cd ~/gate-system && python3 mock/test_packet.py"