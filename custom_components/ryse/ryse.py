"""Minimal BLE wrapper for the RYSE SmartShade.

Connect on demand, write/read GATT, disconnect when done. No persistent
connection management, no keepalive, no proxy pinning — that lives in
the coordinator (or doesn't exist at all). HA's bluetooth integration
and bleak-retry-connector handle adapter routing.
"""

from __future__ import annotations

import asyncio
import logging
import time

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakNotFoundError, establish_connection

from .const import (
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_MAX_RETRY_ATTEMPTS,
    HARDCODED_UUIDS,
)

_LOGGER = logging.getLogger(__name__)

POSITION_CHAR_UUID = HARDCODED_UUIDS["rx_uuid"]
COMMAND_CHAR_UUID = HARDCODED_UUIDS["tx_uuid"]


class RyseDevice:
    """Thin async wrapper around a RYSE BLE peripheral."""

    def __init__(self, address: str):
        self.address = address
        self.ble_device: BLEDevice | None = None
        self.client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._connection_timeout = DEFAULT_CONNECTION_TIMEOUT
        self._max_retry_attempts = DEFAULT_MAX_RETRY_ATTEMPTS
        self._position_callbacks: list = []
        self._battery_callbacks: list = []
        self._adv_callbacks: list = []
        self._disconnect_callbacks: list = []

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def add_position_callback(self, cb):
        self._position_callbacks.append(cb)

    def add_battery_callback(self, cb):
        self._battery_callbacks.append(cb)

    def add_adv_callback(self, cb):
        self._adv_callbacks.append(cb)

    def add_disconnect_callback(self, cb):
        self._disconnect_callbacks.append(cb)

    # Retained for backward compat with cover/sensor wiring; no-op here.
    def add_unavailable_callback(self, _cb):
        pass

    # ------------------------------------------------------------------
    # State accessors
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    def set_ble_device(self, ble_device: BLEDevice | None) -> None:
        self.ble_device = ble_device

    def get_battery_level(self) -> int | None:
        """Provided for entities that cached this on the device; we no longer track it here."""
        return None

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def _handle_notification(self, _sender, data: bytearray) -> None:
        """Parse RYSE GATT notifications.

        Packet format:
          data[0] == 0xF5  (header)
          data[2] == 0x01
          data[3] == 0x07  → position report, position at data[4]
          data[3] == 0x18  → user target report (ignore)
        """
        if len(data) < 5 or data[0] != 0xF5:
            return
        if data[2] == 0x01 and data[3] == 0x07:
            position = data[4]
            for cb in self._position_callbacks:
                try:
                    cb(position)
                except Exception:
                    _LOGGER.exception("[%s] Position callback error", self.address)

    def _on_disconnected(self, client: BleakClient) -> None:
        """Bleak fires this on link drop. Mark ourselves disconnected and notify."""
        if self.client is not client:
            return  # already torn down via explicit disconnect()
        lifetime_s = time.monotonic() - getattr(self, "_connected_at", time.monotonic())
        _LOGGER.warning(
            "[%s] UNEXPECTED disconnect after %.1fs of connected lifetime — device dropped us",
            self.address,
            lifetime_s,
        )
        self.client = None
        for cb in self._disconnect_callbacks:
            try:
                cb()
            except Exception:
                _LOGGER.exception("[%s] Disconnect callback error", self.address)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        async with self._lock:
            if self.is_connected:
                return True
            if not self.ble_device:
                _LOGGER.error("[%s] No BLEDevice available for connection", self.address)
                return False
            adapter_source = None
            details = getattr(self.ble_device, "details", None)
            if isinstance(details, dict):
                adapter_source = details.get("source")
            t0 = time.monotonic()
            try:
                self.client = await establish_connection(
                    BleakClient,
                    self.ble_device,
                    self.address,
                    max_attempts=self._max_retry_attempts,
                    timeout=self._connection_timeout,
                    disconnected_callback=self._on_disconnected,
                )
            except BleakNotFoundError:
                _LOGGER.warning("[%s] Device not found (via %s)", self.address, adapter_source)
                return False
            except Exception as e:
                _LOGGER.warning(
                    "[%s] Connection failed after %.0fms via %s: %s",
                    self.address,
                    (time.monotonic() - t0) * 1000,
                    adapter_source,
                    e,
                )
                return False

            connect_ms = (time.monotonic() - t0) * 1000

            if not self.client.is_connected:
                _LOGGER.warning("[%s] Connect returned but is_connected=False (took %.0fms)", self.address, connect_ms)
                return False

            self._connected_at = time.monotonic()
            _LOGGER.info(
                "[%s] Connected in %.0fms via %s (mtu=%s)",
                self.address,
                connect_ms,
                adapter_source,
                getattr(self.client, "mtu_size", "?"),
            )

            try:
                await self.client.start_notify(POSITION_CHAR_UUID, self._handle_notification)
                _LOGGER.debug("[%s] subscribed to notifications", self.address)
            except Exception as e:
                _LOGGER.debug("[%s] start_notify failed: %s", self.address, e)
            return True

    async def disconnect(self) -> None:
        async with self._lock:
            client = self.client
            self.client = None
            if client and client.is_connected:
                lifetime_s = time.monotonic() - getattr(self, "_connected_at", time.monotonic())
                t0 = time.monotonic()
                try:
                    await client.disconnect()
                    _LOGGER.info(
                        "[%s] Disconnected cleanly after %.1fs (disconnect took %.0fms)",
                        self.address,
                        lifetime_s,
                        (time.monotonic() - t0) * 1000,
                    )
                except Exception as e:
                    _LOGGER.debug("[%s] Error during disconnect: %s", self.address, e)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def set_position(self, position: int) -> None:
        if not 0 <= position <= 100:
            raise ValueError("position must be between 0 and 100")
        data = bytes([0xF5, 0x03, 0x01, 0x01, position])
        checksum = sum(data[2:]) % 256
        await self.write_gatt(COMMAND_CHAR_UUID, data + bytes([checksum]))

    async def open(self) -> None:
        await self.set_position(0)

    async def close(self) -> None:
        await self.set_position(100)

    async def read_gatt(self, char_uuid: str) -> bytes | None:
        if not self.is_connected:
            raise ConnectionError("Not connected to device")
        return await self.client.read_gatt_char(char_uuid)

    async def write_gatt(self, char_uuid: str, data: bytes) -> None:
        if not self.is_connected:
            raise ConnectionError("Not connected to device")
        t0 = time.monotonic()
        await self.client.write_gatt_char(char_uuid, data)
        _LOGGER.debug(
            "[%s] GATT write to %s: %s (%.0fms)",
            self.address,
            char_uuid,
            data.hex(),
            (time.monotonic() - t0) * 1000,
        )

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def parse_advertisement(service_info) -> dict:
        """Extract position + battery from RYSE manufacturer data (mfr_id 0x0409)."""
        result: dict = {}
        for mfr_id, data in getattr(service_info, "manufacturer_data", {}).items():
            if mfr_id not in (0x0409, 0x409):
                continue
            if len(data) >= 3:
                result["position"] = data[1]
                result["battery"] = data[2]
        return result
