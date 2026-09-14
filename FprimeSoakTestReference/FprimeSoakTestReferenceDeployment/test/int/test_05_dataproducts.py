"""Data product serialize -> write -> catalog -> RF file downlink.

Soak-gate discipline: the soak monitor fails an interval on ANY WARNING_LO/HI
event in the GDS log, and events emitted during this pytest run are captured
there. So these tests must not provoke DpCatalog warnings:
  * XmitNotActive (WARNING_LO)  - STOP_XMIT_CATALOG while xmit is idle.
  * DpXmitInProgress (WARNING_LO) - START_XMIT_CATALOG while already active.
We therefore never STOP a catalog xmit defensively and start the xmit with
remainActive=false so it drains and stops itself via CatalogXmitCompleted.
Soak discipline: products are never deleted. Each interval downlinks whatever
DpCatalog reports pending (including products accumulated by test_06's serialize
duty cycle); the drain timeout is scaled from the pending byte count reported by
ProcessingDirectoryComplete and extended as ProductComplete EVRs arrive.
"""

from soak_helpers import (
    CMD_TIMEOUT_S,
    DP_PRODUCE_TIMEOUT_S,
    DP_XMIT_TIMEOUT_S,
    await_catalog_drain,
    dp_catalog_pending,
    dp_xmit_timeout_s,
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
    dp_catalog_pending(fprime_test_api, start)


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
    """BUILD + START_XMIT (remainActive=false): downlink everything pending.

    The catalog holds at least the .fdp from test_dp_serialize_produce_file plus
    any not-yet-downlinked products from earlier intervals. START_XMIT emits
    SendingProduct and then, because remainActive=false, drains and self-stops
    with CatalogXmitCompleted -- no STOP_XMIT_CATALOG, hence no XmitNotActive.
    """
    cat = fprime_test_api.get_mnemonic("Svc.DpCatalog")

    build_start = fprime_test_api.get_event_test_history().size()
    send_cmd(fprime_test_api, f"{cat}.BUILD_CATALOG")
    built = fprime_test_api.await_event(
        f"{cat}.CatalogBuildComplete", start=build_start, timeout=DP_PRODUCE_TIMEOUT_S
    )
    assert built is not None, "CatalogBuildComplete not observed before xmit"
    pending_products, pending_bytes = dp_catalog_pending(fprime_test_api, build_start)
    assert pending_products != 0, "No pending products to downlink"
    drain_timeout_s = dp_xmit_timeout_s(pending_bytes)
    fprime_test_api.log(f"Catalog drain timeout: {drain_timeout_s} s")
    wait_rf_quiet(1.0)

    # SendingProduct, not the OpCode EVRs, proves START_XMIT ran: a duplicate
    # while xmit is active is rejected with DpXmitInProgress and leaves the
    # running xmit untouched, so resending after a lost uplink is safe.
    start = fprime_test_api.get_event_test_history().size()
    sending = None
    for _ in range(2):
        fprime_test_api.send_command(f"{cat}.START_XMIT_CATALOG", ["NO_WAIT", "false"])
        sending = fprime_test_api.await_event(
            f"{cat}.SendingProduct", start=start, timeout=CMD_TIMEOUT_S
        )
        if sending is not None:
            break
        fprime_test_api.log("SendingProduct not observed; resending START_XMIT_CATALOG")
    assert sending is not None, "SendingProduct not observed"

    # remainActive=false => catalog drains and self-stops. Confirm the clean stop
    # rather than forcing STOP_XMIT_CATALOG (which would warn if already done).
    done = await_catalog_drain(fprime_test_api, start, drain_timeout_s)
    assert done is not None, "CatalogXmitCompleted not observed (xmit did not drain)"
    wait_rf_quiet(2.0)
