"""Tests for the RYSE battery sensor entity."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from custom_components.ryse.sensor import RyseBatterySensor


@pytest.fixture
def mock_sensor(mock_ryse_device):
    """Create a RyseBatterySensor with mocked coordinator and entry."""
    coordinator = MagicMock()
    coordinator.device = mock_ryse_device
    coordinator.available = True
    coordinator.initializing = False
    coordinator.battery = 85
    coordinator.async_update_battery = AsyncMock()

    entry = MagicMock()
    entry.data = {"name": "Bedroom Blinds", "address": "AA:BB:CC:DD:EE:FF"}
    entry.entry_id = "test_entry_123"
    entry.options = {}

    sensor = RyseBatterySensor(coordinator, entry)
    return sensor


class TestSensorProperties:
    def test_name(self, mock_sensor) -> None:
        assert mock_sensor._attr_name == "Bedroom Blinds Battery"

    def test_unique_id(self, mock_sensor) -> None:
        assert mock_sensor._attr_unique_id == "test_entry_123_battery"

    def test_device_class(self, mock_sensor) -> None:
        assert mock_sensor._attr_device_class == SensorDeviceClass.BATTERY

    def test_state_class(self, mock_sensor) -> None:
        assert mock_sensor._attr_state_class == SensorStateClass.MEASUREMENT

    def test_unit_of_measurement(self, mock_sensor) -> None:
        assert mock_sensor._attr_native_unit_of_measurement == "%"


class TestSensorValue:
    def test_native_value_returns_battery(self, mock_sensor) -> None:
        mock_sensor._coordinator.battery = 85
        assert mock_sensor.native_value == 85

    def test_native_value_none_when_initializing(self, mock_sensor) -> None:
        mock_sensor._coordinator.initializing = True
        assert mock_sensor.native_value is None

    def test_native_value_none_when_disabled(self, mock_sensor) -> None:
        mock_sensor._entry.options = {"disable_battery_sensor": True}
        assert mock_sensor.native_value is None


class TestSensorAvailability:
    def test_available_when_coordinator_available(self, mock_sensor) -> None:
        assert mock_sensor.available is True

    def test_unavailable_when_coordinator_unavailable(self, mock_sensor) -> None:
        mock_sensor._coordinator.available = False
        assert mock_sensor.available is False

    def test_unavailable_during_init(self, mock_sensor) -> None:
        mock_sensor._coordinator.initializing = True
        assert mock_sensor.available is False

    def test_unavailable_when_disabled(self, mock_sensor) -> None:
        mock_sensor._entry.options = {"disable_battery_sensor": True}
        assert mock_sensor.available is False


class TestSensorState:
    def test_state_during_init(self, mock_sensor) -> None:
        mock_sensor._coordinator.initializing = True
        assert mock_sensor.state == "initializing"


class TestSensorCallbacks:
    async def test_battery_update_callback(self, mock_sensor) -> None:
        """Battery callback should update coordinator battery."""
        await mock_sensor._handle_battery_update(92)
        mock_sensor._coordinator.async_update_battery.assert_awaited_once_with(92)

    def test_unavailable_callback_writes_state(self, mock_sensor) -> None:
        """Unavailable callback should write state without clearing battery."""
        mock_sensor.async_write_ha_state = MagicMock()

        mock_sensor._handle_device_unavailable()

        mock_sensor.async_write_ha_state.assert_called_once()

    def test_adv_seen_callback_when_unavailable(self, mock_sensor) -> None:
        """Adv seen callback should write state when sensor is not available."""
        mock_sensor._coordinator.available = False
        mock_sensor.async_write_ha_state = MagicMock()

        mock_sensor._handle_adv_seen()

        mock_sensor.async_write_ha_state.assert_called_once()

    def test_adv_seen_callback_when_available_no_write(self, mock_sensor) -> None:
        """Adv seen callback should not write state when already available."""
        mock_sensor._coordinator.available = True
        mock_sensor._coordinator.initializing = False
        mock_sensor._entry.options = {}
        mock_sensor.async_write_ha_state = MagicMock()

        mock_sensor._handle_adv_seen()

        mock_sensor.async_write_ha_state.assert_not_called()
