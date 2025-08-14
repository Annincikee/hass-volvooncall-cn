# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview
This is a Home Assistant integration for Volvo On Call CN (China version), which connects to Volvo vehicles and exposes their data and controls as Home Assistant entities.

## Architecture
- Home Assistant custom integration using the standard integration structure
- API implementation uses gRPC to communicate with Volvo's backend services
- Authentication and REST API calls handled in `volvooncall_base.py`
- Main API logic in `volvooncall_cn.py` uses proto files for service definitions
- Entity definitions in individual platform files (sensor.py, binary_sensor.py, etc.)
- Configuration flow implemented in `config_flow.py`

## Key Components
1. **Authentication**: Username/password authentication in `volvooncall_base.py`
2. **API Communication**: gRPC services for vehicle data and commands
3. **Data Updates**: Coordinator pattern for polling vehicle data at configured intervals
4. **Entity Management**: Standard Home Assistant entity classes for each sensor/control

## Development Commands
There are no specific build or development commands. The integration is developed directly as Python files.

## Common Development Tasks
1. Adding new sensors: Add to metaMap in `__init__.py` and create corresponding entity class
2. Adding new commands: Implement in `volvooncall_cn.py` and expose via button/switch entities
3. Protocol updates: Update .proto files and regenerate Python code (if needed)

## Testing
No automated testing framework exists. Testing is done manually through Home Assistant.

## Deployment
1. Install via HACS as a custom repository
2. Add integration through Home Assistant UI
3. Configure with Volvo account credentials