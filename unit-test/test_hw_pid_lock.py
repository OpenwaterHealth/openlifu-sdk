"""
Unit tests for the cross-process OPENLIFU_HW_INTERFACE_PID lock used by
LIFUInterface.

Run with:
    python -m pytest unit-test/test_hw_pid_lock.py -v
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from openlifu_sdk.io.exceptions import LIFUHardwareInUseError
from openlifu_sdk.io.LIFUInterface import (
    OPENLIFU_HW_INTERFACE_PID_ENV,
    LIFUInterface,
    _hw_pid_lock_read,
    _hw_pid_lock_write,
    _pid_alive,
)


def _make_interface() -> LIFUInterface:
    """Build a LIFUInterface that doesn't touch hardware."""
    return LIFUInterface(
        TX_test_mode=True,
        HV_test_mode=True,
        run_async=True,
    )


class PidAliveTests(unittest.TestCase):
    def test_current_process_is_alive(self):
        self.assertTrue(_pid_alive(os.getpid()))

    def test_zero_and_negative_are_not_alive(self):
        self.assertFalse(_pid_alive(0))
        self.assertFalse(_pid_alive(-1))

    def test_unlikely_pid_not_alive(self):
        self.assertFalse(_pid_alive(2_000_000_000))


class HwPidLockTests(unittest.TestCase):
    def setUp(self):
        # Snapshot whatever was there so we can restore on tearDown
        self._saved = _hw_pid_lock_read()
        _hw_pid_lock_write("")

    def tearDown(self):
        _hw_pid_lock_write(self._saved or "")

    def test_lock_set_on_init(self):
        self.assertEqual(_hw_pid_lock_read(), "")
        iface = _make_interface()
        try:
            self.assertEqual(_hw_pid_lock_read(), str(os.getpid()))
        finally:
            iface.close()

    def test_lock_cleared_on_close(self):
        iface = _make_interface()
        self.assertEqual(_hw_pid_lock_read(), str(os.getpid()))
        iface.close()
        self.assertEqual(_hw_pid_lock_read(), "")

    def test_blank_lock_treated_as_unset(self):
        _hw_pid_lock_write("")
        iface = _make_interface()
        try:
            self.assertEqual(_hw_pid_lock_read(), str(os.getpid()))
        finally:
            iface.close()

    def test_stale_pid_is_overwritten(self):
        _hw_pid_lock_write("2000000000")
        iface = _make_interface()
        try:
            self.assertEqual(_hw_pid_lock_read(), str(os.getpid()))
        finally:
            iface.close()

    def test_garbage_lock_is_overwritten(self):
        _hw_pid_lock_write("not-a-pid")
        iface = _make_interface()
        try:
            self.assertEqual(_hw_pid_lock_read(), str(os.getpid()))
        finally:
            iface.close()

    def test_same_pid_does_not_block(self):
        _hw_pid_lock_write(str(os.getpid()))
        iface = _make_interface()
        try:
            self.assertEqual(_hw_pid_lock_read(), str(os.getpid()))
        finally:
            iface.close()

    def test_live_other_pid_raises(self):
        if sys.platform == "win32":
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
            )
        try:
            time.sleep(0.2)
            self.assertTrue(_pid_alive(child.pid))
            _hw_pid_lock_write(str(child.pid))

            with self.assertRaises(LIFUHardwareInUseError) as ctx:
                _make_interface()
            self.assertEqual(ctx.exception.pid, child.pid)
            # The slot should be unchanged when the claim is rejected
            self.assertEqual(_hw_pid_lock_read(), str(child.pid))
        finally:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


class CrossProcessVisibilityTests(unittest.TestCase):
    """Verify that the lock value is actually visible in a *separate* Python
    process (i.e. it really is cross-process, not just within os.environ)."""

    def setUp(self):
        self._saved = _hw_pid_lock_read()
        _hw_pid_lock_write("")

    def tearDown(self):
        _hw_pid_lock_write(self._saved or "")

    def test_value_visible_to_separate_python(self):
        iface = _make_interface()
        try:
            my_pid = os.getpid()
            self.assertEqual(_hw_pid_lock_read(), str(my_pid))

            # Spawn a fresh Python process that calls _hw_pid_lock_read()
            # with the env var explicitly stripped from its environment, so
            # we KNOW the value comes from the persistent registry/file
            # store and not from os.environ inheritance.
            code = (
                "import sys; "
                f"sys.path.insert(0, r'{_SRC}'); "
                "from openlifu_sdk.io.LIFUInterface import _hw_pid_lock_read; "
                "print(_hw_pid_lock_read())"
            )
            child_env = {k: v for k, v in os.environ.items() if k != OPENLIFU_HW_INTERFACE_PID_ENV}
            result = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                timeout=30,
                env=child_env,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(result.stdout.strip(), str(my_pid))
        finally:
            iface.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
