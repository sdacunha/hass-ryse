"""Tests for the repair flow (single-bond pair through best-RSSI proxy)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ryse.const import DOMAIN
from custom_components.ryse.repairs import (
    BleAuthFailedRepairFlow,
    async_create_fix_flow,
)
from tests import RYSE_ADDRESS


def _make_scanner_device(source: str, rssi: int):
    """Synthesize a BluetoothScannerDevice with the bits the repair flow reads."""
    sd = MagicMock()
    sd.scanner.source = source
    sd.advertisement.rssi = rssi
    sd.ble_device = MagicMock()
    return sd


@pytest.fixture
def flow_with_coordinator(hass):
    """A repair flow with a coordinator already registered for our address."""
    coord = MagicMock()
    coord.address = RYSE_ADDRESS
    coord.device.disconnect = AsyncMock()
    coord._entry_id = "test_entry"
    hass.data.setdefault(DOMAIN, {})["test_entry"] = coord

    flow = BleAuthFailedRepairFlow()
    flow.hass = hass
    flow.issue_id = f"ble_auth_failed_{RYSE_ADDRESS}"
    return flow, coord


@pytest.mark.asyncio
async def test_init_step_shows_form(hass):
    """Initial step renders the press-PAIR confirmation form."""
    flow = BleAuthFailedRepairFlow()
    flow.hass = hass
    flow.issue_id = f"ble_auth_failed_{RYSE_ADDRESS}"
    result = await flow.async_step_init()
    assert result["type"] == "form"
    assert result["step_id"] == "init"


@pytest.mark.asyncio
async def test_init_step_proceeds_on_submit(hass):
    """Submitting init step calls the repair step (which aborts here since
    we have no coordinator registered)."""
    flow = BleAuthFailedRepairFlow()
    flow.hass = hass
    flow.issue_id = f"ble_auth_failed_{RYSE_ADDRESS}"
    result = await flow.async_step_init(user_input={})
    assert result["type"] == "abort"
    assert result["reason"] == "device_not_found"


@pytest.mark.asyncio
async def test_repair_aborts_when_no_coordinator(hass):
    """No matching coordinator → abort with device_not_found."""
    flow = BleAuthFailedRepairFlow()
    flow.hass = hass
    flow.issue_id = f"ble_auth_failed_{RYSE_ADDRESS}"
    result = await flow.async_step_repair()
    assert result["type"] == "abort"
    assert result["reason"] == "device_not_found"


@pytest.mark.asyncio
async def test_repair_aborts_when_no_scanner_devices(flow_with_coordinator):
    """No reachable proxies → abort with device_not_found."""
    flow, _coord = flow_with_coordinator
    with patch(
        "custom_components.ryse.repairs.async_scanner_devices_by_address",
        return_value=[],
    ):
        result = await flow.async_step_repair()
    assert result["type"] == "abort"
    assert result["reason"] == "device_not_found"


@pytest.mark.asyncio
async def test_repair_picks_highest_rssi_proxy(flow_with_coordinator):
    """When multiple proxies reach the shade, the strongest-RSSI one wins."""
    flow, _coord = flow_with_coordinator
    strongest = _make_scanner_device("proxy-strong", -45)
    # Capture refs BEFORE invocation — repairs.py sorts the list in-place.
    expected_ble_device = strongest.ble_device
    scanners = [
        _make_scanner_device("proxy-weak", -90),
        strongest,
        _make_scanner_device("proxy-medium", -70),
    ]

    fake_client = MagicMock()
    fake_client.is_connected = True
    fake_client.unpair = AsyncMock()
    fake_client.pair = AsyncMock()
    fake_client.disconnect = AsyncMock()

    with (
        patch(
            "custom_components.ryse.repairs.async_scanner_devices_by_address",
            return_value=scanners,
        ),
        patch(
            "custom_components.ryse.repairs.establish_connection",
            new=AsyncMock(return_value=fake_client),
        ) as mock_establish,
        patch("custom_components.ryse.repairs.ir.async_delete_issue"),
    ):
        result = await flow.async_step_repair()

    # establish_connection got called with the strongest-RSSI proxy's BLEDevice
    args, _ = mock_establish.call_args
    assert args[1] is expected_ble_device
    fake_client.unpair.assert_awaited_once()
    fake_client.pair.assert_awaited_once()
    fake_client.disconnect.assert_awaited()
    assert result["type"] == "create_entry"


@pytest.mark.asyncio
async def test_repair_aborts_on_connect_failure(flow_with_coordinator):
    """establish_connection raising → cannot_connect abort."""
    from bleak.exc import BleakError

    flow, _coord = flow_with_coordinator
    with (
        patch(
            "custom_components.ryse.repairs.async_scanner_devices_by_address",
            return_value=[_make_scanner_device("proxy", -50)],
        ),
        patch(
            "custom_components.ryse.repairs.establish_connection",
            new=AsyncMock(side_effect=BleakError("no")),
        ),
    ):
        result = await flow.async_step_repair()
    assert result["type"] == "abort"
    assert result["reason"] == "cannot_connect"


@pytest.mark.asyncio
async def test_repair_aborts_on_pair_failure(flow_with_coordinator):
    """pair() raising → repair_failed abort, link is torn down."""
    from bleak.exc import BleakError

    flow, _coord = flow_with_coordinator
    fake_client = MagicMock()
    fake_client.is_connected = True
    fake_client.unpair = AsyncMock()
    fake_client.pair = AsyncMock(side_effect=BleakError("auth"))
    fake_client.disconnect = AsyncMock()

    with (
        patch(
            "custom_components.ryse.repairs.async_scanner_devices_by_address",
            return_value=[_make_scanner_device("proxy", -50)],
        ),
        patch(
            "custom_components.ryse.repairs.establish_connection",
            new=AsyncMock(return_value=fake_client),
        ),
    ):
        result = await flow.async_step_repair()
    assert result["type"] == "abort"
    assert result["reason"] == "repair_failed"
    fake_client.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_unpair_failure_is_non_fatal(flow_with_coordinator):
    """If unpair() raises (e.g., no prior bond), pair() still runs."""
    flow, _coord = flow_with_coordinator
    fake_client = MagicMock()
    fake_client.is_connected = True
    fake_client.unpair = AsyncMock(side_effect=RuntimeError("no bond"))
    fake_client.pair = AsyncMock()
    fake_client.disconnect = AsyncMock()

    with (
        patch(
            "custom_components.ryse.repairs.async_scanner_devices_by_address",
            return_value=[_make_scanner_device("proxy", -50)],
        ),
        patch(
            "custom_components.ryse.repairs.establish_connection",
            new=AsyncMock(return_value=fake_client),
        ),
        patch("custom_components.ryse.repairs.ir.async_delete_issue"),
    ):
        result = await flow.async_step_repair()
    fake_client.pair.assert_awaited_once()
    assert result["type"] == "create_entry"


@pytest.mark.asyncio
async def test_async_create_fix_flow_matches_issue_id(hass):
    """The factory returns our flow only for ble_auth_failed_* issues."""
    flow = await async_create_fix_flow(hass, f"ble_auth_failed_{RYSE_ADDRESS}")
    assert isinstance(flow, BleAuthFailedRepairFlow)
    assert await async_create_fix_flow(hass, "some_other_issue") is None
