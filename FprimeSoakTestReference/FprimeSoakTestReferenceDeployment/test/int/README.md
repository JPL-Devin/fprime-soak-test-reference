# Soak / integration tests — FprimeSoakTestReferenceDeployment

Pytest suite for the RF soak deployment. Compatible with
[`nasa/fprime-actions/soak-test`](https://github.com/nasa/fprime-actions): each
~30 minute soak interval first runs `soak_monitor.py` over accumulated telemetry,
then runs everything under this directory against the persistent GDS.

Layout matches [Ref `test/int`](https://github.com/nasa/fprime/tree/devel/TestDeploymentsProject/Ref/test/int):
no custom `conftest.py` — fixtures come from `fprime-gds`; helpers live in
`soak_helpers.py`.

## What the soak monitor gates on (why these tests exist)

`soak_monitor.py` fails an interval on **any** `FATAL` / `WARNING_HI` /
`WARNING_LO` event in the GDS log, plus: `MEMORY_USED` leak ≥ 5 %, any
`BufferManager` `CurrBuffs` leak ≥ 20 %, any `NoBuffs`/`EmptyBuffs` > 0,
`CPU` > 95 %, or `NON_VOLATILE_FREE` < 1 GiB. Two consequences drive this suite:

1. **Tests must not emit warnings.** Events produced *during* pytest land in the
   same GDS log the monitor reads next interval. So the DP tests never issue a
   `STOP_XMIT_CATALOG` while idle (`XmitNotActive`) or a second `START_XMIT`
   (`DpXmitInProgress`). Multi-chunk uplink retries the whole file on stall so
   a single dropped DATA chunk does not leave FileUplink wedged across intervals.

## What is covered

| Module | Purpose |
|--------|---------|
| `test_01_link.py` | TM streaming + command NO-OP over RF |
| `test_02_radio.py` | `TRANSMIT` mute/unmute; `PacketsReceived` |
| `test_03_file_uplink.py` | Small + sequence single-chunk uplink + multi-chunk (>MTU) |
| `test_04_sequence.py` | `CS_VALIDATE` + `CS_RUN` of uplinked sequence |
| `test_05_dataproducts.py` | Catalog build, serialize → `.fdp`, self-draining catalog xmit |
| `test_06_soak_interval.py` | Alternates `START`/`STOP_SERIALIZING` each soak run |

Half-duplex note: flight TX stays enabled during uplink — GDS FileUplink needs
the downlink handshake and verification relies on the `FileReceived` EVR.

## RF-loss discipline (GDS-only verification)

All verification is GDS-side (commands, events, telemetry): there is **no**
SSH/log side-channel to the flight computer. The flight topology places
`Svc.ComRetry` ahead of `Rfm69Manager` so frames refused during RX/TX holdoff
are resent rather than dropped; residual RF loss over the 19.2 kb/s half-duplex
link can still drop a downlinked EVR, so `send_cmd()` retries a
command once when its completion EVRs are missed (pass `resend=False` for
commands that must not run twice, e.g. `CS_RUN`). File uplink is confirmed by
`Svc.FileUplink` `FileReceived`, which FSW emits only when the end-of-file
checksum matches — a single EVR proving both delivery and integrity.

## Flight runtime requirement: realtime scheduling

The FSW binary **must** run with `CAP_SYS_NICE` so F´ Posix tasks get `SCHED_RR`
priorities. Without it the 1 kHz base rate group runs `SCHED_OTHER` and
`RateGroupCycleSlip` (WARNING_HI) floods the soak log, failing the gate. The
`nasa/fprime-actions` `deploy.sh` does this via `setcap cap_sys_nice=eip`; the Pi
launcher `run_fsw.sh` does the same. Verify with `getcap <binary>` and confirm
`SCHED_RR` threads via `ps -eLo pid,cls,rtprio,comm`.

## Parameter DB

On first boot with no `PrmDb.dat`, `PrmDb` emits `PrmFileReadError` (WARNING_HI)
and `PrmIdNotFound` (WARNING_LO). Run the `seq/fix_prm_missing.bin` sequence once
(it sets all defaults and `PRM_SAVE_FILE`s them); subsequent boots are clean.

## Known limitations

* **Multi-chunk file uplink has no ARQ.** A dropped DATA chunk stalls
  `Svc.FileUplink`; the soak helper retries the whole transfer (see
  `rf_uplink`). Expect longer runtime than single-chunk cases.
* **`UnexpectedSequenceCount` (WARNING_LO)** can still appear from genuine RF
  packet loss; it reflects link physics, not a flight defect.

## Local HIL run

Do **not** start GDS from `fprime-rfm69-feather-groundstation` (that yml sets
`output-unframed-data: "-"` and empties UART).

The `space-packet-fprime` framer is provided by the `fprime-gds-space-packet`
plugin in `fprime-rfm69-feather-groundstation` (repo-root `gds-plugin/`).
Install it **once** into the GDS venv (it auto-registers via a setuptools
`fprime_gds` entry point — no `PYTHONPATH` or `FPRIME_GDS_EXTRA_PLUGINS`):

```bash
source fprime-venv/bin/activate
pip install ../fprime-rfm69-feather-groundstation/gds-plugin   # one-time
```

**Terminal 1 — GDS** (from the soak *deployment* directory), reads this
directory's `fprime-gds.yml` (uart `/dev/cu.usbmodem11101`, framing
`space-packet-fprime`, TTS **52051**, chunk 100, cooldown 1.00):

```bash
cd ~/InternshipWork/soak-testing/fprime-soak-test-reference
source fprime-venv/bin/activate
cd FprimeSoakTestReference/FprimeSoakTestReferenceDeployment
fprime-gds --uart-device /dev/cu.usbmodem11101 --framing-selection space-packet-fprime
```

Wait until `logs/fprime-gds-*/comm.py.log` shows `APID 4`.

**Terminal 2 — pytest** (soak repo root, same venv):

```bash
cd ~/InternshipWork/soak-testing/fprime-soak-test-reference
source fprime-venv/bin/activate
pytest -o python_files='test_*.py' -v -rs \
  FprimeSoakTestReference/FprimeSoakTestReferenceDeployment/test/int \
  --dictionary ./build-artifacts/aarch64-linux/FprimeSoakTestReference_FprimeSoakTestReferenceDeployment/dict/FprimeSoakTestReferenceDeploymentTopologyDictionary.json \
  --deployment-config FprimeSoakTestReference/FprimeSoakTestReferenceDeployment/test/int/int_config.json
```

`pytest.ini` supplies chunk 100, cooldown 1.00, and `--tts-port 52051`. A
`RateGroupCycleSlip` means FSW lost `SCHED_RR` — restart after
`setcap cap_sys_nice=eip`. Restart GDS + FSW if GDS has been up for hours.

## Config knobs (`int_config.json`)

| Key | Role |
|-----|------|
| `soak.fsw_tmp` | FSW-side uplink destination dir (default `/tmp`) |
| `soak.cmd_timeout_s` | Command / EVR waits |
| `soak.uplink_timeout_s` | Single-chunk RF uplink wait |
| `soak.uplink_large_timeout_s` | Multi-chunk uplink wait |
| `soak.dp_*_timeout_s` | DP produce / xmit waits |
| `soak.dp_xmit_bytes_per_s` | Effective DP downlink rate used to scale the catalog drain timeout by pending bytes (default 300) |

DP serialize duty state is stored at
`~/.fprime-soak-${DEPLOYMENT_NAME}-dp-serialize` (`on`/`off`).
