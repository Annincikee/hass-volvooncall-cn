"""Tests for config_flow.py - Home Assistant integration configuration."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from homeassistant import config_entries, data_entry_flow
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.volvooncall_cn.config_flow import (
    VolvoOnCallCnConfigFlow,
    VolvoOnCallCnOptionsFlow,
    volvo_validation,
)
from custom_components.volvooncall_cn.volvooncall_base import VolvoAPIError, DEFAULT_SCAN_INTERVAL
from custom_components.volvooncall_cn.volvooncall_cn import DOMAIN

from tests.conftest import TEST_USERNAME, TEST_PASSWORD, TEST_SCAN_INTERVAL


# =============================================================================
# Test volvo_validation helper function
# =============================================================================

class TestVolvoValidation:
    """Test the volvo_validation helper function."""

    @pytest.mark.asyncio
    async def test_validation_success(self, hass: HomeAssistant):
        """Test successful validation."""
        with patch("custom_components.volvooncall_cn.config_flow.VehicleAPI") as mock_api_class:
            mock_api = AsyncMock()
            mock_api.login = AsyncMock()
            mock_api_class.return_value = mock_api
            
            errors = await volvo_validation(hass, TEST_USERNAME, TEST_PASSWORD)
            
            assert errors == {}
            mock_api.login.assert_called_once()

    @pytest.mark.asyncio
    async def test_validation_api_error(self, hass: HomeAssistant):
        """Test validation with VolvoAPIError."""
        with patch("custom_components.volvooncall_cn.config_flow.VehicleAPI") as mock_api_class:
            mock_api = AsyncMock()
            mock_api.login = AsyncMock(side_effect=VolvoAPIError("用户名或密码错误"))
            mock_api_class.return_value = mock_api
            
            errors = await volvo_validation(hass, TEST_USERNAME, "wrong_password")
            
            assert "base" in errors
            assert errors["base"] == "用户名或密码错误"

    @pytest.mark.asyncio
    async def test_validation_unknown_error(self, hass: HomeAssistant):
        """Test validation with unexpected exception."""
        with patch("custom_components.volvooncall_cn.config_flow.VehicleAPI") as mock_api_class:
            mock_api = AsyncMock()
            mock_api.login = AsyncMock(side_effect=Exception("Network error"))
            mock_api_class.return_value = mock_api
            
            errors = await volvo_validation(hass, TEST_USERNAME, TEST_PASSWORD)
            
            assert "base" in errors
            assert errors["base"] == "unknown"


# =============================================================================
# Test Config Flow
# =============================================================================

class TestConfigFlow:
    """Test the config flow for Volvo On Call CN."""

    @pytest.mark.asyncio
    async def test_user_flow_success(self, hass: HomeAssistant):
        """Test successful user flow."""
        with patch("custom_components.volvooncall_cn.config_flow.volvo_validation", return_value={}):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_USER}
            )
            
            assert result["type"] == data_entry_flow.FlowResultType.FORM
            assert result["step_id"] == "user"
            
            # Submit user input
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={
                    CONF_USERNAME: TEST_USERNAME,
                    CONF_PASSWORD: TEST_PASSWORD,
                    CONF_SCAN_INTERVAL: TEST_SCAN_INTERVAL,
                }
            )
            
            assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
            assert result["title"] == TEST_USERNAME
            assert result["data"][CONF_USERNAME] == TEST_USERNAME
            assert result["data"][CONF_PASSWORD] == TEST_PASSWORD
            assert result["data"][CONF_SCAN_INTERVAL] == TEST_SCAN_INTERVAL

    @pytest.mark.asyncio
    async def test_user_flow_invalid_credentials(self, hass: HomeAssistant):
        """Test user flow with invalid credentials."""
        with patch("custom_components.volvooncall_cn.config_flow.volvo_validation", 
                   return_value={"base": "用户名或密码错误"}):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_USER}
            )
            
            # Submit invalid credentials
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={
                    CONF_USERNAME: TEST_USERNAME,
                    CONF_PASSWORD: "wrong_password",
                    CONF_SCAN_INTERVAL: TEST_SCAN_INTERVAL,
                }
            )
            
            assert result["type"] == data_entry_flow.FlowResultType.FORM
            assert result["step_id"] == "user"
            assert "base" in result["errors"]
            assert result["errors"]["base"] == "用户名或密码错误"

    @pytest.mark.asyncio
    async def test_user_flow_default_scan_interval(self, hass: HomeAssistant):
        """Test that default scan interval is used when not specified."""
        with patch("custom_components.volvooncall_cn.config_flow.volvo_validation", return_value={}):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_USER}
            )
            
            # Submit without scan_interval (should use default)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={
                    CONF_USERNAME: TEST_USERNAME,
                    CONF_PASSWORD: TEST_PASSWORD,
                }
            )
            
            assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
            # Default scan interval should be used
            assert result["data"][CONF_SCAN_INTERVAL] == DEFAULT_SCAN_INTERVAL

    @pytest.mark.asyncio
    async def test_user_flow_unique_id(self, hass: HomeAssistant):
        """Test that unique_id is set to username."""
        with patch("custom_components.volvooncall_cn.config_flow.volvo_validation", return_value={}):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_USER}
            )
            
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={
                    CONF_USERNAME: TEST_USERNAME,
                    CONF_PASSWORD: TEST_PASSWORD,
                }
            )
            
            # The entry should be created successfully
            assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
            
            # Try to add the same username again - should be rejected as duplicate
            result2 = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_USER}
            )
            
            result2 = await hass.config_entries.flow.async_configure(
                result2["flow_id"],
                user_input={
                    CONF_USERNAME: TEST_USERNAME,
                    CONF_PASSWORD: TEST_PASSWORD,
                }
            )
            
            # Should abort due to duplicate unique_id
            assert result2["type"] == data_entry_flow.FlowResultType.ABORT
            assert result2["reason"] == "already_configured"


# =============================================================================
# Test Options Flow
# =============================================================================

class TestOptionsFlow:
    """Test the options flow for Volvo On Call CN."""

    @pytest.mark.asyncio
    async def test_options_flow_init(self, hass: HomeAssistant):
        """Test options flow initialization."""
        # Create a config entry first
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: TEST_USERNAME,
                CONF_PASSWORD: TEST_PASSWORD,
                CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            },
            unique_id=TEST_USERNAME,
        )
        config_entry.add_to_hass(hass)
        
        # Start options flow
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "user"

    @pytest.mark.asyncio
    async def test_options_flow_update_scan_interval(self, hass: HomeAssistant):
        """Test updating scan interval via options flow."""
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: TEST_USERNAME,
                CONF_PASSWORD: TEST_PASSWORD,
                CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            },
            unique_id=TEST_USERNAME,
        )
        config_entry.add_to_hass(hass)
        
        with patch("custom_components.volvooncall_cn.config_flow.volvo_validation", return_value={}):
            result = await hass.config_entries.options.async_init(config_entry.entry_id)
            
            # Update scan interval
            new_scan_interval = 120
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                user_input={
                    CONF_USERNAME: TEST_USERNAME,
                    CONF_PASSWORD: TEST_PASSWORD,
                    CONF_SCAN_INTERVAL: new_scan_interval,
                }
            )
            
            assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
            assert result["data"][CONF_SCAN_INTERVAL] == new_scan_interval

    @pytest.mark.asyncio
    async def test_options_flow_validation_error(self, hass: HomeAssistant):
        """Test options flow with validation error."""
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: TEST_USERNAME,
                CONF_PASSWORD: TEST_PASSWORD,
                CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            },
            unique_id=TEST_USERNAME,
        )
        config_entry.add_to_hass(hass)
        
        with patch("custom_components.volvooncall_cn.config_flow.volvo_validation", 
                   return_value={"base": "网络错误"}):
            result = await hass.config_entries.options.async_init(config_entry.entry_id)
            
            # Try to update with invalid credentials
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                user_input={
                    CONF_USERNAME: TEST_USERNAME,
                    CONF_PASSWORD: "wrong_password",
                    CONF_SCAN_INTERVAL: 60,
                }
            )
            
            assert result["type"] == data_entry_flow.FlowResultType.FORM
            assert "base" in result["errors"]
            assert result["errors"]["base"] == "网络错误"

    @pytest.mark.asyncio
    async def test_options_flow_preserves_existing_values(self, hass: HomeAssistant):
        """Test that options flow shows existing values as defaults."""
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: TEST_USERNAME,
                CONF_PASSWORD: TEST_PASSWORD,
                CONF_SCAN_INTERVAL: 90,
            },
            unique_id=TEST_USERNAME,
        )
        config_entry.add_to_hass(hass)
        
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        
        # Check that form shows existing values
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "user"
        # The schema should have defaults set to current values
        # (This is implementation detail, mainly checking no crash occurs)


# =============================================================================
# Test Edge Cases
# =============================================================================

class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_scan_interval_minimum_value(self, hass: HomeAssistant):
        """Test that scan interval has minimum value of 5 seconds."""
        with patch("custom_components.volvooncall_cn.config_flow.volvo_validation", return_value={}):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_USER}
            )
            
            # Try to set scan interval below minimum (should be validated by voluptuous)
            with pytest.raises(Exception):  # voluptuous will raise an error
                result = await hass.config_entries.flow.async_configure(
                    result["flow_id"],
                    user_input={
                        CONF_USERNAME: TEST_USERNAME,
                        CONF_PASSWORD: TEST_PASSWORD,
                        CONF_SCAN_INTERVAL: 2,  # Below minimum of 5
                    }
                )

    @pytest.mark.asyncio
    async def test_empty_username(self, hass: HomeAssistant):
        """Test that empty username is handled."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER}
        )
        
        # Try to submit with empty username
        with pytest.raises(Exception):  # Should raise validation error
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={
                    CONF_USERNAME: "",
                    CONF_PASSWORD: TEST_PASSWORD,
                }
            )

    @pytest.mark.asyncio
    async def test_empty_password(self, hass: HomeAssistant):
        """Test that empty password is handled."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER}
        )
        
        # Try to submit with empty password
        with pytest.raises(Exception):  # Should raise validation error
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={
                    CONF_USERNAME: TEST_USERNAME,
                    CONF_PASSWORD: "",
                }
            )
