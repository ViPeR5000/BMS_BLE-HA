"""Module to support Jikong Smart BMS."""

import asyncio
from collections.abc import Callable
import logging
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak.uuids import normalize_uuid_str

from ..const import (
    ATTR_BATTERY_CHARGING,
    ATTR_BATTERY_LEVEL,
    ATTR_CURRENT,
    ATTR_CYCLE_CAP,
    ATTR_CYCLE_CHRG,
    ATTR_CYCLES,
    ATTR_POWER,
    ATTR_RUNTIME,
    ATTR_TEMPERATURE,
    ATTR_VOLTAGE,
)
from .basebms import BaseBMS

BAT_TIMEOUT = 10
LOGGER = logging.getLogger(__name__)

# setup UUIDs, e.g. for receive: '0000fff1-0000-1000-8000-00805f9b34fb'
UUID_CHAR = normalize_uuid_str("ffe1")
UUID_SERVICE = normalize_uuid_str("ffe0")


class BMS(BaseBMS):
    """Jikong Smart BMS class implementation."""

    HEAD = bytes([0x55, 0xAA, 0xEB, 0x90])
    HEAD_LEN = 4
    INFO_LEN = 300

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Intialize private BMS members."""
        self._reconnect = reconnect
        self._ble_device = ble_device
        assert self._ble_device.name is not None
        self._client: BleakClient | None = None
        self._data: bytearray | None = None
        self._data_event = asyncio.Event()
        self._connected = False  # flag to indicate active BLE connection
        self._char_write_handle: int | None = None
        self._FIELDS: list[tuple[str, int, int, bool, Callable[[int], int | float]]] = [
            (ATTR_TEMPERATURE, 144, 2, True, lambda x: float(x / 10)),
            (ATTR_VOLTAGE, 150, 4, False, lambda x: float(x / 1000)),
            (ATTR_CURRENT, 170, 2, False, lambda x: float(x / 1000)),
            (ATTR_BATTERY_LEVEL, 173, 1, False, lambda x: int(x)),
            (ATTR_CYCLE_CHRG, 174, 4, False, lambda x: float(x / 1000)),
            (ATTR_CYCLES, 182, 4, False, lambda x: int(x)),
        ]  # Protocol: JK02_32S; JK02_24S has offset -32

    @staticmethod
    def matcher_dict_list() -> list[dict[str, Any]]:
        """Provide BluetoothMatcher definition."""
        return [
            {
                "service_uuid": UUID_SERVICE,
                "connectable": True,
                "manufacturer_id": 0x00F0,
            },
            {
                "service_uuid": UUID_SERVICE,
                "connectable": True,
                "manufacturer_id": 0x0B65,
            },
            {
                "service_uuid": UUID_SERVICE,
                "connectable": True,
                "manufacturer_id": 0xEAC1,
            },
            {
                "service_uuid": UUID_SERVICE,
                "connectable": True,
                "manufacturer_id": 0x2D0B,
            },
            {
                "service_uuid": UUID_SERVICE,
                "connectable": True,
                "manufacturer_id": 0x5844,
            },
            {
                "service_uuid": UUID_SERVICE,
                "connectable": True,
                "manufacturer_id": 0x4B4A,
            },
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "Jikong", "model": "Smart BMS"}

    async def _wait_event(self) -> None:
        await self._data_event.wait()
        self._data_event.clear()

    def _on_disconnect(self, client: BleakClient) -> None:
        """Disconnect callback function."""

        LOGGER.debug("Disconnected from BMS (%s)", client.address)
        self._connected = False

    def _notification_handler(self, sender, data: bytearray) -> None:
        LOGGER.debug("Received BLE data: %s", data)
        if data[0:4] == self.HEAD:
            LOGGER.debug("Response start")
            self._data = data
        elif len(data) and self._data is not None:
            self._data += data

        # verify that data long enough and if answer is cell info (0x2)
        if (
            self._data is None
            or len(self._data) < self.INFO_LEN
            or self._data[4] != 0x2
        ):
            return

        crc = self.crc(self._data[0 : self.INFO_LEN - 1])
        if self._data[self.INFO_LEN - 1] != crc:
            LOGGER.debug(
                "Response data CRC is invalid: %i != %i",
                self._data[self.INFO_LEN - 1],
                self.crc(self._data[0 : self.INFO_LEN - 1]),
            )
            self._data = None  # reset invalid data

        self._data_event.set()

    async def _connect(self) -> None:
        """Connect to the BMS and setup notification if not connected."""

        if not self._connected:
            LOGGER.debug("Connecting BMS (%s)", self._ble_device.name)
            self._client = BleakClient(
                self._ble_device,
                disconnected_callback=self._on_disconnect,
                services=[UUID_SERVICE],
            )
            await self._client.connect()
            char_notify_handle: int | None = None
            self._char_write_handle = None
            for service in self._client.services:
                for char in service.characteristics:
                    LOGGER.debug(
                        "Discovered characteristic %s: %s", char.uuid, char.properties
                    )
                    if char.uuid == UUID_CHAR:
                        if "notify" in char.properties:
                            char_notify_handle = char.handle
                        if (
                            "write" in char.properties
                            or "write-without-response" in char.properties
                        ):
                            self._char_write_handle = char.handle
            if char_notify_handle is None or self._char_write_handle is None:
                LOGGER.debug("Failed to detect characteristics")
                await self._client.disconnect()
                return
            await self._client.start_notify(
                char_notify_handle or 0, self._notification_handler
            )
            self._connected = True
        else:
            LOGGER.debug("BMS %s already connected", self._ble_device.name)

    async def disconnect(self) -> None:
        """Disconnect the BMS and includes stoping notifications."""

        if self._client and self._connected:
            LOGGER.debug("Disconnecting BMS (%s)", self._ble_device.name)
            try:
                self._data_event.clear()
                await self._client.disconnect()
            except BleakError:
                LOGGER.warning("Disconnect failed!")

        self._client = None

    def crc(self, frame: bytes):
        """Calculate Jikong frame CRC."""
        return sum(frame) & 0xFF

    def cmd(self, cmd: bytes, value: list[int] | None = None) -> bytes:
        """Assemble a Jikong BMS command."""
        if value is None:
            value = []
        assert len(value) <= 13
        frame = bytes([*self.HEAD, cmd[0]])
        frame += bytes([len(value), *value])
        frame += bytes([0] * (13 - len(value)))
        frame += bytes([self.crc(frame)])
        return frame

    async def async_update(self) -> dict[str, int | float | bool]:
        """Update battery status information."""
        await self._connect()
        assert self._client is not None
        if not self._connected:
            LOGGER.debug("Update request, but device not connected")
            return {}

        # query device info
        await self._client.write_gatt_char(
            self._char_write_handle or 0, data=self.cmd(b"\x97")
        )

        # query cell info
        await self._client.write_gatt_char(
            self._char_write_handle or 0, data=self.cmd(b"\x96")
        )

        await asyncio.wait_for(self._wait_event(), timeout=BAT_TIMEOUT)

        if self._data is None:
            return {}
        if len(self._data) != self.INFO_LEN:
            LOGGER.debug("Wrong data length (%i): %s", len(self._data), self._data)

        data = {
            key: func(
                int.from_bytes(
                    self._data[idx : idx + size], byteorder="little", signed=sign
                )
            )
            for key, idx, size, sign, func in self._FIELDS
        }

        self.calc_values(
            data, {ATTR_POWER, ATTR_BATTERY_CHARGING, ATTR_CYCLE_CAP, ATTR_RUNTIME}
        )

        if self._reconnect:
            # disconnect after data update to force reconnect next time (slow!)
            await self.disconnect()

        return data
