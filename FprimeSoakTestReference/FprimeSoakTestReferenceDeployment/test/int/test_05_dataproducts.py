"""Data product serialize -> write -> catalog -> RF file downlink.

Soak-gate discipline: the soak monitor fails an interval on ANY WARNING_LO/HI
event in the GDS log, and events emitted during this pytest run are captured
there. So these tests must not provoke DpCatalog warnings:
  * XmitNotActive (WARNING_LO)  - STOP_XMIT_CATALOG while xmit is idle.
  * DpXmitInProgress (WARNING_LO) - START_XMIT_CATALOG while already active.
We therefore never STOP a catalog xmit defensively and start the xmit with
remainActive=false so it drains and stops itself via CatalogXmitCompleted.
DpCatalog's state file tracks transmitted products, so repeat xmits only send
new products and no onboard cleanup is needed.
"""

from soak_helpers import (
    CMD_TIMEOUT_S,
    DP_PRODUCE_TIMEOUT_S,
    DP_XMIT_TIMEOUT_S,
    send_cmd,
    wait_rf_quiet,
)


def test_dp_build_catalog(fprime_test_api):
    """BUILD_CATALOG completes (no xmit command -> no warning)."""
    cat = fprime_test_api.get_mnemonic("Svc.DpCatalog")
    start = fprime_test_api.get_event_test_history().size()
    send_cmd(fprime_test_api, f"{cat}.BUILD_CATALOG")
    done = fprime_test_api.await_event(
        f"{cat}.CatalogBuildComplete", start=start, timeout=DP_PRODUCE_TIMEOUT_S
    )
    assert done is not None, "CatalogBuildComplete not observed"


def test_dp_serialize_produce_file(fprime_test_api):
    """START_SERIALIZING produces one filled container and a .fdp, then STOP.

    STOP_SERIALIZING runs in a finally so a mid-test failure never leaves the
    producer emitting a .fdp every ~25 s (which would congest later tests and
    the next soak interval).
    """
    producer = fprime_test_api.get_mnemonic("Components.SensorDataProducer")
    writer = fprime_test_api.get_mnemonic("Svc.DpWriter")

    send_cmd(fprime_test_api, f"{producer}.STOP_SERIALIZING")

    start = fprime_test_api.get_event_test_history().size()
    send_cmd(fprime_test_api, f"{producer}.START_SERIALIZING")
    try:
        started = fprime_test_api.await_event(
            f"{producer}.DpProductionStarted", start=start, timeout=CMD_TIMEOUT_S
        )
        assert started is not None, "DpProductionStarted not observed"

        # RECORD_COUNT=100 @ SAMPLE_STRIDE=5 => ~4 records/s => ~25 s per container
        complete = fprime_test_api.await_event(
            f"{producer}.DpComplete", start=start, timeout=DP_PRODUCE_TIMEOUT_S
        )
        assert complete is not None, "DpComplete not seen (sensors running?)"

        written = fprime_test_api.await_event(
            f"{writer}.FileWritten", start=start, timeout=CMD_TIMEOUT_S
        )
        assert written is not None, "DpWriter.FileWritten not seen"
    finally:
        # Asserted STOP so a failed mid-test never leaves production running.
        stop_start = fprime_test_api.get_event_test_history().size()
        send_cmd(fprime_test_api, f"{producer}.STOP_SERIALIZING")
        stopped = fprime_test_api.await_event(
            f"{producer}.DpProductionStopped", start=stop_start, timeout=CMD_TIMEOUT_S
        )
        assert stopped is not None, "STOP_SERIALIZING not confirmed (DpProductionStopped)"


def test_dp_catalog_xmit_downlink(fprime_test_api):
    """BUILD + START_XMIT (remainActive=false) on the product from the prior test.

    The catalog holds the .fdp produced by test_dp_serialize_produce_file, so
    START_XMIT emits SendingProduct and then, because remainActive=false,
    drains and self-stops with CatalogXmitCompleted -- no STOP_XMIT_CATALOG
    command, hence no XmitNotActive warning.
    """
    cat = fprime_test_api.get_mnemonic("Svc.DpCatalog")

    build_start = fprime_test_api.get_event_test_history().size()
    send_cmd(fprime_test_api, f"{cat}.BUILD_CATALOG")
    built = fprime_test_api.await_event(
        f"{cat}.CatalogBuildComplete", start=build_start, timeout=DP_PRODUCE_TIMEOUT_S
    )
    assert built is not None, "CatalogBuildComplete not observed before xmit"
    wait_rf_quiet(1.0)

    start = fprime_test_api.get_event_test_history().size()
    send_cmd(
        fprime_test_api,
        f"{cat}.START_XMIT_CATALOG",
        ["NO_WAIT", "false"],
        resend=False,
    )

    sending = fprime_test_api.await_event(
        f"{cat}.SendingProduct", start=start, timeout=DP_XMIT_TIMEOUT_S
    )
    assert sending is not None, "SendingProduct not observed"

    # remainActive=false => catalog drains and self-stops. Confirm the clean stop
    # rather than forcing STOP_XMIT_CATALOG (which would warn if already done).
    done = fprime_test_api.await_event(
        f"{cat}.CatalogXmitCompleted", start=start, timeout=DP_XMIT_TIMEOUT_S
    )
    assert done is not None, "CatalogXmitCompleted not observed (xmit did not drain)"
    wait_rf_quiet(2.0)
