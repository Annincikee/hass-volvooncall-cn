# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview
This is a Home Assistant integration for Volvo On Call CN (China version), which connects to Volvo vehicles and exposes their data and controls as Home Assistant entities. The integration uses the Chinese version of Volvo's API to fetch vehicle data and send commands.

## Architecture
- **Home Assistant Integration**: Custom integration using the standard Home Assistant integration structure
- **API Implementation**: 
  - Uses gRPC to communicate with Volvo's backend services via auto-generated proto files
  - Core API layer in `volvooncall_cn.py` implements vehicle data retrieval and commands
  - Authentication and REST API calls handled in `volvooncall_base.py`
- **Data Management**:
  - Follows the Home Assistant `DataUpdateCoordinator` pattern for efficient polling
  - Entity definitions in platform-specific files (sensor.py, binary_sensor.py, etc.)
  - Configuration flow implemented in `config_flow.py`

## Key Components
1. **Authentication & API**:
   - `VehicleBaseAPI` class: Handles authentication, token management, and REST API calls
   - `VehicleAPI` class: Extends base API with gRPC communication for vehicle data
   - `Vehicle` class: Represents a vehicle with properties and methods for data and controls

2. **Proto Files & gRPC**:
   - Proto files in `/proto/` define the service interfaces for Volvo's gRPC API
   - Generated Python modules in `/proto/` contain stubs and message classes
   - Key services: exterior, health, fuel, invocation, odometer, location, etc.

3. **Home Assistant Integration**:
   - `VolvoCoordinator`: Central data coordinator that fetches and caches vehicle data
   - Entity platforms: Expose vehicle data as Home Assistant entities (sensors, switches, etc.)
   - `metaMap` in `__init__.py`: Defines entity metadata for all exposed vehicle properties

4. **Configuration**:
   - `config_flow.py`: Implements the config flow for adding the integration via UI
   - Supports username/password authentication with scan interval configuration

## Development Workflow

### Setup and Dependencies
```bash
# Clone repository
git clone https://github.com/idreamshen/hass-volvooncall-cn.git

# Install required Python packages
pip install grpcio>=1.67.1 grpcio-tools>=1.67.1

# Install for development in Home Assistant
# Copy to config/custom_components/ directory of your Home Assistant installation
```

### Proto Files
If you need to update proto files or regenerate Python code:

```bash
# From the proto directory
python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. *.proto
```

### Development Process
1. Make changes to the code
2. Test within a Home Assistant development environment
3. Verify with actual Volvo vehicles if possible

## Common Development Tasks

### Adding a New Sensor
1. Add entity metadata to `metaMap` in `__init__.py`
2. Add property to `Vehicle` class in `volvooncall_cn.py`
3. Update the appropriate platform file (e.g., `sensor.py`, `binary_sensor.py`)
4. Add data parsing in the corresponding `_parse_*` method in `Vehicle` class

### Adding a New Command
1. Add method to `Vehicle` class in `volvooncall_cn.py`
2. Add corresponding API method in `VehicleAPI` class
3. Expose via appropriate entity (button, switch, etc.)

### Debugging
- Enable debug logging for the component in Home Assistant configuration:
```yaml
logger:
  default: info
  logs:
    custom_components.volvooncall_cn: debug
```

## Testing
Testing is primarily done manually through Home Assistant. No automated testing framework exists.

## Deployment
1. Install via HACS as a custom repository:
   - URL: https://github.com/idreamshen/hass-volvooncall-cn
   - Category: Integration
2. Add integration through Home Assistant UI
3. Configure with Volvo account credentials (phone number and password)

## Supported Features
The integration supports a wide range of vehicle data and controls, including:
- Vehicle lock status and control
- Door, window, sunroof, tailgate status
- Engine status and remote start/stop
- Fuel level and consumption
- Odometer reading
- Vehicle position (with WGS84 coordinates)
- Honk and flash commands
- Various vehicle warnings (fluid levels, tire pressure, service warnings)