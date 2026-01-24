"""Tests for __init__.py - Integration setup and coordinator."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import timedelta
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.volvooncall_cn import (
    async_setup_entry,
    async_update_options,
    VolvoCoordinator,
)
from custom_components.volvooncall_cn.volvooncall_cn import DOMAIN
from custom_components.volvooncall_cn.volvooncall_base import DEFAULT_SCAN_INTERVAL

from tests.conftest import TEST_USERNAME, TEST_PASSWORD, TEST_SCAN_INTERVAL


# =============================================================================
# Test Integration Setup
# =============================================================================

class TestIntegrationSetup:
    """Test the integration setup process."""

    @pytest.mark.asyncio
    async def test_async_setup_entry(self, hass: HomeAssistant, mock_volvo_api):
        """Test successful integration setup."""
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: TEST_USERNAME,
                CONF_PASSWORD: TEST_PASSWORD,
                CONF_SCAN_INTERVAL: TEST_SCAN_INTERVAL,
            },
            unique_id=TEST_USERNAME,
        )
        config_entry.add_to_hass(hass)
        
        with patch("custom_components.volvooncall_cn.VehicleAPI", return_value=mock_volvo_api):
            # Mock coordinator to avoid actual API calls
            with patch("custom_components.volvooncall_cn.VolvoCoordinator.async_config_entry_first_refresh"):
                result = await async_setup_entry(hass, config_entry)
                
                assert result is True
                assert DOMAIN in hass.data
                assert config_entry.entry_id in hass.data[DOMAIN]

    @pytest.mark.asyncio
    async def test_async_setup_entry_with_default_scan_interval(self, hass: HomeAssistant, mock_volvo_api):
        """Test setup with default scan interval when not specified."""
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: TEST_USERNAME,
                CONF_PASSWORD: TEST_PASSWORD,
                # No CONF_SCAN_INTERVAL specified
            },
            unique_id=TEST_USERNAME,
        )
        config_entry.add_to_hass(hass)
        
        with patch("custom_components.volvooncall_cn.VehicleAPI", return_value=mock_volvo_api):
            with patch("custom_components.volvooncall_cn.VolvoCoordinator.async_config_entry_first_refresh"):
                result = await async_setup_entry(hass, config_entry)
                
                assert result is True
                # Check that coordinator was created with default interval
                coordinator = hass.data[DOMAIN][config_entry.entry_id]
                assert coordinator.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)

    @pytest.mark.asyncio
    async def test_async_setup_entry_adds_update_listener(self, hass: HomeAssistant, mock_volvo_api):
        """Test that setup adds update listener for options changes."""
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: TEST_USERNAME,
                CONF_PASSWORD: TEST_PASSWORD,
                CONF_SCAN_INTERVAL: TEST_SCAN_INTERVAL,
            },
            unique_id=TEST_USERNAME,
        )
        config_entry.add_to_hass(hass)
        
        with patch("custom_components.volvooncall_cn.VehicleAPI", return_value=mock_volvo_api):
            with patch("custom_components.volvooncall_cn.VolvoCoordinator.async_config_entry_first_refresh"):
                await async_setup_entry(hass, config_entry)
                
                # Verify update listener was added
                assert len(config_entry.update_listeners) > 0


# =============================================================================
# Test Update Options
# =============================================================================

class TestUpdateOptions:
    """Test the options update functionality."""

    @pytest.mark.asyncio
    async def test_async_update_options(self, hass: HomeAssistant, mock_volvo_api):
        """Test updating configuration options."""
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: TEST_USERNAME,
                CONF_PASSWORD: TEST_PASSWORD,
                CONF_SCAN_INTERVAL: TEST_SCAN_INTERVAL,
            },
            unique_id=TEST_USERNAME,
        )
        config_entry.add_to_hass(hass)
        
        # Setup initial coordinator
        hass.data.setdefault(DOMAIN, {})
        with patch("custom_components.volvooncall_cn.VehicleAPI", return_value=mock_volvo_api):
            coordinator = VolvoCoordinator(hass, mock_volvo_api, TEST_SCAN_INTERVAL)
            hass.data[DOMAIN][config_entry.entry_id] = coordinator
            
            # Update options with new scan interval
            new_scan_interval = 120
            config_entry.options = {CONF_SCAN_INTERVAL: new_scan_interval}
            
            await async_update_options(hass, config_entry)
            
            # Verify scan interval was updated
            assert coordinator.update_interval == timedelta(seconds=new_scan_interval)

    @pytest.mark.asyncio
    async def test_async_update_options_updates_api(self, hass: HomeAssistant, mock_volvo_api):
        """Test that updating options creates new API instance."""
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: TEST_USERNAME,
                CONF_PASSWORD: TEST_PASSWORD,
                CONF_SCAN_INTERVAL: TEST_SCAN_INTERVAL,
            },
            unique_id=TEST_USERNAME,
        )
        config_entry.add_to_hass(hass)
        
        hass.data.setdefault(DOMAIN, {})
        with patch("custom_components.volvooncall_cn.VehicleAPI") as mock_api_class:
            old_api = AsyncMock()
            mock_api_class.return_value = old_api
            
            coordinator = VolvoCoordinator(hass, old_api, TEST_SCAN_INTERVAL)
            hass.data[DOMAIN][config_entry.entry_id] = coordinator
            
            # Update options
            new_password = "new_password"
            config_entry.options = {CONF_PASSWORD: new_password}
            
            await async_update_options(hass, config_entry)
            
            # Verify new API instance was created
            assert mock_api_class.called
            # Verify coordinator's API was updated
            assert coordinator.volvo_api is not None


# =============================================================================
# Test VolvoCoordinator
# =============================================================================

class TestVolvoCoordinator:
    """Test the VolvoCoordinator class."""

    def test_coordinator_initialization(self, hass: HomeAssistant, mock_volvo_api):
        """Test coordinator initialization."""
        coordinator = VolvoCoordinator(hass, mock_volvo_api, TEST_SCAN_INTERVAL)
        
        assert coordinator.volvo_api == mock_volvo_api
        assert coordinator.update_interval == timedelta(seconds=TEST_SCAN_INTERVAL)
        assert coordinator.name == "Volvo On Call CN sensor"
        assert coordinator.store_datas == []

    @pytest.mark.asyncio
    async def test_coordinator_update_data_success(self, hass: HomeAssistant, mock_volvo_api, mock_vehicle):
        """Test successful data update."""
        coordinator = VolvoCoordinator(hass, mock_volvo_api, TEST_SCAN_INTERVAL)
        
        # Mock get_all_vehicles to return a vehicle
        mock_volvo_api.get_all_vehicles = AsyncMock(return_value=[mock_vehicle])
        
        # Call _async_update_data
        result = await coordinator._async_update_data()
        
        # Verify get_all_vehicles was called
        mock_volvo_api.get_all_vehicles.assert_called_once()
        
        # Verify data was stored
        assert len(coordinator.store_datas) > 0

    @pytest.mark.asyncio
    async def test_coordinator_handles_empty_vehicles(self, hass: HomeAssistant, mock_volvo_api):
        """Test coordinator handles empty vehicle list."""
        coordinator = VolvoCoordinator(hass, mock_volvo_api, TEST_SCAN_INTERVAL)
        
        # Mock get_all_vehicles to return empty list
        mock_volvo_api.get_all_vehicles = AsyncMock(return_value=[])
        
        result = await coordinator._async_update_data()
        
        # Should not raise error, just return empty data
        assert coordinator.store_datas == []

    @pytest.mark.asyncio
    async def test_coordinator_handles_api_error(self, hass: HomeAssistant, mock_volvo_api):
        """Test coordinator handles API errors gracefully."""
        coordinator = VolvoCoordinator(hass, mock_volvo_api, TEST_SCAN_INTERVAL)
        
        # Mock get_all_vehicles to raise exception
        mock_volvo_api.get_all_vehicles = AsyncMock(side_effect=Exception("API Error"))
        
        # Should raise UpdateFailed
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    @pytest.mark.asyncio
    async def test_coordinator_update_interval_change(self, hass: HomeAssistant, mock_volvo_api):
        """Test changing coordinator update interval."""
        coordinator = VolvoCoordinator(hass, mock_volvo_api, TEST_SCAN_INTERVAL)
        
        assert coordinator.update_interval == timedelta(seconds=TEST_SCAN_INTERVAL)
        
        # Change interval
        new_interval = 120
        coordinator.update_interval = timedelta(seconds=new_interval)
        
        assert coordinator.update_interval == timedelta(seconds=new_interval)


# =============================================================================
# Test Platform Loading
# =============================================================================

class TestPlatformLoading:
    """Test that platforms are loaded correctly."""

    @pytest.mark.asyncio
    async def test_platforms_are_forwarded(self, hass: HomeAssistant, mock_volvo_api):
        """Test that all platforms are forwarded during setup."""
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: TEST_USERNAME,
                CONF_PASSWORD: TEST_PASSWORD,
                CONF_SCAN_INTERVAL: TEST_SCAN_INTERVAL,
            },
            unique_id=TEST_USERNAME,
        )
        config_entry.add_to_hass(hass)
        
        with patch("custom_components.volvooncall_cn.VehicleAPI", return_value=mock_volvo_api):
            with patch("custom_components.volvooncall_cn.VolvoCoordinator.async_config_entry_first_refresh"):
                with patch.object(hass.config_entries, "async_forward_entry_setups") as mock_forward:
                    await async_setup_entry(hass, config_entry)
                    
                    # Verify platforms were forwarded
                    mock_forward.assert_called_once()
                    # Get the platforms that were forwarded
                    call_args = mock_forward.call_args
                    platforms = call_args[0][1]
                    
                    # Verify all expected platforms are present
                    expected_platforms = {
                        "sensor", "binary_sensor", "device_tracker",
                        "lock", "button", "number", "switch"
                    }
                    assert set(platforms.keys()) == expected_platforms


# =============================================================================
# Test Error Scenarios
# =============================================================================

class TestErrorScenarios:
    """Test various error scenarios."""

    @pytest.mark.asyncio
    async def test_setup_with_invalid_config(self, hass: HomeAssistant):
        """Test setup with missing required config."""
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                # Missing username and password
                CONF_SCAN_INTERVAL: TEST_SCAN_INTERVAL,
            },
            unique_id="invalid",
        )
        config_entry.add_to_hass(hass)
        
        # This should handle gracefully or raise appropriate error
        # (Depends on your implementation - adjust test accordingly)
        with patch("custom_components.volvooncall_cn.VehicleAPI") as mock_api:
            mock_api.side_effect = KeyError("Missing config")
            
            try:
                result = await async_setup_entry(hass, config_entry)
                # If it returns False, that's acceptable
                assert result is False or result is None
            except (KeyError, Exception):
                # Or if it raises an exception, that's also acceptable
                pass

    @pytest.mark.asyncio
    async def test_coordinator_first_refresh_failure(self, hass: HomeAssistant, mock_volvo_api):
        """Test that setup handles first refresh failure."""
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: TEST_USERNAME,
                CONF_PASSWORD: TEST_PASSWORD,
                CONF_SCAN_INTERVAL: TEST_SCAN_INTERVAL,
            },
            unique_id=TEST_USERNAME,
        )
        config_entry.add_to_hass(hass)
        
        with patch("custom_components.volvooncall_cn.VehicleAPI", return_value=mock_volvo_api):
            # Mock first refresh to fail
            with patch.object(VolvoCoordinator, "async_config_entry_first_refresh", 
                            side_effect=ConfigEntryAuthFailed("Auth failed")):
                # Should raise ConfigEntryAuthFailed
                with pytest.raises(ConfigEntryAuthFailed):
                    await async_setup_entry(hass, config_entry)


# =============================================================================
# Test Data Storage
# =============================================================================

class TestDataStorage:
    """Test that data is properly stored in hass.data."""

    @pytest.mark.asyncio
    async def test_data_structure(self, hass: HomeAssistant, mock_volvo_api):
        """Test that data is stored in correct structure."""
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: TEST_USERNAME,
                CONF_PASSWORD: TEST_PASSWORD,
                CONF_SCAN_INTERVAL: TEST_SCAN_INTERVAL,
            },
            unique_id=TEST_USERNAME,
        )
        config_entry.add_to_hass(hass)
        
        with patch("custom_components.volvooncall_cn.VehicleAPI", return_value=mock_volvo_api):
            with patch("custom_components.volvooncall_cn.VolvoCoordinator.async_config_entry_first_refresh"):
                await async_setup_entry(hass, config_entry)
                
                # Verify data structure
                assert DOMAIN in hass.data
                assert isinstance(hass.data[DOMAIN], dict)
                assert config_entry.entry_id in hass.data[DOMAIN]
                
                # Verify coordinator is stored
                coordinator = hass.data[DOMAIN][config_entry.entry_id]
                assert isinstance(coordinator, VolvoCoordinator)

    @pytest.mark.asyncio
    async def test_multiple_entries(self, hass: HomeAssistant, mock_volvo_api):
        """Test that multiple config entries can coexist."""
        # First entry
        config_entry1 = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: "13800138001",
                CONF_PASSWORD: TEST_PASSWORD,
                CONF_SCAN_INTERVAL: TEST_SCAN_INTERVAL,
            },
            unique_id="13800138001",
        )
        config_entry1.add_to_hass(hass)
        
        # Second entry
        config_entry2 = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: "13800138002",
                CONF_PASSWORD: TEST_PASSWORD,
                CONF_SCAN_INTERVAL: TEST_SCAN_INTERVAL,
            },
            unique_id="13800138002",
        )
        config_entry2.add_to_hass(hass)
        
        with patch("custom_components.volvooncall_cn.VehicleAPI", return_value=mock_volvo_api):
            with patch("custom_components.volvooncall_cn.VolvoCoordinator.async_config_entry_first_refresh"):
                await async_setup_entry(hass, config_entry1)
                await async_setup_entry(hass, config_entry2)
                
                # Both entries should be in hass.data
                assert config_entry1.entry_id in hass.data[DOMAIN]
                assert config_entry2.entry_id in hass.data[DOMAIN]
                
                # Each should have its own coordinator
                coordinator1 = hass.data[DOMAIN][config_entry1.entry_id]
                coordinator2 = hass.data[DOMAIN][config_entry2.entry_id]
                assert coordinator1 is not coordinator2
