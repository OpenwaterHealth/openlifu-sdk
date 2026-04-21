from __future__ import annotations

import logging
import threading

log = logging.getLogger("OWSignal")


class OWSignal:
    def __init__(self):
        self._slots = []
        self._lock = threading.Lock()

    def connect(self, slot):
        if callable(slot):
            with self._lock:
                if slot not in self._slots:
                    self._slots.append(slot)

    def disconnect(self, slot):
        with self._lock:
            try:
                self._slots.remove(slot)
            except ValueError:
                pass

    def disconnect_all(self):
        with self._lock:
            self._slots.clear()

    def emit(self, *args, **kwargs):
        with self._lock:
            slots = list(self._slots)
        for slot in slots:
            try:
                slot(*args, **kwargs)
            except Exception:
                log.exception("Error in signal slot")
