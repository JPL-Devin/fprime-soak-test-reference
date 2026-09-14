"""Basic radio / GDS link health (GDS Test API / Ref patterns)."""

import time

from soak_helpers import (
    CMD_TIMEOUT_S,
    send_cmd,
    set_default_filters,
)


def test_telemetry_streaming(fprime_test_api):
    """FSW is alive and TM is crossing the RF link into GDS."""
    results = fprime_test_api.assert_telemetry_count(3, timeout=CMD_TIMEOUT_S)
    assert results, "Expected telemetry updates over the radio link"


def test_command_noop_over_radio(fprime_test_api):
    """Ground → RF → cmdDisp NO-OP (Ref test_send_command)."""
    set_default_filters(fprime_test_api)
    send_cmd(fprime_test_api, "CdhCore.cmdDisp.CMD_NO_OP")
    assert fprime_test_api.get_command_test_history().size() >= 1


def test_command_noop_string_over_radio(fprime_test_api):
    """NO-OP with string argument (Ref test_send_command_args)."""
    value = "soak-radio-probe"
    events = [
        fprime_test_api.get_event_pred(
            "CdhCore.cmdDisp.NoOpStringReceived",
            [value],
        )
    ]
    send_cmd(
        fprime_test_api,
        "CdhCore.cmdDisp.CMD_NO_OP_STRING",
        [value],
        events=events,
    )


def test_downlink_opcode_events_reach_gds(fprime_test_api):
    """Most NO-OP EVRs should cross the RF downlink into GDS.

    Spacing exceeds flight RX_TX_HOLDOFF so each command's EVRs get a TX window.
    """
    gds_ok = 0
    for _ in range(3):
        start = fprime_test_api.get_event_test_history().size()
        fprime_test_api.send_command("CdhCore.cmdDisp.CMD_NO_OP")
        if fprime_test_api.await_event(
            "CdhCore.cmdDisp.NoOpReceived", start=start, timeout=5
        ) is not None:
            gds_ok += 1
        time.sleep(1.2)
    fprime_test_api.log(f"NO-OP EVRs seen in GDS: {gds_ok}/3")
    assert gds_ok >= 2, f"GDS saw only {gds_ok}/3 NO-OP EVRs over the RF downlink"
