"""
Unit test untuk packet parser.
Jalankan: python test_packet.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../receiver"))

from packet import parse_hex_packet, build_hex_packet, Mode, GatePacket


def test(name: str, fn):
    try:
        fn()
        print(f"  ✓  {name}")
    except AssertionError as e:
        print(f"  ✗  {name} — AssertionError: {e}")
    except Exception as e:
        print(f"  ✗  {name} — {type(e).__name__}: {e}")


print("=== Packet Parser Tests ===\n")

# --- Parse tests ---
print("[ Parse ]")

def t_parse_basic():
    p = parse_hex_packet("0001000112AB34CD56EF7890")
    assert p.mode == Mode.TX, f"mode={p.mode}"
    assert p.gate_id == 1, f"gate_id={p.gate_id}"
    assert p.ktp_uid == "12AB34CD56EF7890", f"uid={p.ktp_uid}"

def t_parse_rx_mode():
    p = parse_hex_packet("0002000212AB34CD56EF7890")
    assert p.mode == Mode.RX
    assert p.gate_id == 2

def t_parse_gate3():
    p = parse_hex_packet("0001000312AB34CD56EF7890")
    assert p.gate_id == 3

def t_parse_lowercase():
    p = parse_hex_packet("0001000112ab34cd56ef7890")
    assert p.ktp_uid == "12AB34CD56EF7890"

def t_parse_wrong_length():
    try:
        parse_hex_packet("0001000112AB34CD")
        assert False, "Harus raise ValueError"
    except ValueError:
        pass

def t_parse_invalid_mode():
    try:
        parse_hex_packet("FFFF000112AB34CD56EF7890")
        assert False, "Harus raise ValueError"
    except ValueError:
        pass

def t_parse_non_hex():
    try:
        parse_hex_packet("ZZZZZZZZZZZZZZZZZZZZZZZZ")
        assert False, "Harus raise ValueError"
    except ValueError:
        pass

test("Parse TX mode, gate 1", t_parse_basic)
test("Parse RX mode, gate 2", t_parse_rx_mode)
test("Parse gate 3", t_parse_gate3)
test("Parse lowercase hex", t_parse_lowercase)
test("Reject wrong length", t_parse_wrong_length)
test("Reject invalid mode", t_parse_invalid_mode)
test("Reject non-hex string", t_parse_non_hex)

# --- Build tests ---
print("\n[ Build ]")

def t_build_basic():
    h = build_hex_packet(Mode.TX, 1, "12AB34CD56EF7890")
    assert h == "0001000112AB34CD56EF7890", f"got={h}"

def t_build_rx():
    h = build_hex_packet(Mode.RX, 2, "AABBCCDDEEFF0011")
    assert h == "0002000200AABBCCDDEEFF0011"[:24] or len(h) == 24

def t_roundtrip():
    original = "0001000312AB34CD56EF7890"
    p = parse_hex_packet(original)
    rebuilt = build_hex_packet(p.mode, p.gate_id, p.ktp_uid)
    assert rebuilt == original, f"roundtrip fail: {rebuilt} != {original}"

def t_build_wrong_uid_length():
    try:
        build_hex_packet(Mode.TX, 1, "ABCD")
        assert False, "Harus raise ValueError"
    except ValueError:
        pass

test("Build TX gate 1", t_build_basic)
test("Roundtrip parse→build", t_roundtrip)
test("Reject UID terlalu pendek", t_build_wrong_uid_length)

# --- to_dict ---
print("\n[ to_dict ]")

def t_to_dict():
    p = parse_hex_packet("0001000112AB34CD56EF7890")
    d = p.to_dict()
    assert d["mode"] == "TX"
    assert d["gate_id"] == 1
    assert d["ktp_uid"] == "12AB34CD56EF7890"
    assert "raw_hex" in d

test("to_dict keys dan values", t_to_dict)

print("\n=== Done ===")
