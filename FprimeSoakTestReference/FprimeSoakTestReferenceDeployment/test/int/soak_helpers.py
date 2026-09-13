"""Helpers for soak RF integration tests (Ref-style; no pytest conftest).

GDS Test API guide:
https://fprime.jpl.nasa.gov/latest/docs/user-manual/gds/gds-test-api-guide/

All verification is GDS-only (commands/events/telemetry over the radio link).
There is no side-channel (SSH/log) access to the flight computer.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

INT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = INT_DIR / "int_config.json"

with open(CONFIG_PATH, encoding="utf-8") as _cfg:
    CONFIG: dict = json.load(_cfg)

FSW_TMP = str(CONFIG.get("soak.fsw_tmp", "/tmp"))
CMD_TIMEOUT_S = int(CONFIG.get("soak.cmd_timeout_s", 15))
UPLINK_TIMEOUT_S = int(CONFIG.get("soak.uplink_timeout_s", 30))
UPLINK_LARGE_TIMEOUT_S = int(CONFIG.get("soak.uplink_large_timeout_s", 150))
DP_PRODUCE_TIMEOUT_S = int(CONFIG.get("soak.dp_produce_timeout_s", 60))
DP_XMIT_TIMEOUT_S = int(CONFIG.get("soak.dp_xmit_timeout_s", 90))
# Conservative effective DP downlink rate over RF (observed ~490 B/s on the 19.2 kb/s link).
DP_XMIT_BYTES_PER_S = float(CONFIG.get("soak.dp_xmit_bytes_per_s", 300))


def dp_serialize_state_path() -> Path:
    name = os.environ.get("DEPLOYMENT_NAME", "fprime-soak-test-reference")
    return Path.home() / f".fprime-soak-{name}-dp-serialize"


def cmd(api, component_key: str, command: str) -> str:
    return f"{api.get_mnemonic(component_key)}.{command}"


def set_default_filters(api) -> None:
    for severity in (
        "COMMAND",
        "ACTIVITY_LO",
        "ACTIVITY_HI",
        "WARNING_LO",
        "WARNING_HI",
    ):
        api.send_command("CdhCore.events.SET_EVENT_FILTER", [severity, "ENABLED"])
        # Flight defers downlink for RX_TX_HOLDOFF_TICKS (500 ms) after each
        # uplink packet; space commands out so EVRs get a TX window.
        time.sleep(1.0)
    api.send_command("CdhCore.events.SET_EVENT_FILTER", ["DIAGNOSTIC", "DISABLED"])
    time.sleep(2.0)
    api.clear_histories()


def send_cmd(
    api,
    command: str,
    args=None,
    timeout_s: int = CMD_TIMEOUT_S,
    events=None,
    resend: bool = True,
):
    """Send a command and confirm completion via GDS EVRs only.

    Retries once if the completion EVR is dropped on the lossy RF downlink.
    Pass resend=False for commands that must not run twice (e.g. CS_RUN).
    """
    args = args or []
    try:
        return api.send_and_assert_command(command, args, timeout=timeout_s, events=events)
    except AssertionError:
        if not resend:
            raise
        api.log(f"GDS EVRs missed for {command}; resending once")
        return api.send_and_assert_command(command, args, timeout=timeout_s, events=events)


def mute_downlink(api) -> None:
    """Disable flight TX without expecting downlinked OpCode EVRs."""
    command = cmd(api, "Rfm69.Rfm69Manager", "TRANSMIT")
    api.send_command(command, ["DISABLED"])
    time.sleep(1.5)


def unmute_downlink(api, timeout_s: int = CMD_TIMEOUT_S) -> None:
    """Re-enable flight TX and let the flight ComQueue drain."""
    command = cmd(api, "Rfm69.Rfm69Manager", "TRANSMIT")
    api.send_command(command, ["ENABLED"])
    time.sleep(3.0)
    wait_rf_quiet(2.0)
    try:
        send_cmd(api, "CdhCore.cmdDisp.CMD_NO_OP", timeout_s=timeout_s, resend=False)
    except AssertionError:
        api.log("unmute NO-OP EVRs missed after TRANSMIT ENABLED; continuing")


def rf_uplink(
    api,
    local_path: Path,
    dest: str,
    uplink_timeout_s: int = UPLINK_TIMEOUT_S,
    attempts: int = 2,
) -> None:
    """Uplink over RF; verify via the FileUplink FileReceived EVR.

    FSW emits FileReceived only when the end-of-file checksum matches, so this
    single EVR confirms both delivery and integrity. Flight TX stays enabled:
    GDS FileUplink needs the downlink handshake and we need the EVR back.
    """
    uplink = api.get_mnemonic("Svc.FileUplink")
    received_pred = api.get_event_pred(f"{uplink}.FileReceived", [dest])
    for attempt in range(attempts):
        start = api.get_event_test_history().size()
        api.uplink_file(str(local_path), dest)
        ev = api.await_event(received_pred, start=start, timeout=int(uplink_timeout_s))
        if ev is not None:
            api.log(f"FileReceived confirmed {local_path} -> {dest}")
            wait_rf_quiet(1.0)
            return
        api.log(f"uplink attempt {attempt + 1}/{attempts} unconfirmed for {dest}")
        # Brief quiet so FileUplink can finish tearing down before retry.
        time.sleep(1.0)
    raise AssertionError(f"Uplink failed for {local_path} -> {dest} (no FileReceived)")


def wait_rf_quiet(seconds: float = 3.0) -> None:
    time.sleep(seconds)


def _event_arg_vals(event) -> list:
    return [getattr(arg, "val", arg) for arg in (event.get_args() or [])]


def dp_catalog_pending(api, start) -> tuple[int, int]:
    """(pending products, pending bytes) from ProcessingDirectoryComplete EVRs
    received since `start`, summed over catalog directories; (-1, -1) if none arrived."""
    cat = api.get_mnemonic("Svc.DpCatalog")
    name = f"{cat}.ProcessingDirectoryComplete"
    products, nbytes, seen = 0, 0, False
    for ev in api.get_event_test_history().retrieve(start):
        if ev.get_full_name() != name:
            continue
        vals = _event_arg_vals(ev)
        if len(vals) >= 4:
            products += int(vals[2])
            nbytes += int(vals[3])
            seen = True
    pending = (products, nbytes) if seen else (-1, -1)
    api.log(f"DpCatalog pending: {pending[0]} product(s), {pending[1]} byte(s)")
    return pending


def dp_xmit_timeout_s(pending_bytes: int) -> int:
    """Drain timeout for a catalog xmit: base timeout plus pending bytes at the RF rate."""
    return DP_XMIT_TIMEOUT_S + int(max(pending_bytes, 0) / DP_XMIT_BYTES_PER_S)


def await_catalog_drain(api, start, timeout_s: int):
    """Await CatalogXmitCompleted; the deadline extends while ProductComplete EVRs
    keep arriving, so an unexpectedly large pending set is not a false failure."""
    cat = api.get_mnemonic("Svc.DpCatalog")
    done_name = f"{cat}.CatalogXmitCompleted"
    progress_name = f"{cat}.ProductComplete"
    deadline = time.time() + timeout_s
    seen = start
    while time.time() < deadline:
        for ev in api.get_event_test_history().retrieve(seen):
            seen += 1
            if ev.get_full_name() == done_name:
                return ev
            if ev.get_full_name() == progress_name:
                vals = _event_arg_vals(ev)
                if len(vals) >= 3:
                    deadline = time.time() + dp_xmit_timeout_s(int(vals[2]))
        time.sleep(1.0)
    return None


def latest_channel_value(api, channel: str, timeout_s: int = CMD_TIMEOUT_S):
    """Return the most recent value of a telemetry channel, or None.

    Prefers a freshly downlinked sample but falls back to the newest matching
    sample already in history (RF may not update a channel within the window).
    """
    update = api.await_telemetry(channel, timeout=int(timeout_s))
    if update is not None:
        return update.get_val()
    history = api.get_telemetry_test_history()
    latest = None
    for item in history.retrieve():
        try:
            if item.get_full_name() == channel:
                latest = item
        except Exception:
            continue
    return latest.get_val() if latest is not None else None
