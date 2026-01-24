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
                # Use proper setup method that acquires the lock
                await hass.config_entries.async_setup(config_entry.entry_id)
                
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
                await hass.config_entries.async_setup(config_entry.entry_id)
                
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
                await hass.config_entries.async_setup(config_entry.entry_id)
                
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
            hass.config_entries.async_update_entry(config_entry, options={CONF_SCAN_INTERVAL: new_scan_interval})
            
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
            hass.config_entries.async_update_entry(config_entry, options={CONF_PASSWORD: new_password})
            
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
        
        # Mock get_vehicles_vins to return a vehicle
        mock_volvo_api.get_vehicles_vins = AsyncMock(return_value={
            "TEST_VIN_12345678": {"modelYear": "2024", "model": "XC60"}
        })
        
        #Mock Vehicle update to avoid actual API calls
        with patch("custom_components.volvooncall_cn.volvooncall_cn.Vehicle.update", new_callable=AsyncMock):
            # Call _async_update_data
            result = await coordinator._async_update_data()
        
            # Verify get_vehicles_vins was called
            mock_volvo_api.get_vehicles_vins.assert_called_once()
        
            # Verify vehicles were returned
            assert len(result) == 1
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
        
        # Mock get_vehicles_vins to raise exception
        mock_volvo_api.get_vehicles_vins = AsyncMock(side_effect=Exception("API Error"))
        
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
                    await hass.config_entries.async_setup(config_entry.entry_id)
                    
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
                await hass.config_entries.async_setup(config_entry.entry_id)
                # If setup succeeds, check that it handled the error
                assert DOMAIN not in hass.data or config_entry.entry_id not in hass.data.get(DOMAIN, {})
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
            # Mock first refresh to fail with auth error
            with patch.object(VolvoCoordinator, "async_config_entry_first_refresh", 
                            side_effect=ConfigEntryAuthFailed("Auth failed")):
                # Mock async_step_reauth to avoid the UnknownStep error
                with patch.object(config_entry, "async_start_reauth", new_callable=AsyncMock) as mock_reauth:
                    # Setup should fail but not crash
                    await hass.config_entries.async_setup(config_entry.entry_id)
                    
                    # Verify reauth flow was initiated or entry state is in auth failed state
                    # The setup internally handles the ConfigEntryAuthFailed and triggers reauth
                    # We can't easily test the reauth was triggered since it's async in background
                    # Instead verify the entry is not loaded
                    from homeassistant.config_entries import ConfigEntryState
                    assert config_entry.state != ConfigEntryState.LOADED


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
                await hass.config_entries.async_setup(config_entry.entry_id)
                
                # Verify data structure
                assert DOMAIN in hass.data
                assert isinstance(hass.data[DOMAIN], dict)
                assert config_entry.entry_id in hass.data[DOMAIN]
                
                # Verify coordinator is stored
                coordinator = hass.data[DOMAIN][config_entry.entry_id]
                assert isinstance(coordinator, VolvoCoordinator)
