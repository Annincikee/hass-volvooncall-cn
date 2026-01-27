"""Fixtures and configuration for pytest."""

# NOTE: The upstream CI uses pytest-homeassistant-custom-component's strict
# cleanup checks which fail if *any* non-whitelisted thread is left running.
# grpcio starts a background "safe shutdown" thread ("_run_safe_shutdown_loop")
# once grpc is imported/initialized, and it is intentionally long-lived.
#
# For this integration we allow that thread in tests by filtering it out of
# threading.enumerate(), so the cleanup plugin doesn't treat it as a leak.

import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import json


def pytest_configure(config):
    """Patch threading.enumerate early to ignore grpc's long-lived shutdown thread."""

    original_enumerate = threading.enumerate

    def filtered_enumerate():
        threads = original_enumerate()
        return [
            t
            for t in threads
            if "_run_safe_shutdown_loop" not in getattr(t, "name", "")
        ]

    threading.enumerate = filtered_enumerate

# Add custom_components to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import from pytest-homeassistant-custom-component
pytest_plugins = "pytest_homeassistant_custom_component"


# ============================================================================
# Essential Fixtures for Home Assistant Testing
# ============================================================================

@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations for all tests.
    
    This fixture is required for Home Assistant >= 2021.6.0
    to allow loading custom components during tests.
    """
    yield


@pytest.fixture
def load_fixture():
    """Load a fixture file and return its content."""
    def _load_fixture(filename):
        """Load fixture data from tests/fixtures directory."""
        path = Path(__file__).parent / "fixtures" / filename
        with open(path, encoding="utf-8") as file:
            return file.read()
    return _load_fixture


@pytest.fixture
def load_json_fixture(load_fixture):
    """Load a JSON fixture file."""
    def _load_json_fixture(filename):
        """Load JSON fixture data."""
        data = load_fixture(filename)
        return json.loads(data)
    return _load_json_fixture


# ============================================================================
# Mock API Responses
# ============================================================================

@pytest.fixture
def mock_auth_response():
    """Mock authentication response."""
    return {
        "success": True,
        "data": {
            "refreshToken": "mock_refresh_token",
            "token": "mock_access_token",
            "xToken": "mock_x_token"
        }
    }


@pytest.fixture
def mock_vehicles_response():
    """Mock vehicles list response."""
    return {
        "success": True,
        "data": [
            {
                "vin": "TEST_VIN_12345678",
                "vehicleName": "测试车辆",
                "modelYear": 2024,
                "model": "XC60"
            }
        ]
    }


@pytest.fixture
def mock_vocapi_token_response():
    """Mock VOC API token response."""
    return {
        "success": True,
        "data": {
            "token": "mock_vocapi_access_token"
        }
    }


# ============================================================================
# Mock gRPC Responses
# ============================================================================

@pytest.fixture
def mock_exterior_response():
    """Mock exterior status gRPC response."""
    mock_response = MagicMock()
    mock_response.exteriorStatus.lockStatus = 2  # LOCKED
    mock_response.exteriorStatus.frontLeftDoor = 1  # CLOSED
    mock_response.exteriorStatus.frontRightDoor = 1  # CLOSED
    mock_response.exteriorStatus.rearLeftDoor = 1  # CLOSED
    mock_response.exteriorStatus.rearRightDoor = 1  # CLOSED
    mock_response.exteriorStatus.hood = 1  # CLOSED
    mock_response.exteriorStatus.tailGate = 1  # CLOSED
    mock_response.exteriorStatus.frontLeftWindow = 1  # CLOSED
    mock_response.exteriorStatus.frontRightWindow = 1  # CLOSED
    mock_response.exteriorStatus.rearLeftWindow = 1  # CLOSED
    mock_response.exteriorStatus.rearRightWindow = 1  # CLOSED
    mock_response.exteriorStatus.sunroof = 1  # CLOSED
    return mock_response


@pytest.fixture
def mock_health_response():
    """Mock health status gRPC response."""
    mock_response = MagicMock()
    mock_response.healthStatus.engineRunning = False
    mock_response.healthStatus.brakeFluidLevelWarning = False
    mock_response.healthStatus.engineCoolantLevelWarning = False
    mock_response.healthStatus.oilLevelWarning = False
    mock_response.healthStatus.washerFluidLevelWarning = False
    mock_response.healthStatus.frontLeftTyrePressureWarning = False
    mock_response.healthStatus.frontRightTyrePressureWarning = False
    mock_response.healthStatus.rearLeftTyrePressureWarning = False
    mock_response.healthStatus.rearRightTyrePressureWarning = False
    mock_response.healthStatus.serviceWarningStatus = 0  # NO_WARNING
    return mock_response


@pytest.fixture
def mock_fuel_response():
    """Mock fuel status gRPC response."""
    mock_response = MagicMock()
    mock_response.fuelAmount = 45.5
    mock_response.distanceToEmpty = 580
    mock_response.averageConsumption = 7.8
    return mock_response


@pytest.fixture
def mock_odometer_response():
    """Mock odometer gRPC response."""
    mock_response = MagicMock()
    mock_response.odometer = 12345
    return mock_response


@pytest.fixture
def mock_location_response():
    """Mock location gRPC response."""
    mock_response = MagicMock()
    # Using GCJ02 coordinates (China GPS offset system)
    mock_response.latitude = 39.9042  # Beijing example
    mock_response.longitude = 116.4074
    mock_response.heading = 180
    mock_response.timestamp = 1234567890
    return mock_response


# ============================================================================
# Mock API Classes
# ============================================================================

@pytest.fixture
def mock_volvo_api():
    """Create a mock VehicleAPI instance."""
    mock_api = AsyncMock()
    mock_api.login = AsyncMock()
    mock_api.update_token = AsyncMock()
    mock_api.get_vehicles = AsyncMock(return_value=[])
    mock_api.get_vehicles_vins = AsyncMock(return_value={})
    mock_api._refresh_token = "mock_refresh_token"
    mock_api._digitalvolvo_access_token = "mock_access_token"
    mock_api._vocapi_access_token = "mock_vocapi_token"
    return mock_api


@pytest.fixture
def mock_vehicle():
    """Create a mock Vehicle instance."""
    mock_vehicle = MagicMock()
    mock_vehicle.vin = "TEST_VIN_12345678"
    mock_vehicle.vehicle_name = "测试车辆"
    mock_vehicle.model_year = 2024
    mock_vehicle.model = "XC60"
    
    # Mock vehicle properties
    mock_vehicle.is_locked = True
    mock_vehicle.is_engine_running = False
    mock_vehicle.fuel_amount = 45.5
    mock_vehicle.distance_to_empty = 580
    mock_vehicle.odometer = 12345
    
    # Mock vehicle methods
    mock_vehicle.lock = AsyncMock()
    mock_vehicle.unlock = AsyncMock()
    mock_vehicle.start_engine = AsyncMock()
    mock_vehicle.stop_engine = AsyncMock()
    mock_vehicle.honk_and_flash = AsyncMock()
    
    return mock_vehicle


# ============================================================================
# Mock Home Assistant Session
# ============================================================================

@pytest.fixture
def mock_aiohttp_session():
    """Create a mock aiohttp ClientSession."""
    session = AsyncMock()
    
    # Mock response
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    
    # Mock request method
    session.request = AsyncMock(return_value=mock_response)
    session.get = AsyncMock(return_value=mock_response)
    session.post = AsyncMock(return_value=mock_response)
    
    return session


# ============================================================================
# Test Constants
# ============================================================================

TEST_USERNAME = "13800138000"
TEST_PASSWORD = "test_password"
TEST_VIN = "TEST_VIN_12345678"
TEST_SCAN_INTERVAL = 60
