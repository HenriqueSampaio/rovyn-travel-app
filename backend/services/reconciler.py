"""The settlement reconciler — the safety net for a process that died or a
provider that timed out mid-settlement.

Every provider call the settlement makes is preceded by a persisted cursor
(which step, which item, which attempt reference). If the thread dies, the
cursor says exactly what might be half-done. The reconciler runs at startup
and every few minutes: it resolves the in-flight call by READING the provider
(same client reference / metadata), never by repeating money-moving calls,
then lets the settlement continue. What it cannot resolve becomes
``needs_attention`` with an email to the leader — no capture, cancel or refund
is issued on a guess.
"""
import os
import threading
import time

from backend.services import booking_service

INTERVAL_SECONDS = 300
STALE_AFTER_SECONDS = 600
_thread: threading.Thread | None = None
_stop = threading.Event()


def enabled() -> bool:
    return (os.environ.get("ROVYN_RECONCILER") or "1").strip() != "0"


def reconcile_all() -> list[dict]:
    """One pass over every stuck settlement. Safe to call any time."""
    results = []
    for settlement_id in booking_service.stuck_settlement_ids(STALE_AFTER_SECONDS):
        try:
            results.append(booking_service.reconcile(settlement_id))
        except Exception as exc:  # one bad settlement must not stop the sweep
            results.append({"settlement_id": settlement_id, "error": str(exc)})
    return results


def _loop() -> None:
    while not _stop.wait(INTERVAL_SECONDS):
        try:
            reconcile_all()
        except Exception:
            pass


def start() -> None:
    """Start the background sweep once per process (no-op when disabled)."""
    global _thread
    if not enabled() or (_thread is not None and _thread.is_alive()):
        return
    try:
        reconcile_all()
    except Exception:
        pass
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="settlement-reconciler")
    _thread.start()


def stop() -> None:
    _stop.set()
