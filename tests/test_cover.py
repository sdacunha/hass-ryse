"""Tests for the RYSE SmartShade cover entity."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.components.cover import CoverEntityFeature

from custom_components.ryse.cover import SmartShadeCover


@pytest.fixture
def mock_cover(mock_ryse_device):
    """Create a SmartShadeCover with mocked coordinator and entry."""
    coordinator = MagicMock()
    coordinator.device = mock_ryse_device
    coordinator.available = True
    coordinator.initializing = False
    coordinator.position = 50
    coordinator.battery = 85
    coordinator.async_open_cover = AsyncMock()
    coordinator.async_close_cover = AsyncMock()
    coordinator.async_set_position = AsyncMock()

    entry = MagicMock()
    entry.data = {"name": "Bedroom Blinds", "address": "AA:BB:CC:DD:EE:FF"}
    entry.entry_id = "test_entry_123"

    cover = SmartShadeCover(coordinator, entry)
    return cover


class TestCoverProperties:
    def test_name(self, mock_cover) -> None:
        assert mock_cover._attr_name == "Bedroom Blinds"

    def test_unique_id(self, mock_cover) -> None:
        assert mock_cover._attr_unique_id == "test_entry_123_cover"

    def test_supported_features(self, mock_cover) -> None:
        features = mock_cover.supported_features
        assert features & CoverEntityFeature.OPEN
        assert features & CoverEntityFeature.CLOSE
        assert features & CoverEntityFeature.SET_POSITION

    def test_device_info(self, mock_cover) -> None:
        info = mock_cover.device_info
        assert ("ryse", "AA:BB:CC:DD:EE:FF") in info["identifiers"]
        assert info["manufacturer"] == "RYSE Inc."
        assert info["model"] == "SmartShade"


class TestCoverPosition:
    """Position is inverted: RYSE 0=open, HA 100=open."""

    def test_position_inverted(self, mock_cover) -> None:
        """RYSE position 50 should map to HA position 50 (100-50)."""
        mock_cover._coordinator.position = 50
        assert mock_cover.current_cover_position == 50

    def test_position_fully_open(self, mock_cover) -> None:
        """RYSE position 0 (open) should map to HA position 100."""
        mock_cover._coordinator.position = 0
        assert mock_cover.current_cover_position == 100

    def test_position_fully_closed(self, mock_cover) -> None:
        """RYSE position 100 (closed) should map to HA position 0."""
        mock_cover._coordinator.position = 100
        assert mock_cover.current_cover_position == 0

    def test_position_none(self, mock_cover) -> None:
        mock_cover._coordinator.position = None
        assert mock_cover.current_cover_position is None

    def test_position_during_initializing(self, mock_cover) -> None:
        mock_cover._coordinator.initializing = True
        assert mock_cover.current_cover_position is None


class TestCoverState:
    def test_is_closed_when_fully_closed(self, mock_cover) -> None:
        mock_cover._coordinator.position = 100
        assert mock_cover.is_closed is True

    def test_is_not_closed_when_open(self, mock_cover) -> None:
        mock_cover._coordinator.position = 50
        assert mock_cover.is_closed is False

    def test_is_closed_none_when_no_position(self, mock_cover) -> None:
        mock_cover._coordinator.position = None
        assert mock_cover.is_closed is None

    def test_is_closed_none_during_init(self, mock_cover) -> None:
        mock_cover._coordinator.initializing = True
        assert mock_cover.is_closed is None

    def test_state_during_init(self, mock_cover) -> None:
        mock_cover._coordinator.initializing = True
        assert mock_cover.state == "initializing"


class TestCoverAvailability:
    def test_available_when_coordinator_available(self, mock_cover) -> None:
        mock_cover._coordinator.available = True
        mock_cover._coordinator.initializing = False
        assert mock_cover.available is True

    def test_unavailable_when_coordinator_unavailable(self, mock_cover) -> None:
        mock_cover._coordinator.available = False
        assert mock_cover.available is False

    def test_unavailable_during_init(self, mock_cover) -> None:
        mock_cover._coordinator.initializing = True
        assert mock_cover.available is False


class TestCoverCommands:
    async def test_open_cover(self, mock_cover) -> None:
        await mock_cover.async_open_cover()
        mock_cover._coordinator.async_open_cover.assert_awaited_once()

    async def test_close_cover(self, mock_cover) -> None:
        await mock_cover.async_close_cover()
        mock_cover._coordinator.async_close_cover.assert_awaited_once()

    async def test_set_cover_position(self, mock_cover) -> None:
        """HA position 75 should map to RYSE position 25 (100-75)."""
        await mock_cover.async_set_cover_position(position=75)
        mock_cover._coordinator.async_set_position.assert_awaited_once_with(25)

    async def test_set_cover_position_fully_open(self, mock_cover) -> None:
        """HA 100 (fully open) → RYSE 0."""
        await mock_cover.async_set_cover_position(position=100)
        mock_cover._coordinator.async_set_position.assert_awaited_once_with(0)

    async def test_set_cover_position_fully_closed(self, mock_cover) -> None:
        """HA 0 (fully closed) → RYSE 100."""
        await mock_cover.async_set_cover_position(position=0)
        mock_cover._coordinator.async_set_position.assert_awaited_once_with(100)
