from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.active_update_coordinator import ActiveBluetoothDataUpdateCoordinator
from homeassistant.core import HomeAssistant, callback
from datetime import datetime, timedelta
import asyncio
import logging
import inspect
from .ryse import RyseDevice
from .const import HARDCODED_UUIDS, DEFAULT_INIT_TIMEOUT

_LOGGER = logging.getLogger(__name__)

class RyseCoordinator(ActiveBluetoothDataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, address: str, device: RyseDevice, name: str):
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            address=address,
            needs_poll_method=self._needs_poll,
            poll_method=self._async_update,
            mode=bluetooth.BluetoothScanningMode.ACTIVE,
            connectable=True,
        )
        self.device = device
        self._name = name
        self._position = None
        self._battery = None
        self._last_adv = None
        # Always start as unavailable and initializing until a valid adv or GATT poll
        self._available = False
        self._initializing = True
        self._ready_event = asyncio.Event()
        self._was_unavailable = True
        self._unavailable_cancel = bluetooth.async_track_unavailable(
            hass, self._handle_unavailable, address, connectable=True
        )
        self._adv_cancel = bluetooth.async_register_callback(
            hass, self._handle_adv, {"address": address}, bluetooth.BluetoothScanningMode.ACTIVE
        )
        # Start the initialization timer
        self.hass.async_create_task(self._async_init_timeout())

    async def _async_init_timeout(self):
        await asyncio.sleep(DEFAULT_INIT_TIMEOUT)
        if self._initializing:
            self._initializing = False
            if not self._available:
                _LOGGER.info(f"Device {self._name} did not become available after {DEFAULT_INIT_TIMEOUT}s, marking as unavailable.")
            self.async_update_listeners()

    @callback
    def _handle_adv(self, service_info, change):
        # Always update BLE device reference from latest adv
        if hasattr(service_info, 'device') and service_info.device:
            self.device.set_ble_device(service_info.device)
        adv = self.device.parse_advertisement(service_info)
        if adv.get('position') is not None:
            self._position = adv['position']
            self._ready_event.set()  # Mark as ready if we get a valid adv
        if adv.get('battery') is not None:
            self._battery = adv['battery']
            # Call battery callbacks with the new battery value
            for callback in self.device._battery_callbacks:
                if inspect.iscoroutinefunction(callback):
                    self.hass.async_create_task(callback(adv['battery']))
                else:
                    callback(adv['battery'])
            # Call advertisement callbacks
            for callback in self.device._adv_callbacks:
                if inspect.iscoroutinefunction(callback):
                    self.hass.async_create_task(callback())
                else:
                    callback()
        self._last_adv = datetime.now()
        # Only set available to True if we get a valid adv
        self._available = True
        if self._initializing:
            self._initializing = False
        if self._was_unavailable:
            _LOGGER.info(f"Device {self._name} is online")
            self._was_unavailable = False
        self.async_update_listeners()

    @callback
    def _handle_unavailable(self, service_info):
        """Handle when device stops advertising.

        For battery-powered devices, we're more lenient - they may stop advertising
        to save power but are still reachable. Only mark as truly unavailable if
        we haven't heard from them in a very long time (15 minutes).
        """
        # If we've received an advertisement recently, don't mark as unavailable yet
        if self._last_adv:
            time_since_adv = datetime.now() - self._last_adv
            # Be lenient - battery devices may not advertise frequently
            if time_since_adv < timedelta(minutes=15):
                _LOGGER.debug(f"[Coordinator] {self._name} stopped advertising but was seen {time_since_adv.total_seconds():.0f}s ago, keeping as available")
                return

        _LOGGER.warning(f"[Coordinator] _handle_unavailable called for {self._name} (address: {self.device.address})")
        self._available = False
        self._was_unavailable = True
        self.async_update_listeners()

    @callback
    def _needs_poll(self, service_info, seconds_since_last_poll):
        ble_device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        should_poll = (
            self.hass.state == self.hass.CoreState.running and
            self.device.poll_needed(seconds_since_last_poll)
            and bool(ble_device)
        )
        _LOGGER.debug(
            "[Coordinator] _needs_poll called: seconds_since_last_poll=%s, has_ble_device=%s, should_poll=%s, position=%s, battery=%s",
            seconds_since_last_poll, bool(ble_device), should_poll, self._position, self._battery
        )
        return should_poll

    async def _async_update(self, service_info):
        _LOGGER.debug("[Coordinator] _async_update called for %s (address: %s)", self._name, self.address)
        ble_device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if not ble_device:
            _LOGGER.warning("[Coordinator] No BLE device found for %s during poll", self._name)
            self._available = False
            self._was_unavailable = True
            self.async_update_listeners()
            return
        self.device.set_ble_device(ble_device)
        if not await self.device.connect():
            _LOGGER.warning("[Coordinator] Could not connect to %s during poll", self._name)
            self._available = False
            self._was_unavailable = True
            self.async_update_listeners()
            return
        try:
            data = await self.device.read_gatt(HARDCODED_UUIDS["rx_uuid"])
            _LOGGER.debug("[Coordinator] GATT poll data for %s: %s", self._name, data)
            if len(data) >= 3:
                self._position = data[1]
                self._battery = data[2]
                self._available = True
                if self._initializing:
                    self._initializing = False
                if self._was_unavailable:
                    _LOGGER.info(f"Device {self._name} is online (via GATT poll)")
                    self._was_unavailable = False
        except Exception as e:
            _LOGGER.error(f"[Coordinator] GATT poll failed: {e}")
            self._available = False
            self._was_unavailable = True
            self.async_update_listeners()
        self.async_update_listeners()

    async def async_wait_ready(self, timeout=30):
        try:
            async with asyncio.timeout(timeout):
                await self._ready_event.wait()
                return True
        except TimeoutError:
            return False

    @property
    def position(self):
        return self._position

    @property
    def battery(self):
        return self._battery

    @property
    def available(self):
        return self._available

    @property
    def initializing(self):
        return self._initializing

    async def async_update_battery(self, battery_level: int | None) -> None:
        """Update the battery level and notify listeners."""
        self._battery = battery_level
        self.async_update_listeners()

    @property
    def name(self):
        return self._name

    async def _ensure_connected(self) -> bool:
        """Ensure the device is connected before performing operations."""
        # Capture client ref to avoid race with _on_disconnected callback
        client = self.device.client
        if not client or not client.is_connected:
            ble_device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
            if not ble_device:
                self._available = False
                self._was_unavailable = True
                self.async_update_listeners()
                return False
            self.device.set_ble_device(ble_device)
            if not await self.device.connect():
                self._available = False
                self._was_unavailable = True
                self.async_update_listeners()
                return False
        return True

    async def async_set_position(self, position: int) -> None:
        """Set the cover position."""
        if await self._ensure_connected():
            await self.device.set_position(position)

    async def async_open_cover(self) -> None:
        """Open the cover."""
        if self.position is not None and self.position == 0:
            return
        if await self._ensure_connected():
            await self.device.open()

    async def async_close_cover(self) -> None:
        """Close the cover."""
        if self.position is not None and self.position == 100:
            return
        if await self._ensure_connected():
            await self.device.close() 