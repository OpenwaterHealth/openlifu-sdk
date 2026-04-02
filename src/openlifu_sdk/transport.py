"""Transport protocol definition for openlifu-sdk.

:class:`Transport` is a :class:`typing.Protocol` that formalises the interface
expected by :class:`~openlifu_sdk.io.LIFUTXDevice.TxDevice` and
:class:`~openlifu_sdk.io.LIFUHVController.HVController`.

Any object that implements these attributes and methods — including the concrete
:class:`~openlifu_sdk.io.LIFUUart.LIFUUart` and any test double — can be passed
as the ``uart`` argument without inheriting from a base class.

Example — creating a minimal test double::

    from openlifu_sdk.transport import Transport

    class FakeTransport:
        demo_mode = True
        asyncMode = False

        def is_connected(self) -> bool:
            return True

        def check_usb_status(self) -> None:
            pass

        def send_packet(self, **kwargs):
            ...  # return a mock packet

        def clear_buffer(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

    assert isinstance(FakeTransport(), Transport)  # runtime check works
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    pass


@runtime_checkable
class Transport(Protocol):
    """Structural interface fulfilled by :class:`~openlifu_sdk.io.LIFUUart.LIFUUart`.

    Any class that exposes these attributes and methods is a valid transport.
    No explicit inheritance is required (duck typing / structural subtyping).
    """

    #: When ``True`` the transport simulates hardware responses without a real device.
    demo_mode: bool

    #: When ``True`` the transport operates in asynchronous (non-blocking) mode.
    asyncMode: bool

    def is_connected(self) -> bool:
        """Return ``True`` if the physical device port is open and connected."""
        ...

    def check_usb_status(self) -> None:
        """Probe USB ports and update the connection state."""
        ...

    def send_packet(self, **kwargs: Any) -> Any:
        """Send a packet to the device and return the response packet."""
        ...

    def clear_buffer(self) -> None:
        """Flush any pending data from the receive buffer."""
        ...

    def disconnect(self) -> None:
        """Close the underlying serial port."""
        ...
