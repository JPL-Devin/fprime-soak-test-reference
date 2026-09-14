"""CmdSequencer: uplink, validate, and run a sequence over RF.

The sequence binary is RF-uplinked (FileReceived confirms checksum-verified
delivery) and CS_VALIDATE re-checks the file CRC onboard before CS_RUN.
"""

from pathlib import Path

from soak_helpers import (
    CMD_TIMEOUT_S,
    FSW_TMP,
    UPLINK_TIMEOUT_S,
    rf_uplink,
    send_cmd,
    wait_rf_quiet,
)

INT_DIR = Path(__file__).parent.resolve()


def test_sequence_validate_and_run(fprime_test_api):
    """RF-uplink a sequence binary, then CS_VALIDATE + CS_RUN BLOCK."""
    local = INT_DIR / "sequences" / "soak_radio_probe.bin"
    assert local.is_file(), (
        f"missing {local}; compile sequences/soak_radio_probe.seq with fprime-seqgen"
    )
    seq_path = f"{FSW_TMP}/soak_seq_staged.bin"
    rf_uplink(fprime_test_api, local, seq_path, UPLINK_TIMEOUT_S)

    wait_rf_quiet(2.0)
    sequencer = fprime_test_api.get_mnemonic("Svc.CmdSequencer")
    send_cmd(fprime_test_api, f"{sequencer}.CS_VALIDATE", [seq_path])

    start = fprime_test_api.get_event_test_history().size()
    # CS_RUN BLOCK must not be resent: a duplicate run congests the link.
    send_cmd(
        fprime_test_api,
        f"{sequencer}.CS_RUN",
        [seq_path, "BLOCK"],
        resend=False,
    )
    done = fprime_test_api.await_event(
        f"{sequencer}.CS_SequenceComplete", start=start, timeout=CMD_TIMEOUT_S
    )
    assert done is not None, "CS_SequenceComplete not observed"
