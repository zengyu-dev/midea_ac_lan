"""Runtime patches for X13 fan-light (风扇灯) devices.

The upstream ``midealan`` x13 driver (midea-lan 2026.9.2) only exposes the
light and reads the light power byte from the wrong offset, so fan control
and correct light state tracking are injected here at import time. The patch
lives inside the custom component so it survives ``midealan`` re-installs by
the requirements mechanism.

Protocol notes (verified live against model M0200008, subtype 0, 2026-10-10):

* A4 status body layout (``body = frame[10:-1]``, starting with the ``0xA4``
  body byte):
    [1] brightness (0-255)       [2] color temperature (0-255)
    [3] effect index + 1         [5] light power (0/1)
    [6] fan speed (1-100)        [10] fan power (0/1)
  The upstream parser reads light power from index 8, which is a constant 1
  on this model, so the light state never tracked reality.
* Set frame = ``aa <len> 13 00*6 02 <reg> <val> 00 00 00 00 00 <crc>``
  (7-byte body). The device silently ignores 5-byte bodies for light
  registers, and every accepted set is answered with an ``<reg|0x80> 01``
  ACK frame.
* Registers: 0x01 light power (0/1), 0x03 color temperature, 0x04 brightness,
  0x31 fan power (0/1), 0x34 fan speed (1..100; the six gears are
  1, 21, 41, 61, 81, 100).
"""

from __future__ import annotations

import logging
from typing import Any

from midealan.device import DeviceType
from midealan.devices.x13 import Midea13Device
from midealan.devices.x13 import message as x13_message
from midealan.message import ListTypes, MessageRequest, MessageType

_LOGGER = logging.getLogger(__name__)

ATTR_FAN_POWER = "fan_power"
ATTR_FAN_SPEED = "fan_speed"

#: Fan speed percent values emitted by the device, one per gear (1..6).
FAN_SPEED_VALUES: tuple[int, ...] = (1, 21, 41, 61, 81, 100)

#: Fallback fan speed (gear 3) used when a plain turn-on has no remembered speed.
DEFAULT_X13_FAN_SPEED = 41

REGISTER_LIGHT_POWER = 0x01
REGISTER_COLOR_TEMPERATURE = 0x03
REGISTER_BRIGHTNESS = 0x04
REGISTER_FAN_POWER = 0x31
REGISTER_FAN_SPEED = 0x34

_STATUS_BODY_TYPE = 0xA4
_STATUS_LIGHT_POWER_INDEX = 5
_STATUS_FAN_SPEED_INDEX = 6
_STATUS_FAN_POWER_INDEX = 10


class _RegisterSet(MessageRequest):
    """Raw X13 register set frame (7-byte body: body_type + 6 payload bytes)."""

    def __init__(self, protocol_version: int, register: int, value: int) -> None:
        """Initialize a register set frame."""
        super().__init__(
            device_type=DeviceType.X13,
            protocol_version=protocol_version,
            message_type=MessageType.set,
            body_type=ListTypes(register),
        )
        self._value = value

    @property
    def _body(self) -> bytearray:
        return bytearray([self._value, 0x00, 0x00, 0x00, 0x00, 0x00])


def _parse_status_body(frame: bytes) -> dict[str, Any] | None:
    """Extract the fan/light state fields from a decrypted A4 status frame."""
    if len(frame) < 24 or frame[0] != 0xAA or frame[10] != _STATUS_BODY_TYPE:
        return None
    body = frame[10:-1]
    return {
        ATTR_FAN_SPEED: body[_STATUS_FAN_SPEED_INDEX],
        ATTR_FAN_POWER: body[_STATUS_FAN_POWER_INDEX] > 0,
        "power": body[_STATUS_LIGHT_POWER_INDEX] > 0,
    }


def _apply_patches() -> None:
    """Patch Midea13Device with fan attributes and corrected state parsing."""
    if getattr(Midea13Device, "_fan_light_patched", False):
        return

    orig_init = Midea13Device.__init__
    orig_process_message = Midea13Device.process_message
    orig_set_attribute = Midea13Device.set_attribute
    # ``_body`` is a property; keep the underlying getter to reuse the stock
    # body-type/value mapping and only pad the payload length.
    orig_message_set_body = x13_message.MessageSet._body.fget

    def new_init(self: Midea13Device, *args: Any, **kwargs: Any) -> None:
        orig_init(self, *args, **kwargs)
        self._attributes[ATTR_FAN_POWER] = False
        self._attributes[ATTR_FAN_SPEED] = 0

    def new_process_message(self: Midea13Device, msg: bytes) -> dict[str, Any]:
        status = orig_process_message(self, msg)
        parsed = _parse_status_body(bytes(msg))
        if parsed is not None:
            for key, value in parsed.items():
                self._attributes[key] = value
            status.update(parsed)
        return status

    def new_set_attribute(
        self: Midea13Device,
        attr: str,
        value: str | int | bool,
    ) -> None:
        if attr == ATTR_FAN_POWER:
            self.build_send(
                _RegisterSet(
                    self._message_protocol_version,
                    REGISTER_FAN_POWER,
                    0x01 if value else 0x00,
                ),
            )
            return
        if attr == ATTR_FAN_SPEED:
            speed = max(0, min(100, int(value)))
            self.build_send(
                _RegisterSet(
                    self._message_protocol_version,
                    REGISTER_FAN_SPEED,
                    speed,
                ),
            )
            return
        orig_set_attribute(self, attr, value)

    def new_message_set_body(self: Any) -> bytearray:
        # The device ignores the 4-byte payload of the stock driver and only
        # acts on 6-byte payloads; pad without touching the stock mapping.
        return orig_message_set_body(self) + bytearray([0x00, 0x00])

    Midea13Device.__init__ = new_init
    Midea13Device.process_message = new_process_message
    Midea13Device.set_attribute = new_set_attribute
    x13_message.MessageSet._body = property(new_message_set_body)
    Midea13Device._fan_light_patched = True
    _LOGGER.debug("X13 fan-light patches applied")


_apply_patches()
