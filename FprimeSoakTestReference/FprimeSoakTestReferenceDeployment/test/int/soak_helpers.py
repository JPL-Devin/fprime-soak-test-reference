"""Helpers for soak RF integration tests (Ref-style; no pytest conftest).

GDS Test API guide:
https://fprime.jpl.nasa.gov/latest/docs/user-manual/gds/gds-test-api-guide/

RF HIL notes:
- OpCode EVRs often never reach GDS; confirm via Pi fsw.log when needed.
- While TRANSMIT is DISABLED, do not await downlinked EVRs (TX is off).
- File uplink is verified by FSW file size while muted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

INT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = INT_DIR / "int_config.json"

with open(CONFIG_PATH, encoding="utf-8") as _cfg:
    CONFIG: dict = json.load(_cfg)

FSW_TMP = str(CONFIG.get("soak.fsw_tmp", "/tmp"))
CMD_TIMEOUT_S = int(CONFIG.get("soak.cmd_timeout_s", 30))
UPLINK_TIMEOUT_S = int(CONFIG.get("soak.uplink_timeout_s", 90))
UPLINK_LARGE_TIMEOUT_S = int(CONFIG.get("soak.uplink_large_timeout_s", 120))
DP_PRODUCE_TIMEOUT_S = int(CONFIG.get("soak.dp_produce_timeout_s", 45))
DP_XMIT_TIMEOUT_S = int(CONFIG.get("soak.dp_xmit_timeout_s", 90))
# Conservative effective DP downlink rate over RF (observed ~490 B/s on the 19.2 kb/s link).
DP_XMIT_BYTES_PER_S = float(CONFIG.get("soak.dp_xmit_bytes_per_s", 300))
PI_HOST = os.environ.get("SOAK_PI_HOST", "pi@raspberrypi.local")
FSW_LOG = os.environ.get("SOAK_FSW_LOG", "/home/pi/fprime/fsw.log")


def dp_serialize_state_path() -> Path:
    name = os.environ.get("DEPLOYMENT_NAME", "fprime-soak-test-reference")
    return Path.home() / f".fprime-soak-{name}-dp-serialize"


def cmd(api, component_key: str, command: str) -> str:
    return f"{api.get_mnemonic(component_key)}.{command}"


def pi_ssh(remote_cmd: str, timeout: int = 15) -> str:
    return subprocess.check_output(
        [
            "ssh",
            "-o",
            "ConnectTimeout=8",
            "-o",
            "BatchMode=yes",
            PI_HOST,
            remote_cmd,
        ],
        text=True,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def pi_put(local_path: Path, dest: str, timeout: int = 30) -> None:
    """Copy a local file to the FSW over SSH (integrity-guaranteed staging).

    Used when a test needs a *correct* file on the FSW independent of RF-uplink
    reliability (e.g. a sequence for CmdSequencer): RF uplink can silently corrupt
    bytes, so we do not route test fixtures through it.
    """
    subprocess.check_call(
        ["scp", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", str(local_path),
         f"{PI_HOST}:{dest}"],
        timeout=timeout,
    )


def fsw_log_count(pattern: str) -> int:
    try:
        out = pi_ssh(f"grep -cE '{pattern}' {FSW_LOG} || true").strip()
        return int(out.splitlines()[-1] or "0")
    except Exception:
        return -1


def opcode_hex(api, command: str) -> str:
    cmd_id = api.translate_command_name(command)
    return f"0x{cmd_id:x}" if isinstance(cmd_id, int) else str(cmd_id)


def _opcode_hex_variants(ox: str) -> list[str]:
    """FSW text logger uses unpadded hex (0x1000000); some prints pad to 8 digits."""
    variants = {ox, ox.lower()}
    if ox.startswith("0x"):
        try:
            n = int(ox, 16)
            variants.add(f"0x{n:x}")
            variants.add(f"0x{n:08x}")
        except ValueError:
            pass
    return list(variants)


def _fsw_opcode_count(ox: str, evr: str) -> int:
    best = -1
    for variant in _opcode_hex_variants(ox):
        for pattern in (
            f"{evr}.*Opcode {variant}",
            f"{evr}.*{variant}",
        ):
            count = fsw_log_count(pattern)
            if count > best:
                best = count
    return best


def fsw_completed_count(ox: str) -> int:
    return _fsw_opcode_count(ox, "OpCodeCompleted")


def fsw_dispatched_count(ox: str) -> int:
    return _fsw_opcode_count(ox, "OpCodeDispatched")


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
        # uplink packet. Sleeping less than that refreshes the holdoff so
        # Dispatched/Completed never get a TX window and GDS misses EVRs.
        time.sleep(1.0)
    api.send_command("CdhCore.events.SET_EVENT_FILTER", ["DIAGNOSTIC", "DISABLED"])
    time.sleep(2.0)
    api.clear_histories()


def _confirm_fsw_completed(
    api, command: str, args: list, before: int, timeout_s: int, resend: bool = True
) -> bool:
    ox = opcode_hex(api, command)
    deadline = time.time() + timeout_s
    resent = False
    while time.time() < deadline:
        after = fsw_completed_count(ox)
        if before >= 0 and after > before:
            api.log(f"FSW confirmed {command} ({ox}) completed")
            return True
        # Only resend if FSW never saw the original command. Immediate resends
        # congest the half-duplex link and make the rest of the suite miss EVRs.
        if resend and not resent and time.time() > deadline - timeout_s + 6:
            api.send_command(command, args)
            resent = True
        time.sleep(0.5)
    return False


def send_cmd(api, command: str, args=None, timeout_s: int = CMD_TIMEOUT_S, events=None):
    """Send a command; confirm via GDS EVRs or the Pi fsw.log.

    Do not pass max_delay: Dispatched and Completed often ride separate RF
    packets with >1 s of holdoff between them, which fails a 1.0 s bound even
    when both EVRs arrive.

    GDS is given a short window first. If those EVRs drop, poll fsw.log for the
    rest of timeout_s. Resend only when FSW never logged Dispatched — otherwise
    BLOCK commands like CS_RUN get executed again and congest the link.
    """
    args = args or []
    ox = opcode_hex(api, command)
    before_done = fsw_completed_count(ox)
    before_disp = fsw_dispatched_count(ox)
    gds_timeout = min(8, int(timeout_s))

    try:
        return api.send_and_assert_command(
            command,
            args,
            timeout=gds_timeout,
            events=events,
        )
    except AssertionError as exc:
        api.log(f"GDS EVR assert missed for {command}; checking FSW log")
        if _confirm_fsw_completed(
            api, command, args, before_done, timeout_s=timeout_s, resend=False
        ):
            return []
        after_disp = fsw_dispatched_count(ox)
        if before_disp >= 0 and after_disp > before_disp:
            raise AssertionError(
                f"Command {command} ({ox}) was dispatched onboard but "
                "OpCodeCompleted was not confirmed; not resending"
            ) from exc
        api.log(f"FSW log did not show {command}; retrying once")
        api.send_command(command, args)
        if _confirm_fsw_completed(
            api, command, args, before_done, timeout_s=timeout_s, resend=False
        ):
            return []
        raise AssertionError(
            f"Command {command} ({ox}) not confirmed via GDS EVRs or FSW log"
        ) from exc


def mute_downlink(api) -> None:
    """Disable flight TX without expecting downlinked OpCode EVRs."""
    command = cmd(api, "Rfm69.Rfm69Manager", "TRANSMIT")
    api.send_command(command, ["DISABLED"])
    time.sleep(1.5)


def unmute_downlink(api, timeout_s: int = CMD_TIMEOUT_S) -> None:
    """Re-enable flight TX after a muted uplink.

    After a muted window the flight ComQueue is often full; give it a few
    seconds to drain. A NO-OP is best-effort: the uplink file match is the
    pass/fail for the transfer. Failing the whole test because Completeds
    were dropped on RF was cascading into the rest of the suite.
    """
    command = cmd(api, "Rfm69.Rfm69Manager", "TRANSMIT")
    api.send_command(command, ["ENABLED"])
    time.sleep(3.0)
    wait_rf_quiet(2.0)
    try:
        send_cmd(api, "CdhCore.cmdDisp.CMD_NO_OP", timeout_s=timeout_s)
    except AssertionError:
        api.log("unmute NO-OP EVRs missed after TRANSMIT ENABLED; continuing")


def rf_uplink(
    api,
    local_path: Path,
    dest: str,
    uplink_timeout_s: int = UPLINK_TIMEOUT_S,
    mute: bool = True,
    attempts: int = 2,
) -> None:
    """Uplink over RF; verify by FSW file size and MD5.

    GDS FileUplink waits for a downlink handshake before the next chunk.
    Multi-chunk transfers therefore cannot mute flight TX or DATA packets
    never leave GDS (FSW sees START then END → packet 5 after 0).
    Size-only checks hide BadChecksum files that still land at the right length.
    """
    expected = local_path.stat().st_size
    expected_md5 = hashlib.md5(local_path.read_bytes()).hexdigest()
    last_size = -1
    if mute:
        mute_downlink(api)
    try:
        for attempt in range(attempts):
            try:
                pi_ssh(f"rm -f {dest}")
            except Exception:
                pass
            api.uplink_file(str(local_path), dest)
            deadline = time.time() + int(uplink_timeout_s)
            last_size = -1
            while time.time() < deadline:
                try:
                    # Avoid `wc < missing` (bash redirect error); always emit an int.
                    out = pi_ssh(
                        f"if [ -f {dest} ]; then wc -c < {dest}; else echo 0; fi"
                    ).strip()
                    size = int(out.splitlines()[-1])
                    if size != last_size:
                        api.log(
                            f"uplink attempt {attempt + 1}/{attempts} {dest} "
                            f"size={size}/{expected}"
                        )
                        last_size = size
                    if size == expected:
                        remote_md5 = pi_ssh(f"md5sum {dest}").strip().split()[0].lower()
                        if remote_md5 == expected_md5:
                            api.log(
                                f"FSW file match for {dest} "
                                f"({size} bytes md5={expected_md5})"
                            )
                            return
                        api.log(
                            f"FSW size match but MD5 mismatch "
                            f"(got {remote_md5}, want {expected_md5})"
                        )
                        break
                except Exception as exc:
                    api.log(f"size poll error: {exc}")
                time.sleep(1.0)
            api.log(
                f"uplink attempt {attempt + 1}/{attempts} incomplete "
                f"(size {last_size}/{expected})"
            )
            # Brief quiet so FileUplink can finish tearing down before retry.
            time.sleep(1.0)
        raise AssertionError(
            f"Uplink failed for {local_path} -> {dest} (size {last_size}/{expected})"
        )
    finally:
        if mute:
            unmute_downlink(api)
        else:
            # Let queued EVRs drain before the next test asserts GDS history.
            wait_rf_quiet(2.0)


def fsw_mark(pattern: str) -> int:
    """Capture an FSW-log baseline count for `pattern`.

    Call this BEFORE the command/action that produces the event, then pass the
    result as `fsw_before` to await_event_or_fsw. This avoids the race where the
    event is already written to fsw.log by the time the fallback samples its
    baseline (the triggering command has usually completed inside send_cmd), which
    would make growth-detection wait forever for a second occurrence.
    """
    return fsw_log_count(pattern)


def await_event_or_fsw(
    api,
    event_name: str,
    fsw_pattern: str,
    start,
    timeout_s: int,
    fsw_before: int | None = None,
):
    """Await a GDS event (including already-buffered), else FSW-log growth.

    Over the lossy RF link EVRs are frequently dropped on downlink, so the FSW
    log is the source of truth. Pass `fsw_before` captured via fsw_mark() BEFORE
    the triggering command; if omitted we sample now (only correct when the event
    has not been produced yet).
    """
    if fsw_before is None:
        fsw_before = fsw_log_count(fsw_pattern)
    # Already in GDS history?
    ev = api.await_event(event_name, start=start, timeout=0)
    if ev is not None:
        return ev
    ev = api.await_event(event_name, start=start, timeout=int(timeout_s))
    if ev is not None:
        return ev
    deadline = time.time() + timeout_s
    while True:
        after = fsw_log_count(fsw_pattern)
        if fsw_before >= 0 and after > fsw_before:
            api.log(f"FSW confirmed event pattern {fsw_pattern!r} (EVR likely dropped)")
            return True
        if time.time() >= deadline:
            return None
        time.sleep(1.0)


_PENDING_RE = re.compile(r"Pending products: (\d+) Pending bytes: (\d+)")


def dp_catalog_pending(api=None) -> tuple[int, int]:
    """(pending products, pending bytes) from the latest ProcessingDirectoryComplete
    in the FSW log; (-1, -1) if unavailable."""
    try:
        out = pi_ssh(f"grep -E 'ProcessingDirectoryComplete' {FSW_LOG} | tail -n 1 || true")
        m = _PENDING_RE.search(out)
        pending = (int(m.group(1)), int(m.group(2))) if m else (-1, -1)
    except Exception:
        pending = (-1, -1)
    if api is not None:
        api.log(f"DpCatalog pending: {pending[0]} product(s), {pending[1]} byte(s)")
    return pending


def dp_xmit_timeout_s(pending_bytes: int) -> int:
    """Drain timeout for a catalog xmit: base timeout plus bytes at the RF rate."""
    return DP_XMIT_TIMEOUT_S + int(max(pending_bytes, 0) / DP_XMIT_BYTES_PER_S)


def wait_rf_quiet(seconds: float = 3.0) -> None:
    time.sleep(seconds)


def latest_channel_value(api, channel: str, timeout_s: int = CMD_TIMEOUT_S):
    """Return the most recent value of a telemetry channel, or None.

    Prefers a freshly downlinked sample (await_telemetry) but falls back to the
    newest matching sample already in history -- over lossy RF a given channel
    may not update within the window even though earlier samples arrived.
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
