"""RF file uplink verified by the FileUplink FileReceived EVR.

Covers single-chunk probes and a multi-chunk transfer that exceeds the RFM69
255-byte Space Packet MTU. FileReceived is only emitted when the end-of-file
checksum matches, so it confirms both delivery and integrity.
"""

from pathlib import Path

from soak_helpers import (
    FSW_TMP,
    UPLINK_LARGE_TIMEOUT_S,
    UPLINK_TIMEOUT_S,
    rf_uplink,
)

INT_DIR = Path(__file__).parent.resolve()
# Multi-chunk RF uplink well above the 255-byte RFM69 Space Packet MTU.
# 10 KiB ≈ 103 DATA chunks at 100 B plus START/END (~73 s at 0.70 s cooldown).
LARGE_UPLINK_BYTES = 10 * 1024


def test_file_uplink_small_probe(fprime_test_api):
    """Uplink a small text probe; verify via FileReceived."""
    local = INT_DIR / "data" / "uplink_probe.txt"
    dest = f"{FSW_TMP}/soak_uplink_probe.txt"
    assert local.is_file(), f"missing uplink asset {local}"
    assert local.stat().st_size < 255
    rf_uplink(fprime_test_api, local, dest, UPLINK_TIMEOUT_S)


def test_file_uplink_sequence_bin(fprime_test_api):
    """Uplink the compiled soak sequence used by the sequencer test."""
    local = INT_DIR / "sequences" / "soak_radio_probe.bin"
    dest = f"{FSW_TMP}/soak_radio_probe.bin"
    assert local.is_file(), (
        f"missing {local}; compile sequences/soak_radio_probe.seq with fprime-seqgen"
    )
    rf_uplink(fprime_test_api, local, dest, UPLINK_TIMEOUT_S)


def test_file_uplink_larger_than_mtu(fprime_test_api):
    """Uplink a 10 KiB file (multi-chunk over the 255-byte RF MTU)."""
    local = INT_DIR / "data" / "uplink_large.bin"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(bytes((i * 17 + 3) & 0xFF for i in range(LARGE_UPLINK_BYTES)))
    assert local.stat().st_size == LARGE_UPLINK_BYTES
    dest = f"{FSW_TMP}/soak_uplink_large.bin"
    rf_uplink(fprime_test_api, local, dest, UPLINK_LARGE_TIMEOUT_S, attempts=2)
    fprime_test_api.log(f"Large uplink OK: {LARGE_UPLINK_BYTES} bytes -> {dest}")
