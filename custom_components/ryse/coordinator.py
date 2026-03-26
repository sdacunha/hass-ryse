from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from datetime import datetime, timedelta
import asyncio
import logging
import inspect
from bleak.exc import BleakError
from .ryse import RyseDevice
from .const import DOMAIN, HARDCODED_UUIDS, DEFAULT_INIT_TIMEOUT

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
            poll_method=self._async_update,
            mode=bluetooth.BluetoothScanningMode.PASSIVE,
            connectable=True,
        )
        self.device = device
        self._name = name
        self._entry_id = entry_id
        self._position = None
        self._battery = None
        self._last_adv = None
        # Always start as unavailable and initializing until a valid adv or GATT poll
        self._available = False
        self._initializing = True
        self._ready_event = asyncio.Event()
        self._was_unavailable = True
        self._reconnect_task = None
        self._last_warm_connect_attempt = None
        self.device.add_disconnect_callback(self._handle_device_disconnected)
        self.device.add_position_callback(self._handle_position_notification)
        # Provide a callback so establish_connection can fetch a fresh BLEDevice
        # on each retry, automatically routing through the best available proxy.
        self.device._ble_device_callback = lambda: bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )

    def async_start(self):
        """Start the coordinator — called via entry.async_on_unload.

        Returns a cancel callback that cleans up all registrations.
        """
        # Battery-powered shades may advertise infrequently.  Tell HA's
        # availability tracker to use a generous fallback so the device
        # isn't marked unavailable between advertisements.
        bluetooth.async_set_fallback_availability_interval(
            self.hass,
            self.address,
            900.0,  # 15 minutes
        )
        cancel = super().async_start()
        # Start the initialization timer
        self._init_task = self.hass.async_create_task(self._async_init_timeout())
        # In active mode, establish connection on startup
        if self.device._active_mode:
            self._reconnect_task = self.hass.async_create_task(self._active_reconnect())

        def _cancel():
            cancel()
            self._cancel_reconnect_task()
            if hasattr(self, "_init_task") and not self._init_task.done():
                self._init_task.cancel()

        return _cancel

    def _cancel_reconnect_task(self):
        """Cancel any running reconnect task."""
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            self._reconnect_task = None

    async def _async_init_timeout(self):
        await asyncio.sleep(DEFAULT_INIT_TIMEOUT)
        if self._initializing:
            self._initializing = False
            if not self._available:
                _LOGGER.info(
                    f"Device {self._name} did not become available after {DEFAULT_INIT_TIMEOUT}s, marking as unavailable."
                )
            self.async_update_listeners()

    # ------------------------------------------------------------------
    # Base class overrides — these replace the manual callback registrations.
    # ActiveBluetoothDataUpdateCoordinator calls these automatically.
    # ------------------------------------------------------------------

    @callback
    def _async_handle_bluetooth_event(self, service_info, change):
        """Handle advertisement data from the base class callback."""
        # Always update BLE device reference from latest advertisement
        if hasattr(service_info, "device") and service_info.device:
            self.device.set_ble_device(service_info.device)
        adv = self.device.parse_advertisement(service_info)
        if adv.get("position") is not None:
            self._position = adv["position"]
            self._ready_event.set()
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
        self._last_adv = datetime.now()
        self._available = True
        if self._initializing:
            self._initializing = False
        if self._was_unavailable:
            _LOGGER.info(f"Device {self._name} is online")
            self._was_unavailable = False
        self.async_update_listeners()
        # Proactively reconnect in active mode when we hear an advertisement
        # and the connection is down (recovers from silent drops).
        if not self.device._is_connected and not self.device._connecting:
            if self._reconnect_task and not self._reconnect_task.done():
                return
            if self.device._active_mode:
                now = datetime.now()
                if self._last_warm_connect_attempt is None or (now - self._last_warm_connect_attempt) > timedelta(
                    seconds=60
                ):
                    _LOGGER.info(
                        "[Coordinator] Active mode: reconnecting on advertisement for %s",
                        self._name,
                    )
                    self.hass.async_create_task(self._warm_connect())

    @callback
    def _async_handle_unavailable(self, service_info):
        """Handle when device stops advertising (base class callback).

        HA's availability tracker already applies the fallback interval
        we set in async_start(), so this only fires after 15 minutes
        of silence.  No manual leniency check needed.
        """
        _LOGGER.warning(
            "[Coordinator] %s is unavailable (address: %s)",
            self._name,
            self.device.address,
        )
        self._available = False
        self._was_unavailable = True
        self.async_update_listeners()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def _warm_connect(self):
        """Proactively connect after hearing an advertisement."""
        async with self.device._connection_semaphore:
            try:
                await self.device.disconnect()
                if await self.device.connect():
                    self._last_warm_connect_attempt = None
                    return
            except Exception as e:
                _LOGGER.debug("[Coordinator] Warm connect failed for %s: %s", self._name, e)
        self._last_warm_connect_attempt = datetime.now()

    @callback
    def _handle_position_notification(self, position: int):
        """Handle real-time position update from GATT notification."""
        self._position = position
        self._available = True
        if self._initializing:
            self._initializing = False
        self.async_update_listeners()

    @callback
    def _handle_device_disconnected(self):
        """Handle unexpected BLE disconnection from the device."""
        if not self.device._active_mode:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return
        _LOGGER.info("[Coordinator] Active mode: scheduling reconnect for %s", self._name)
        self._reconnect_task = self.hass.async_create_task(self._active_reconnect())

    async def _active_reconnect(self):
        """Reconnect in active mode with exponential backoff."""
        delay = self.device._active_reconnect_delay
        for attempt in range(self.device._max_retry_attempts):
            await asyncio.sleep(delay)
            async with self.device._connection_semaphore:
                await self.device.disconnect()
                ble_device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
                if not ble_device:
                    _LOGGER.debug(
                        "[Coordinator] Active reconnect attempt %d: no BLE device for %s",
                        attempt + 1,
                        self._name,
                    )
                    delay = min(delay * 2, 60)
                    continue
                self.device.set_ble_device(ble_device)
                if await self.device.connect():
                    _LOGGER.info("[Coordinator] Active mode reconnected to %s", self._name)
                    return
            delay = min(delay * 2, 60)
        _LOGGER.warning(
            "[Coordinator] Active mode reconnect failed for %s after %d attempts, scheduling slow retry",
            self._name,
            self.device._max_retry_attempts,
        )
        self._last_warm_connect_attempt = None
        self._reconnect_task = self.hass.async_create_task(self._slow_reconnect_loop())

    async def _slow_reconnect_loop(self):
        """Slow periodic reconnect every 5 minutes until connected."""
        while True:
            await asyncio.sleep(300)
            if self.device._is_connected:
                _LOGGER.debug(
                    "[Coordinator] Slow reconnect loop: %s already connected, stopping",
                    self._name,
                )
                return
            async with self.device._connection_semaphore:
                _LOGGER.info("[Coordinator] Slow reconnect attempt for %s", self._name)
                await self.device.disconnect()
                ble_device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
                if not ble_device:
                    _LOGGER.debug(
                        "[Coordinator] Slow reconnect: no BLE device for %s, will retry",
                        self._name,
                    )
                    continue
                self.device.set_ble_device(ble_device)
                if await self.device.connect():
                    _LOGGER.info("[Coordinator] Slow reconnect succeeded for %s", self._name)
                    self._last_warm_connect_attempt = None
                    return

    # ------------------------------------------------------------------
    # Polling (passive mode only)
    # ------------------------------------------------------------------

    @callback
    def _needs_poll(self, service_info, seconds_since_last_poll):
        if self.device._active_mode:
            return False
        ble_device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        should_poll = (
            self.hass.state == self.hass.CoreState.running
            and self.device.poll_needed(seconds_since_last_poll)
            and bool(ble_device)
        )
        _LOGGER.debug(
            "[Coordinator] _needs_poll: seconds=%s, ble_device=%s, poll=%s",
            seconds_since_last_poll,
            bool(ble_device),
            should_poll,
        )
        return should_poll

    async def _async_update(self, service_info):
        _LOGGER.debug(
            "[Coordinator] _async_update called for %s (address: %s)",
            self._name,
            self.address,
        )
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
            _LOGGER.debug(
                "[Coordinator] GATT poll data for %s: raw=%s (hex=%s)",
                self._name,
                list(data) if data else None,
                data.hex() if data else None,
            )
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

    # ------------------------------------------------------------------
    # Properties and helpers
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    async def _ensure_connected(self) -> bool:
        """Ensure the device is connected before performing operations."""
        client = self.device.client
        if not client or not client.is_connected:
            async with self.device._connection_semaphore:
                await self.device.disconnect()
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
                    if self.device._needs_repair:
                        self.device._needs_repair = False
                        ir.async_create_issue(
                            self.hass,
                            DOMAIN,
                            f"ble_auth_failed_{self.address}",
                            is_fixable=True,
                            severity=ir.IssueSeverity.ERROR,
                            translation_key="ble_auth_failed",
                            translation_placeholders={"name": self._name},
                        )
                    self.async_update_listeners()
                    return False
        return True

    async def _execute_command(self, operation, *args) -> None:
        """Execute a BLE command with one retry on connection drop."""
        for attempt in range(2):
            if not await self._ensure_connected():
                return
            try:
                await operation(*args)
                ir.async_delete_issue(self.hass, DOMAIN, f"ble_auth_failed_{self.address}")
                return
            except (BleakError, ConnectionError) as e:
                err_str = str(e)
                _LOGGER.warning(
                    "[Coordinator] Command failed for %s (attempt %d): %s",
                    self._name,
                    attempt + 1,
                    e,
                )
                if "Insufficient authentication" in err_str and attempt == 0:
                    _LOGGER.warning(
                        "[Coordinator] %s: BLE auth failed, attempting re-pair",
                        self._name,
                    )
                    try:
                        if not self.device.client or not self.device.client.is_connected:
                            await self.device.disconnect()
                            if not await self._ensure_connected():
                                continue
                        await self.device.client.pair()
                        _LOGGER.info("[Coordinator] %s: BLE re-pair successful", self._name)
                        continue
                    except Exception as pair_err:
                        _LOGGER.error(
                            "[Coordinator] %s: BLE re-pair failed: %s",
                            self._name,
                            pair_err,
                        )
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
                    _LOGGER.info("[Coordinator] Retrying command for %s", self._name)
        # Both attempts failed
        self._available = False
        self._was_unavailable = True
        self.async_update_listeners()
        if self.device._active_mode and (not self._reconnect_task or self._reconnect_task.done()):
            _LOGGER.info(
                "[Coordinator] Scheduling reconnect for %s after command failure",
                self._name,
            )
            self._reconnect_task = self.hass.async_create_task(self._active_reconnect())

    async def async_set_position(self, position: int) -> None:
        """Set the cover position."""
        await self._execute_command(self.device.set_position, position)

    async def async_open_cover(self) -> None:
        """Open the cover."""
        if self.position is not None and self.position == 0:
            return
        await self._execute_command(self.device.open)

    async def async_close_cover(self) -> None:
        """Close the cover."""
        if self.position is not None and self.position == 100:
            return
        await self._execute_command(self.device.close)
