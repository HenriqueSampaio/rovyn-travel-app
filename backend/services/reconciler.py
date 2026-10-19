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


def reconcile_all(stale_after_seconds: int = STALE_AFTER_SECONDS) -> list[dict]:
    """One pass: open rounds past their group deadline are released, releases
    Stripe never confirmed are finished, then every stuck settlement is
    resolved. Safe to call any time."""
    results = []
    for trip_id in booking_service.overdue_open_trip_ids():
        try:
            if booking_service.expire_if_due(trip_id):
                results.append({"trip_id": trip_id, "expired": True})
        except Exception as exc:
            results.append({"trip_id": trip_id, "error": str(exc)})
    for attempt_id in booking_service.releasing_attempt_ids(stale_after_seconds):
        try:
            results.append(booking_service.resolve_releasing(attempt_id))
        except Exception as exc:
            results.append({"attempt_id": attempt_id, "error": str(exc)})
    for settlement_id in booking_service.stuck_settlement_ids(stale_after_seconds):
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
    """Start the background sweep once per process (no-op when disabled).
    The boot pass uses no staleness: nothing can still be running from the
    previous process, so an interrupted round resumes right away."""
    global _thread
    if not enabled() or (_thread is not None and _thread.is_alive()):
        return
    try:
        reconcile_all(stale_after_seconds=0)
    except Exception:
        pass
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="settlement-reconciler")
    _thread.start()


def stop() -> None:
    _stop.set()
