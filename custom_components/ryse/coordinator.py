from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.active_update_coordinator import ActiveBluetoothDataUpdateCoordinator
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
    def __init__(self, hass: HomeAssistant, address: str, device: RyseDevice, name: str, entry_id: str | None = None):
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
        self._unavailable_cancel = bluetooth.async_track_unavailable(
            hass, self._handle_unavailable, address, connectable=True
        )
        self._adv_cancel = bluetooth.async_register_callback(
            hass, self._handle_adv, {"address": address}, bluetooth.BluetoothScanningMode.PASSIVE
        )
        self._reconnect_task = None
        self._last_warm_connect_attempt = None
        self.device.add_disconnect_callback(self._handle_device_disconnected)
        # Start the initialization timer
        self.hass.async_create_task(self._async_init_timeout())
        # In active mode, establish connection on startup
        if self.device._active_mode:
            self.hass.async_create_task(self._active_reconnect())

    async def _async_init_timeout(self):
        await asyncio.sleep(DEFAULT_INIT_TIMEOUT)
        if self._initializing:
            self._initializing = False
            if not self._available:
                _LOGGER.info(
                    f"Device {self._name} did not become available after {DEFAULT_INIT_TIMEOUT}s, marking as unavailable."
                )
            self.async_update_listeners()

    @callback
    def _handle_adv(self, service_info, change):
        # Always update BLE device reference from latest adv
        if hasattr(service_info, "device") and service_info.device:
            self.device.set_ble_device(service_info.device)
        adv = self.device.parse_advertisement(service_info)
        if adv.get("position") is not None:
            self._position = adv["position"]
            self._ready_event.set()  # Mark as ready if we get a valid adv
        if adv.get("battery") is not None:
            self._battery = adv["battery"]
            # Call battery callbacks with the new battery value
            for callback in self.device._battery_callbacks:
                if inspect.iscoroutinefunction(callback):
                    self.hass.async_create_task(callback(adv["battery"]))
                else:
                    callback(adv["battery"])
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
        # Proactively connect when we hear an advertisement so the connection
        # is warm when a command arrives. In active mode, this also recovers
        # from silent connection drops (where the disconnect callback didn't fire).
        if not self.device._is_connected and not self.device._connecting:
            # Don't pile on if _active_reconnect is already trying
            if self._reconnect_task and not self._reconnect_task.done():
                return
            now = datetime.now()
            if self.device._active_mode:
                # Active mode: cooldown of 60s between warm connect attempts
                if self._last_warm_connect_attempt is None or (now - self._last_warm_connect_attempt) > timedelta(
                    seconds=60
                ):
                    _LOGGER.info("[Coordinator] Active mode: reconnecting on advertisement for %s", self._name)
                    self._last_warm_connect_attempt = now
                    self.hass.async_create_task(self._warm_connect())
            else:
                # Passive mode: don't warm-connect. Connect on-demand when a
                # command is sent via _ensure_connected(). Warm connecting just
                # creates pointless connect/disconnect churn because the idle
                # timer disconnects before any command arrives.
                pass

    async def _warm_connect(self):
        """Proactively connect after hearing an advertisement."""
        try:
            if await self.device.connect():
                # Reset cooldown on success so next disconnect can reconnect immediately
                self._last_warm_connect_attempt = None
        except Exception as e:
            _LOGGER.debug("[Coordinator] Warm connect failed for %s: %s", self._name, e)

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
                _LOGGER.debug(
                    f"[Coordinator] {self._name} stopped advertising but was seen {time_since_adv.total_seconds():.0f}s ago, keeping as available"
                )
                return

        _LOGGER.warning(f"[Coordinator] _handle_unavailable called for {self._name} (address: {self.device.address})")
        self._available = False
        self._was_unavailable = True
        self.async_update_listeners()

    @callback
    def _handle_device_disconnected(self):
        """Handle unexpected BLE disconnection from the device."""
        if not self.device._active_mode:
            return
        # Avoid stacking reconnect tasks
        if self._reconnect_task and not self._reconnect_task.done():
            return
        _LOGGER.info(f"[Coordinator] Active mode: scheduling reconnect for {self._name}")
        self._reconnect_task = self.hass.async_create_task(self._active_reconnect())

    async def _active_reconnect(self):
        """Reconnect in active mode after unexpected disconnect."""
        for attempt in range(self.device._max_retry_attempts):
            await asyncio.sleep(self.device._active_reconnect_delay)
            ble_device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
            if not ble_device:
                _LOGGER.debug(
                    "[Coordinator] Active reconnect attempt %d: no BLE device for %s",
                    attempt + 1,
                    self._name,
                )
                continue
            self.device.set_ble_device(ble_device)
            if await self.device.connect():
                _LOGGER.info(f"[Coordinator] Active mode reconnected to {self._name}")
                return
        _LOGGER.warning(
            "[Coordinator] Active mode reconnect failed for %s after %d attempts",
            self._name,
            self.device._max_retry_attempts,
        )

    @callback
    def _needs_poll(self, service_info, seconds_since_last_poll):
        # Active mode: advertisements + persistent connection handle everything.
        # Polling just causes connect/disconnect churn and auth failures.
        if self.device._active_mode:
            return False
        ble_device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        should_poll = (
            self.hass.state == self.hass.CoreState.running
            and self.device.poll_needed(seconds_since_last_poll)
            and bool(ble_device)
        )
        _LOGGER.debug(
            "[Coordinator] _needs_poll called: seconds_since_last_poll=%s, has_ble_device=%s, should_poll=%s, position=%s, battery=%s",
            seconds_since_last_poll,
            bool(ble_device),
            should_poll,
            self._position,
            self._battery,
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
                # Insufficient authentication means the BLE session is stale
                # (e.g. after HA reboot). Try to re-pair before retrying.
                if "Insufficient authentication" in err_str and attempt == 0:
                    _LOGGER.warning(
                        "[Coordinator] %s: BLE auth failed, attempting re-pair",
                        self._name,
                    )
                    try:
                        # The connection may have dropped (clearing client)
                        # before we get here — reconnect first if needed.
                        if not self.device.client or not self.device.client.is_connected:
                            await self.device.disconnect()
                            if not await self._ensure_connected():
                                continue
                        await self.device.client.pair()
                        _LOGGER.info("[Coordinator] %s: BLE re-pair successful", self._name)
                        continue  # Retry the command without disconnecting
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
                # Force disconnect to clear stale state before retry
                await self.device.disconnect()
                if attempt == 0:
                    _LOGGER.info("[Coordinator] Retrying command for %s", self._name)
        # Both attempts failed
        self._available = False
        self._was_unavailable = True
        self.async_update_listeners()

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
