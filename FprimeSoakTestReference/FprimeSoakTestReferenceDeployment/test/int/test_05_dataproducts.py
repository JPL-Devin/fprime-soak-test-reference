"""Data product serialize -> write -> catalog -> RF file downlink.

Soak-gate discipline: the soak monitor fails an interval on ANY WARNING_LO/HI
event in the GDS log, and events emitted during this pytest run are captured
there. So these tests must not provoke DpCatalog warnings:
  * XmitNotActive (WARNING_LO)  - STOP_XMIT_CATALOG while xmit is idle.
  * DpXmitInProgress (WARNING_LO) - START_XMIT_CATALOG while already active.
We therefore never STOP a catalog xmit defensively. Instead we clear the
catalog dir (filesystem only, no flight command), keep it small, and start the
xmit with remainActive=false so it drains and stops itself via the ACTIVITY_HI
CatalogXmitCompleted event.

EVR-loss discipline: over the lossy RF link downlinked EVRs are frequently
dropped, so every event wait uses await_event_or_fsw with an fsw_mark() baseline
captured BEFORE the triggering command (the FSW log is the source of truth).
"""

from soak_helpers import (
    CMD_TIMEOUT_S,
    DP_PRODUCE_TIMEOUT_S,
    DP_XMIT_TIMEOUT_S,
    await_event_or_fsw,
    clear_dp_catalog_dir,
    fsw_mark,
    send_cmd,
    wait_rf_quiet,
)


def test_dp_build_catalog(fprime_test_api):
    """BUILD_CATALOG on a freshly cleared DpCat (no xmit command -> no warning).

    test_06's serialize duty cycle accumulates products between intervals.
    """
    removed = clear_dp_catalog_dir(fprime_test_api)
    assert removed >= 0, "DpCat cleanup failed (FSW host unreachable?)"
    wait_rf_quiet(2.0)

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
    """BUILD + START_XMIT (remainActive=false) on the product from the prior test.

    The catalog holds the single .fdp produced by test_dp_serialize_produce_file,
    so START_XMIT emits SendingProduct and then, because remainActive=false,
    drains and self-stops with CatalogXmitCompleted -- no STOP_XMIT_CATALOG
    command, hence no XmitNotActive warning.
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
        timeout_s=DP_XMIT_TIMEOUT_S,
        fsw_before=done_mark,
    )
    assert done is not None, "CatalogXmitCompleted not observed (xmit did not drain)"
    wait_rf_quiet(2.0)
    # Products are spent once drained; keep the next interval's catalog small.
    clear_dp_catalog_dir(fprime_test_api)
