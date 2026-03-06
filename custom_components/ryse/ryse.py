import logging
import asyncio
from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection, BleakNotFoundError
from .const import (
    HARDCODED_UUIDS,
    DEFAULT_IDLE_DISCONNECT_TIMEOUT,
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_MAX_RETRY_ATTEMPTS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_ACTIVE_MODE,
    DEFAULT_ACTIVE_RECONNECT_DELAY,
)

_LOGGER = logging.getLogger(__name__)

# Use the RX and TX UUIDs for operations
POSITION_CHAR_UUID = HARDCODED_UUIDS["rx_uuid"]
COMMAND_CHAR_UUID = HARDCODED_UUIDS["tx_uuid"]


class RyseDevice:
    def __init__(self, address: str):
        self.address = address
        self.ble_device: BLEDevice | None = None
        self.client: BleakClient | None = None
        self._battery_callbacks = []
        self._unavailable_callbacks = []
        self._adv_callbacks = []
        self._latest_battery = None
        self._battery_level = None
        self._is_connected = False
        self._connection_lock = asyncio.Lock()
        self._connecting = False
        self._idle_timer = None
        self._disconnect_callbacks = []
        # Configurable timeouts (can be updated from config entry options)
        self._connection_timeout = DEFAULT_CONNECTION_TIMEOUT
        self._max_retry_attempts = DEFAULT_MAX_RETRY_ATTEMPTS
        self._poll_interval = DEFAULT_POLL_INTERVAL
        self._idle_disconnect_timeout = DEFAULT_IDLE_DISCONNECT_TIMEOUT
        self._active_mode = DEFAULT_ACTIVE_MODE
        self._active_reconnect_delay = DEFAULT_ACTIVE_RECONNECT_DELAY

    def add_battery_callback(self, callback):
        """Add a callback for battery updates."""
        self._battery_callbacks.append(callback)

    def add_unavailable_callback(self, callback):
        """Add a callback for device unavailability."""
        self._unavailable_callbacks.append(callback)

    def add_adv_callback(self, callback):
        """Add a callback for advertisement updates."""
        self._adv_callbacks.append(callback)

    def add_disconnect_callback(self, callback):
        """Add a callback for unexpected disconnections."""
        self._disconnect_callbacks.append(callback)

    def get_battery_level(self) -> int | None:
        """Get the latest battery level."""
        return self._battery_level

    def _on_disconnected(self, client: BleakClient):
        """Handle unexpected BLE disconnection."""
        if self.client is not client:
            return  # Already cleaned up via explicit disconnect
        _LOGGER.warning(f"[{self.address}] BLE connection lost unexpectedly")
        self.client = None
        self._is_connected = False
        self._connecting = False
        self._cancel_idle_timer()
        for cb in self._disconnect_callbacks:
            cb()

    def _schedule_idle_disconnect(self):
        """Reset the idle disconnect timer. Disconnects after inactivity to prevent stale connections."""
        if self._active_mode:
            return  # Active mode: keep connection alive
        self._cancel_idle_timer()
        try:
            loop = asyncio.get_running_loop()
            self._idle_timer = loop.call_later(
                self._idle_disconnect_timeout,
                lambda: asyncio.ensure_future(self._idle_disconnect()),
            )
        except RuntimeError:
            pass  # No running loop (e.g. during shutdown)

    def _cancel_idle_timer(self):
        """Cancel the idle disconnect timer."""
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    async def _idle_disconnect(self):
        """Disconnect after idle timeout to prevent stale connections."""
        _LOGGER.debug(f"[{self.address}] Idle timeout ({self._idle_disconnect_timeout}s), disconnecting proactively")
        await self.disconnect()

    def set_ble_device(self, ble_device: BLEDevice | None) -> None:
        self.ble_device = ble_device

    def update_ble_device_from_adv(self, service_info):
        if hasattr(service_info, "device") and service_info.device:
            self.set_ble_device(service_info.device)

    async def connect(self):
        """Connect using bleak-retry-connector for reliable connection establishment."""
        async with self._connection_lock:
            # Already connected
            if self.client and self.client.is_connected:
                self._is_connected = True
                self._connecting = False
                self._schedule_idle_disconnect()
                _LOGGER.debug(f"[{self.address}] Already connected")
                return True

            # Prevent concurrent connection attempts
            if self._connecting:
                _LOGGER.debug(f"[{self.address}] Connection already in progress")
                return False

            self._connecting = True

            if not self.ble_device:
                _LOGGER.error(f"[{self.address}] No BLEDevice available for connection")
                self._connecting = False
                raise ConnectionError("No BLEDevice available for connection")

            try:
                _LOGGER.info(
                    f"[{self.address}] Connecting via bleak-retry-connector (max_attempts={self._max_retry_attempts})"
                )

                self.client = await establish_connection(
                    BleakClient,
                    self.ble_device,
                    self.address,
                    max_attempts=self._max_retry_attempts,
                    timeout=self._connection_timeout,
                    disconnected_callback=self._on_disconnected,
                )

                if self.client.is_connected:
                    self._is_connected = True
                    self._connecting = False
                    self._schedule_idle_disconnect()
                    _LOGGER.info(f"[{self.address}] Successfully connected")
                    return True

            except BleakNotFoundError:
                _LOGGER.warning(f"[{self.address}] Device not found")
            except asyncio.TimeoutError:
                _LOGGER.warning(f"[{self.address}] Connection timed out")
            except Exception as e:
                _LOGGER.error(f"[{self.address}] Connection failed: {type(e).__name__}: {e}")

            # Connection failed
            _LOGGER.error(f"[{self.address}] Connection attempts failed")
            self._is_connected = False
            self._connecting = False
            for callback in self._unavailable_callbacks:
                callback()
            return False

    async def disconnect(self):
        """Disconnect from the device with proper state tracking."""
        async with self._connection_lock:
            self._cancel_idle_timer()
            # Capture and clear client ref before disconnecting so the
            # _on_disconnected callback (fired by bleak) becomes a no-op.
            client = self.client
            self.client = None
            self._is_connected = False
            self._connecting = False
            if client:
                try:
                    if client.is_connected:
                        _LOGGER.debug(f"[{self.address}] Disconnecting")
                        await client.disconnect()
                except Exception as e:
                    _LOGGER.debug(f"[{self.address}] Error during disconnect: {e}")
            else:
                _LOGGER.debug(f"[{self.address}] Already disconnected")

    async def set_position(self, position: int):
        if not (0 <= position <= 100):
            raise ValueError("position must be between 0 and 100")
        data_bytes = bytes([0xF5, 0x03, 0x01, 0x01, position])
        checksum = sum(data_bytes[2:]) % 256
        packet = data_bytes + bytes([checksum])
        await self.write_gatt(COMMAND_CHAR_UUID, packet)

    async def open(self):
        await self.set_position(0)

    async def close(self):
        await self.set_position(100)

    async def get_battery(self) -> int | None:
        data = await self.read_gatt(POSITION_CHAR_UUID)
        if data and len(data) >= 3:
            self._battery_level = data[2]
            self._latest_battery = data[2]
            for callback in self._battery_callbacks:
                await callback(data[2])
            return data[2]
        return None

    async def get_position(self) -> int | None:
        data = await self.read_gatt(POSITION_CHAR_UUID)
        if data and len(data) >= 2:
            return data[1]
        return None

    async def read_gatt(self, char_uuid: str) -> bytes | None:
        if not self.client or not self.client.is_connected:
            raise ConnectionError("Not connected to device")
        result = await self.client.read_gatt_char(char_uuid)
        self._schedule_idle_disconnect()  # Reset idle timer on activity
        return result

    async def write_gatt(self, char_uuid: str, data: bytes):
        if not self.client or not self.client.is_connected:
            raise ConnectionError("Not connected to device")
        await self.client.write_gatt_char(char_uuid, data)
        self._schedule_idle_disconnect()  # Reset idle timer on activity

    @staticmethod
    def parse_advertisement(service_info) -> dict:
        result = {}
        for mfr_id, data in getattr(service_info, "manufacturer_data", {}).items():
            _LOGGER.debug(
                "[ADV] %s mfr_id=0x%04X raw=%s (hex=%s)",
                getattr(service_info, "address", "?"),
                mfr_id,
                list(data),
                data.hex(),
            )
            # Only use the RYSE manufacturer ID (0x0409). Other entries
            # (e.g. 0x2082) contain unrelated data that was overwriting
            # the correct position/battery values.
            if mfr_id not in (0x0409, 0x409):
                continue
            if len(data) >= 3:
                result["position"] = data[1]
                result["battery"] = data[2]
        return result

    def poll_needed(self, seconds_since_last_poll):
        """Determine if a poll is needed based on configurable interval."""
        if seconds_since_last_poll is None:
            return True
        return seconds_since_last_poll > self._poll_interval
