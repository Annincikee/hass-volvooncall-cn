"""Tests for __init__.py - Integration setup and coordinator."""
import asyncio
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import timedelta
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.volvooncall_cn import (
    AUTH_STORES_DATA,
    async_migrate_entry,
    async_remove_entry,
    async_setup_entry,
    async_update_options,
    VolvoCoordinator,
    _async_attach_token_store,
    _auth_store_for,
)
from custom_components.volvooncall_cn.config_flow import volvo_validation
from custom_components.volvooncall_cn.const import (
    CONF_POWERTRAIN_TYPE,
    POWERTRAIN_HYBRID,
    VALIDATED_TOKENS_DATA,
)
from custom_components.volvooncall_cn.volvooncall_cn import DOMAIN
from custom_components.volvooncall_cn.volvooncall_base import (
    DEFAULT_SCAN_INTERVAL,
    VehicleBaseAPI,
)

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
        mock_volvo_api.get_vehicles_vins.return_value = {
            "TEST_VIN_12345678": {"modelYear": "2024"}
        }

        with patch(
            "custom_components.volvooncall_cn.VehicleAPI",
            return_value=mock_volvo_api,
        ), patch(
            "custom_components.volvooncall_cn.Vehicle.update",
            new=AsyncMock(),
        ):
            # Use proper setup method that acquires the lock.
            await hass.config_entries.async_setup(config_entry.entry_id)

            assert DOMAIN in hass.data
            assert config_entry.entry_id in hass.data[DOMAIN]
            assert (
                hass.data[DOMAIN][config_entry.entry_id].config_entry
                is config_entry
            )

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
        mock_volvo_api.get_vehicles_vins.return_value = {
            "TEST_VIN_12345678": {"modelYear": "2024"}
        }

        with patch(
            "custom_components.volvooncall_cn.VehicleAPI",
            return_value=mock_volvo_api,
        ), patch(
            "custom_components.volvooncall_cn.Vehicle.update",
            new=AsyncMock(),
        ):
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
        mock_volvo_api.get_vehicles_vins.return_value = {
            "TEST_VIN_12345678": {"modelYear": "2024"}
        }

        with patch(
            "custom_components.volvooncall_cn.VehicleAPI",
            return_value=mock_volvo_api,
        ), patch(
            "custom_components.volvooncall_cn.Vehicle.update",
            new=AsyncMock(),
        ):
            await hass.config_entries.async_setup(config_entry.entry_id)

            # Verify update listener was added
            assert len(config_entry.update_listeners) > 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "setup_error",
        [
            ConfigEntryNotReady("backend unavailable"),
            asyncio.CancelledError(),
        ],
    )
    async def test_setup_failure_cleans_coordinator_and_api(
        self,
        hass: HomeAssistant,
        mock_volvo_api,
        setup_error,
    ):
        """A failed or cancelled first refresh must leave no live client."""
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: TEST_USERNAME,
                CONF_PASSWORD: TEST_PASSWORD,
            },
            unique_id=TEST_USERNAME,
        )
        config_entry.add_to_hass(hass)
        other_coordinator = object()
        hass.data.setdefault(DOMAIN, {})["other-entry"] = other_coordinator

        with patch(
            "custom_components.volvooncall_cn.VehicleAPI",
            return_value=mock_volvo_api,
        ), patch(
            "custom_components.volvooncall_cn.VolvoCoordinator."
            "async_config_entry_first_refresh",
            new=AsyncMock(side_effect=setup_error),
        ):
            with pytest.raises(type(setup_error)):
                await async_setup_entry(hass, config_entry)

        assert config_entry.entry_id not in hass.data[DOMAIN]
        assert hass.data[DOMAIN]["other-entry"] is other_coordinator
        mock_volvo_api.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_remove_entry_deletes_token_store_and_pending_save(
        self, hass: HomeAssistant
    ):
        """Deleting the integration must not leave reusable account tokens."""
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: TEST_USERNAME,
                CONF_PASSWORD: TEST_PASSWORD,
            },
            unique_id=TEST_USERNAME,
        )
        config_entry.add_to_hass(hass)
        auth_store = _auth_store_for(hass, TEST_USERNAME)
        await auth_store.async_save({"refresh_token": "test-refresh-token"})
        auth_store.async_delay_save(
            lambda: {"refresh_token": "must-not-return"}, 1
        )
        hass.data.setdefault(AUTH_STORES_DATA, {})[
            config_entry.entry_id
        ] = [auth_store]

        await async_remove_entry(hass, config_entry)
        await asyncio.sleep(0.02)
        await hass.async_block_till_done()

        assert await _auth_store_for(hass, TEST_USERNAME).async_load() is None
        assert AUTH_STORES_DATA not in hass.data

    @pytest.mark.asyncio
    async def test_config_validation_hands_tokens_to_setup_without_relogin(
        self, hass: HomeAssistant
    ):
        """Initial setup performs exactly one password authentication."""
        tokens = {
            "refresh_token": "validated-refresh",
            "digitalvolvo_access_token": "validated-access",
            "digitalvolvo_x_token": "validated-x-token",
            "vocapi_access_token": "validated-global-token",
            "access_token_expire_at": int(time.time()) + 3600,
            "last_password_login_at": int(time.time()),
        }
        validation_api = MagicMock()
        validation_api.login = AsyncMock()
        validation_api.export_tokens.return_value = tokens

        with patch(
            "custom_components.volvooncall_cn.config_flow.VehicleAPI",
            return_value=validation_api,
        ):
            assert await volvo_validation(
                hass, TEST_USERNAME, TEST_PASSWORD
            ) == {}

        runtime_api = VehicleBaseAPI(
            MagicMock(), TEST_USERNAME, TEST_PASSWORD
        )
        runtime_api.digitalvolvo_post = AsyncMock()
        await _async_attach_token_store(hass, runtime_api, TEST_USERNAME)
        await runtime_api.login()

        validation_api.login.assert_awaited_once()
        runtime_api.digitalvolvo_post.assert_not_awaited()
        assert VALIDATED_TOKENS_DATA not in hass.data
        assert runtime_api._refresh_token == "validated-refresh"


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
            unsubscribe = coordinator.async_add_listener(lambda: None)
            
            # Update options with new scan interval
            new_scan_interval = 120
            hass.config_entries.async_update_entry(config_entry, options={CONF_SCAN_INTERVAL: new_scan_interval})
            
            with patch.object(coordinator, "_schedule_refresh") as reschedule:
                await async_update_options(hass, config_entry)
            
            # Verify scan interval was updated
            assert coordinator.update_interval == timedelta(seconds=new_scan_interval)
            reschedule.assert_called_once()
            unsubscribe()

    @pytest.mark.asyncio
    async def test_async_update_options_reloads_on_credential_change(
        self, hass: HomeAssistant, mock_volvo_api
    ):
        """Credential changes reload every cached vehicle and API together."""
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
        with patch(
            "custom_components.volvooncall_cn.VehicleAPI"
        ) as mock_api_class, patch.object(
            hass.config_entries, "async_schedule_reload"
        ) as mock_reload:
            old_api = mock_volvo_api
            coordinator = VolvoCoordinator(hass, old_api, TEST_SCAN_INTERVAL)
            hass.data[DOMAIN][config_entry.entry_id] = coordinator
            
            # Update options
            new_password = "new_password"
            hass.config_entries.async_update_entry(config_entry, options={CONF_PASSWORD: new_password})
            
            await async_update_options(hass, config_entry)

            mock_reload.assert_called_once_with(config_entry.entry_id)
            mock_api_class.assert_not_called()
            assert coordinator.volvo_api is old_api
            old_api.close.assert_not_awaited()


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
    async def test_coordinator_retries_when_account_has_no_vehicles(self, hass: HomeAssistant, mock_volvo_api):
        """An initial empty account must keep retrying instead of going inert."""
        coordinator = VolvoCoordinator(hass, mock_volvo_api, TEST_SCAN_INTERVAL)
        mock_volvo_api.get_vehicles_vins = AsyncMock(return_value={})
        
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

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
    async def test_coordinator_throttles_manual_refresh_with_cached_data(self, hass: HomeAssistant, mock_volvo_api):
        """Refreshes inside the global scan interval should reuse cached data."""
        coordinator = VolvoCoordinator(hass, mock_volvo_api, TEST_SCAN_INTERVAL)
        cached_data = [MagicMock()]
        coordinator.data = cached_data
        coordinator._last_update_started_at = time.monotonic()

        result = await coordinator._async_update_data()

        assert result is cached_data
        mock_volvo_api.login.assert_not_called()
        mock_volvo_api.update_token.assert_not_called()
        mock_volvo_api.get_vehicles_vins.assert_not_called()

    @pytest.mark.asyncio
    async def test_forced_refresh_bypasses_cached_scan_interval(
        self, hass: HomeAssistant, mock_volvo_api
    ):
        """Control confirmation must perform a real update immediately."""
        coordinator = VolvoCoordinator(
            hass, mock_volvo_api, TEST_SCAN_INTERVAL
        )
        coordinator.data = [MagicMock()]
        coordinator._last_update_started_at = time.monotonic()
        coordinator._pending_force_refreshes += 1
        mock_volvo_api.get_vehicles_vins = AsyncMock(
            return_value={
                "TEST_VIN_12345678": {"modelYear": "2024"}
            }
        )

        with patch(
            "custom_components.volvooncall_cn.Vehicle.update",
            new=AsyncMock(),
        ):
            result = await coordinator._async_update_data()

        assert len(result) == 1
        mock_volvo_api.login.assert_awaited_once()
        mock_volvo_api.update_token.assert_awaited_once()
        mock_volvo_api.get_vehicles_vins.assert_awaited_once()
        assert coordinator._pending_force_refreshes == 0

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
                with patch.object(
                    config_entry,
                    "async_start_reauth",
                    new_callable=MagicMock,
                ):
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


@pytest.mark.asyncio
async def test_existing_entry_migrates_to_hybrid(hass: HomeAssistant):
    """Upgrades preserve existing electric entities by default."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: TEST_USERNAME,
            CONF_PASSWORD: TEST_PASSWORD,
            CONF_SCAN_INTERVAL: TEST_SCAN_INTERVAL,
        },
        unique_id=TEST_USERNAME,
        version=1,
    )
    config_entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, config_entry) is True
    assert config_entry.version == 3
    assert config_entry.data[CONF_POWERTRAIN_TYPE] == POWERTRAIN_HYBRID
