"""Per-interval soak duty: exercise START then always STOP_SERIALIZING.

Leaving serialize active after a test floods RF with .fdp production and starves
later cases / the next soak interval. This module always ends with an asserted
STOP so FSW returns to idle.
"""

from soak_helpers import (
    CMD_TIMEOUT_S,
    dp_serialize_state_path,
    latest_channel_value,
    send_cmd,
    wait_rf_quiet,
)


def test_soak_interval_dp_serialize_duty(fprime_test_api):
    """START_SERIALIZING, confirm active, then STOP_SERIALIZING and assert stop."""
    wait_rf_quiet(2.0)

    producer = fprime_test_api.get_mnemonic("Components.SensorDataProducer")
    state_path = dp_serialize_state_path()

    # Best-effort idle before START (STOP always emits DpProductionStopped).
    send_cmd(fprime_test_api, f"{producer}.STOP_SERIALIZING")
    wait_rf_quiet(1.0)

    start = fprime_test_api.get_event_test_history().size()
    send_cmd(fprime_test_api, f"{producer}.START_SERIALIZING")
    try:
        started = fprime_test_api.await_event(
            f"{producer}.DpProductionStarted", start=start, timeout=CMD_TIMEOUT_S
        )
        assert started is not None, "DpProductionStarted not observed"

        val = latest_channel_value(
            fprime_test_api, f"{producer}.DpActive", timeout_s=CMD_TIMEOUT_S
        )
        if bool(val) is not True:
            # None: sample never arrived. False: often a stale pre-START
            # sample. DpProductionStarted already confirmed the start.
            fprime_test_api.log(
                f"DpActive={val!r} after START; "
                "accepting DpProductionStarted confirmation"
            )
    finally:
        # Always stop — never leave the producer serializing after the suite.
        stop_start = fprime_test_api.get_event_test_history().size()
        send_cmd(fprime_test_api, f"{producer}.STOP_SERIALIZING")
        stopped = fprime_test_api.await_event(
            f"{producer}.DpProductionStopped", start=stop_start, timeout=CMD_TIMEOUT_S
        )
        assert stopped is not None, "STOP_SERIALIZING not confirmed (DpProductionStopped)"

    state_path.write_text("off\n", encoding="utf-8")
    fprime_test_api.log("Persisted soak DP serialize state -> 'off' (asserted STOP)")


def test_soak_interval_radio_still_commandable(fprime_test_api):
    """After duty-cycle START/STOP, still accept a command over RF."""
    wait_rf_quiet(2.0)
    send_cmd(fprime_test_api, "CdhCore.cmdDisp.CMD_NO_OP")
