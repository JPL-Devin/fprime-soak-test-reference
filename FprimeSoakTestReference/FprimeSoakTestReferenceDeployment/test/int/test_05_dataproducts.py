"""Data product serialize -> write -> catalog -> RF file downlink.

Soak-gate discipline: the soak monitor fails an interval on ANY WARNING_LO/HI
event in the GDS log, and events emitted during this pytest run are captured
there. So these tests must not provoke DpCatalog warnings:
  * XmitNotActive (WARNING_LO)  - STOP_XMIT_CATALOG while xmit is idle.
  * DpXmitInProgress (WARNING_LO) - START_XMIT_CATALOG while already active.
We therefore never STOP a catalog xmit defensively. Instead we start the xmit
with remainActive=false so it drains and stops itself via the ACTIVITY_HI
CatalogXmitCompleted event.

Soak discipline: products are never deleted. DpCatalog tracks what has been
downlinked (DpState.dat), so each interval downlinks whatever is pending --
including products accumulated by test_06's serialize duty cycle -- and the
drain timeout is scaled from the pending byte count reported at build time.

EVR-loss discipline: over the lossy RF link downlinked EVRs are frequently
dropped, so every event wait uses await_event_or_fsw with an fsw_mark() baseline
captured BEFORE the triggering command (the FSW log is the source of truth).
"""

from soak_helpers import (
    CMD_TIMEOUT_S,
    DP_PRODUCE_TIMEOUT_S,
    DP_XMIT_TIMEOUT_S,
    await_event_or_fsw,
    dp_catalog_pending,
    dp_xmit_timeout_s,
    fsw_mark,
    send_cmd,
    wait_rf_quiet,
)


def test_dp_build_catalog(fprime_test_api):
    """BUILD_CATALOG alone (no xmit command -> no warning)."""
    cat = fprime_test_api.get_mnemonic("Svc.DpCatalog")
    fsw_before = fsw_mark("CatalogBuildComplete")
    start = fprime_test_api.get_event_test_history().size()
    send_cmd(fprime_test_api, f"{cat}.BUILD_CATALOG")
    done = await_event_or_fsw(
        fprime_test_api,
        f"{cat}.CatalogBuildComplete",
        "CatalogBuildComplete",
        start=start,
        timeout_s=DP_PRODUCE_TIMEOUT_S,
        fsw_before=fsw_before,
    )
    assert done is not None, "CatalogBuildComplete not observed"
    dp_catalog_pending(fprime_test_api)


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
    mark_started = fsw_mark("DpProductionStarted")
    mark_complete = fsw_mark("DpComplete")
    mark_written = fsw_mark("FileWritten")
    send_cmd(fprime_test_api, f"{producer}.START_SERIALIZING")
    try:
        started = await_event_or_fsw(
            fprime_test_api,
            f"{producer}.DpProductionStarted",
            "DpProductionStarted",
            start=start,
            timeout_s=CMD_TIMEOUT_S,
            fsw_before=mark_started,
        )
        assert started is not None

        # RECORD_COUNT=100 @ SAMPLE_STRIDE=5 => ~4 records/s => ~25 s per container
        complete = await_event_or_fsw(
            fprime_test_api,
            f"{producer}.DpComplete",
            "DpComplete",
            start=start,
            timeout_s=DP_PRODUCE_TIMEOUT_S,
            fsw_before=mark_complete,
        )
        assert complete is not None, "DpComplete not seen (sensors running?)"

        written = await_event_or_fsw(
            fprime_test_api,
            f"{writer}.FileWritten",
            "FileWritten",
            start=start,
            timeout_s=CMD_TIMEOUT_S,
            fsw_before=mark_written,
        )
        assert written is not None, "DpWriter.FileWritten not seen"
    finally:
        # Asserted STOP so a failed mid-test never leaves production running.
        stop_start = fprime_test_api.get_event_test_history().size()
        mark_stopped = fsw_mark("DpProductionStopped")
        send_cmd(fprime_test_api, f"{producer}.STOP_SERIALIZING")
        stopped = await_event_or_fsw(
            fprime_test_api,
            f"{producer}.DpProductionStopped",
            "DpProductionStopped",
            start=stop_start,
            timeout_s=CMD_TIMEOUT_S,
            fsw_before=mark_stopped,
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

    build_mark = fsw_mark("CatalogBuildComplete")
    build_start = fprime_test_api.get_event_test_history().size()
    send_cmd(fprime_test_api, f"{cat}.BUILD_CATALOG")
    built = await_event_or_fsw(
        fprime_test_api,
        f"{cat}.CatalogBuildComplete",
        "CatalogBuildComplete",
        start=build_start,
        timeout_s=DP_PRODUCE_TIMEOUT_S,
        fsw_before=build_mark,
    )
    assert built is not None, "CatalogBuildComplete not observed before xmit"
    pending_products, pending_bytes = dp_catalog_pending(fprime_test_api)
    assert pending_products != 0, "No pending products to downlink"
    drain_timeout_s = dp_xmit_timeout_s(pending_bytes)
    fprime_test_api.log(f"Catalog drain timeout: {drain_timeout_s} s")
    wait_rf_quiet(1.0)

    send_mark = fsw_mark("SendingProduct|CatalogXmitStarted")
    done_mark = fsw_mark("CatalogXmitCompleted")
    start = fprime_test_api.get_event_test_history().size()
    send_cmd(
        fprime_test_api,
        f"{cat}.START_XMIT_CATALOG",
        ["NO_WAIT", "false"],
    )

    sending = await_event_or_fsw(
        fprime_test_api,
        f"{cat}.SendingProduct",
        "SendingProduct|CatalogXmitStarted",
        start=start,
        timeout_s=DP_XMIT_TIMEOUT_S,
        fsw_before=send_mark,
    )
    assert sending is not None, "Neither SendingProduct nor CatalogXmitStarted"

    # remainActive=false => catalog drains and self-stops. Confirm the clean stop
    # rather than forcing STOP_XMIT_CATALOG (which would warn if already done).
    done = await_event_or_fsw(
        fprime_test_api,
        f"{cat}.CatalogXmitCompleted",
        "CatalogXmitCompleted",
        start=start,
        timeout_s=drain_timeout_s,
        fsw_before=done_mark,
    )
    assert done is not None, "CatalogXmitCompleted not observed (xmit did not drain)"
    wait_rf_quiet(2.0)
