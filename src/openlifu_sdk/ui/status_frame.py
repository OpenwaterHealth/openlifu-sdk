"""STATUS-frame parser shared by real firmware and the simulator.

The firmware (and :class:`~openlifu_sdk.ui.simulated_interface.SimulatedTxDevice`)
emits unsolicited single-line STATUS frames over the TX UART during
sonication, formatted as::

    STATUS:<status>,MODE:<mode>,PULSE_TRAIN:[<curr>/<total>],PULSE:[<curr>/<total>],TEMP_TX:<f>,TEMP_AMBIENT:<f>

Some older firmware images omit the ``PULSE:[..]`` segment. Both shapes
are accepted by :func:`parse_status_string`.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

_RE_WITH_PULSE = re.compile(
    r"STATUS:(\w+),"
    r"MODE:(\w+),"
    r"PULSE_TRAIN:\[(\d+)/(\d+)\],"
    r"PULSE:\[(\d+)/(\d+)\],"
    r"TEMP_TX:([0-9.]+),"
    r"TEMP_AMBIENT:([0-9.]+)"
)

_RE_WITHOUT_PULSE = re.compile(
    r"STATUS:(\w+),"
    r"MODE:(\w+),"
    r"PULSE_TRAIN:\[(\d+)/(\d+)\],"
    r"TEMP_TX:([0-9.]+),"
    r"TEMP_AMBIENT:([0-9.]+)"
)


def _empty_result() -> dict:
    return {
        "status": None,
        "mode": None,
        "pulse_train_current": None,
        "pulse_train_total": None,
        "pulse_current": None,
        "pulse_total": None,
        "pulse_train_percent": None,
        "pulse_percent": None,
        "temp_tx": None,
        "temp_ambient": None,
    }


def parse_status_string(status_str: str) -> dict:
    """Parse a STATUS frame into a dict of fields.

    Returns a dict with all fields set to ``None`` if the frame is
    unparseable (i.e. never raises). Always returns the same key set
    so QML consumers can read fields unconditionally.
    """
    result = _empty_result()
    if not status_str:
        return result
    try:
        match = _RE_WITH_PULSE.match(status_str.strip())
        if match:
            status, mode, pt_c, pt_t, p_c, p_t, t_tx, t_amb = match.groups()
            pt_c, pt_t = int(pt_c), int(pt_t)
            p_c, p_t = int(p_c), int(p_t)
            result.update(
                status=status,
                mode=mode,
                pulse_train_current=pt_c,
                pulse_train_total=pt_t,
                pulse_current=p_c,
                pulse_total=p_t,
                pulse_train_percent=(pt_c / pt_t * 100) if pt_t > 0 else 0,
                pulse_percent=(p_c / p_t * 100) if p_t > 0 else 0,
                temp_tx=float(t_tx),
                temp_ambient=float(t_amb),
            )
            return result

        match = _RE_WITHOUT_PULSE.match(status_str.strip())
        if not match:
            return result
        status, mode, pt_c, pt_t, t_tx, t_amb = match.groups()
        pt_c, pt_t = int(pt_c), int(pt_t)
        result.update(
            status=status,
            mode=mode,
            pulse_train_current=pt_c,
            pulse_train_total=pt_t,
            pulse_train_percent=(pt_c / pt_t * 100) if pt_t > 0 else 0,
            temp_tx=float(t_tx),
            temp_ambient=float(t_amb),
        )
        return result
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Failed to parse status string %r: %s", status_str, exc)
        return _empty_result()


def format_status_frame(
    pt_curr: int,
    pt_total: int,
    p_curr: int = 0,
    p_total: int = 0,
    temp_tx: float = 25.0,
    temp_amb: float = 25.0,
    status: str = "RUNNING",
    mode: str = "SEQUENCE",
) -> str:
    """Build a STATUS frame string matching :func:`parse_status_string`.

    Used by :class:`~openlifu_sdk.ui.simulated_interface.SimulatedTxDevice`
    and by tests that round-trip frames through the parser.
    """
    return (
        f"STATUS:{status},MODE:{mode},"
        f"PULSE_TRAIN:[{pt_curr}/{pt_total}],"
        f"PULSE:[{p_curr}/{p_total}],"
        f"TEMP_TX:{temp_tx:.2f},"
        f"TEMP_AMBIENT:{temp_amb:.2f}"
    )


__all__ = ["format_status_frame", "parse_status_string"]
