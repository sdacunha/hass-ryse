"""Coordinator for RYSE SmartShade.

Listens to BLE advertisements for state, connects on demand to issue
commands, disconnects right after. No persistent connection, no
keepalive, no proxy pinning — trust HA's bluetooth scoring and
bleak-retry-connector to route through the right adapter/proxy.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time

from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .ryse import RyseDevice

_LOGGER = logging.getLogger(__name__)


class RyseCoordinator(ActiveBluetoothDataUpdateCoordinator):
    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        device: RyseDevice,
        name: str,
        entry_id: str | None = None,
    ):
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            address=address,
            needs_poll_method=self._needs_poll,
            poll_method=self._noop_poll,
            mode=bluetooth.BluetoothScanningMode.PASSIVE,
            connectable=True,
        )
        self.device = device
        self._name = name
        self._entry_id = entry_id
        self._position: int | None = None
        self._battery: int | None = None
        self._available = False
        self._adv_count = 0
        self._adv_first_ts: float | None = None
        self._adv_last_ts: float | None = None
        self._adv_last_summary_ts: float = 0.0
        self._adv_sources: dict[str, int] = {}  # scanner source MAC → count
        self.device.add_position_callback(self._handle_position_notification)

    def async_start(self):
        # Battery shades advertise infrequently — give the availability
        # tracker generous slack before marking unavailable.
        bluetooth.async_set_fallback_availability_interval(self.hass, self.address, 900.0)
        return super().async_start()

    # ------------------------------------------------------------------
    # Advertisement handling
    # ------------------------------------------------------------------

    @callback
    def _async_handle_bluetooth_event(self, service_info, change):
        now = time.monotonic()
        self._adv_count += 1
        if self._adv_first_ts is None:
            self._adv_first_ts = now
        gap_ms = (now - self._adv_last_ts) * 1000 if self._adv_last_ts is not None else None
        self._adv_last_ts = now
        source = getattr(service_info, "source", None)
        if source:
            self._adv_sources[source] = self._adv_sources.get(source, 0) + 1
        rssi = getattr(service_info, "rssi", None)
        # Per-adv detail at debug only; periodic summary at info so it's visible.
        _LOGGER.debug(
            "[%s] ADV #%d rssi=%s source=%s gap=%s position=%s battery=%s",
            self._name,
            self._adv_count,
            rssi,
            source,
            f"{gap_ms:.0f}ms" if gap_ms is not None else "first",
            getattr(service_info, "manufacturer_data", {}),
            None,
        )
        if now - self._adv_last_summary_ts > 60:
            elapsed = now - self._adv_first_ts if self._adv_first_ts else 1
            rate = self._adv_count / max(elapsed, 1)
            _LOGGER.info(
                "[%s] adv stats: %d total in %.0fs (%.2f/sec), sources=%s",
                self._name,
                self._adv_count,
                elapsed,
                rate,
                self._adv_sources,
            )
            self._adv_last_summary_ts = now

        if hasattr(service_info, "device") and service_info.device:
            self.device.set_ble_device(service_info.device)
        adv = self.device.parse_advertisement(service_info)
        if adv.get("position") is not None:
            self._position = adv["position"]
        if adv.get("battery") is not None:
            self._battery = adv["battery"]
            for cb in self.device._battery_callbacks:
                if inspect.iscoroutinefunction(cb):
                    self.hass.async_create_task(cb(adv["battery"]))
                else:
                    cb(adv["battery"])
        for cb in self.device._adv_callbacks:
            if inspect.iscoroutinefunction(cb):
                self.hass.async_create_task(cb())
            else:
                cb()
        self._available = True
        self.async_update_listeners()

    @callback
    def _async_handle_unavailable(self, service_info):
        _LOGGER.warning("[Coordinator] %s is unavailable (%s)", self._name, self.address)
        self._available = False
        self.async_update_listeners()

    @callback
    def _handle_position_notification(self, position: int) -> None:
        """Real-time position update from a GATT notification while we're connected."""
        _LOGGER.info("[%s] position notification: %d", self._name, position)
        self._position = position
        self._available = True
        self.async_update_listeners()

    # ------------------------------------------------------------------
    # Polling — disabled. Advertisements carry position + battery, so
    # there's no need for a periodic GATT poll. The base class still
    # wants the callbacks to exist.
    # ------------------------------------------------------------------

    @callback
    def _needs_poll(self, service_info, seconds_since_last_poll):
        return False

    async def _noop_poll(self, service_info):
        return

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    async def _execute_command(self, op, *args) -> None:
        """Connect → run op → disconnect. One retry on transient failure."""
        for attempt in range(2):
            ble_device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
            if not ble_device:
                _LOGGER.warning("[Coordinator] No BLE device for %s", self._name)
                self._available = False
                self.async_update_listeners()
                return

            self.device.set_ble_device(ble_device)
            if not await self.device.connect():
                if attempt == 0:
                    await asyncio.sleep(2)
                    continue
                self._available = False
                self.async_update_listeners()
                return

            try:
                await op(*args)
                ir.async_delete_issue(self.hass, DOMAIN, f"ble_auth_failed_{self.address}")
                await self.device.disconnect()
                return
            except (BleakError, ConnectionError) as e:
                err_str = str(e)
                _LOGGER.warning(
                    "[Coordinator] Command failed for %s (attempt %d): %s",
                    self._name,
                    attempt + 1,
                    e,
                )
                if "Insufficient authentication" in err_str:
                    ir.async_create_issue(
                        self.hass,
                        DOMAIN,
                        f"ble_auth_failed_{self.address}",
                        is_fixable=True,
                        severity=ir.IssueSeverity.ERROR,
                        translation_key="ble_auth_failed",
                        translation_placeholders={"name": self._name},
                    )
                await self.device.disconnect()
                if attempt == 0:
                    await asyncio.sleep(2)

        # Both attempts failed
        self._available = False
        self.async_update_listeners()

    async def async_set_position(self, position: int) -> None:
        await self._execute_command(self.device.set_position, position)

    async def async_open_cover(self) -> None:
        if self._position is not None and self._position == 0:
            return
        await self._execute_command(self.device.open)

    async def async_close_cover(self) -> None:
        if self._position is not None and self._position == 100:
            return
        await self._execute_command(self.device.close)

    # ------------------------------------------------------------------
    # Properties used by entity classes
    # ------------------------------------------------------------------

    @property
    def position(self) -> int | None:
        return self._position

    @property
    def battery(self) -> int | None:
        return self._battery

    @property
    def available(self) -> bool:
        return self._available

    @property
    def initializing(self) -> bool:
        # No init phase anymore — adv-driven, state shows up when it shows up.
        return False

    @property
    def name(self) -> str:
        return self._name

    async def async_update_battery(self, battery_level: int | None) -> None:
        self._battery = battery_level
        self.async_update_listeners()
