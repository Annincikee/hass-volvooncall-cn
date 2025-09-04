#!/usr/bin/env python3
import asyncio
import argparse
import logging
import json
import sys

from .volvooncall_base import VehicleBaseAPI
from .volvooncall_cn import VehicleAPI, Vehicle
from aiohttp import ClientSession

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

_LOGGER = logging.getLogger("volvooncall_test")


async def test_api(username, password, debug=False):
    """Test Volvo On Call CN API with the provided credentials."""
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)

    _LOGGER.info("Initializing Volvo On Call CN API test...")

    async with ClientSession() as session:
        try:
            # Initialize the API
            _LOGGER.info("Creating VehicleAPI instance...")
            api = VehicleAPI(session, username, password)

            # Authenticate
            _LOGGER.info("Authenticating with provided credentials...")
            await api.login()

            # Get vehicles
            _LOGGER.info("Retrieving vehicles...")
            vehicles_data = await api.get_vehicles()

            if not vehicles_data:
                _LOGGER.error("No vehicles found for this account.")
                return

            _LOGGER.info(f"Found {len(vehicles_data)} vehicles")

            # Display vehicle info
            vehicle_instances = []
            for idx, vehicle_data in enumerate(vehicles_data):
                vin = vehicle_data["vinCode"]
                _LOGGER.info(f"Vehicle {idx+1}: VIN: {vin}, Model: {vehicle_data.get('modelName', 'Unknown')}, Series: {vehicle_data.get('seriesName', 'Unknown')}")

                # Create Vehicle instance
                _LOGGER.info(f"Initializing Vehicle instance for VIN {vin}...")
                is_aaos = "isAaos" in vehicle_data and vehicle_data["isAaos"]
                vehicle = Vehicle(vin, api, is_aaos)
                vehicle_instances.append(vehicle)

                # Update vehicle data
                _LOGGER.info(f"Updating vehicle data for VIN {vin}...")
                await vehicle.update()

                # Print vehicle status
                print_vehicle_status(vehicle)

            # Keep the script running to allow for interactive testing
            if vehicle_instances:
                await interactive_menu(vehicle_instances)

        except Exception as e:
            _LOGGER.error(f"Error during API test: {e}", exc_info=True)


def print_vehicle_status(vehicle):
    """Print the current status of the vehicle."""
    _LOGGER.info(f"Status for vehicle: {vehicle.nickname or vehicle.model_name or vehicle.vin}")

    # Get all attributes of the vehicle object
    attributes = [attr for attr in dir(vehicle) if not attr.startswith('_') and not callable(getattr(vehicle, attr))]

    # Print only the data attributes (not functions)
    for attr in sorted(attributes):
        if attr not in ['vin', '_api', 'isAaos', 'series_name', 'model_name', 'nickname']:
            value = getattr(vehicle, attr)
            if isinstance(value, dict):
                _LOGGER.info(f"  {attr}: {json.dumps(value, indent=2)}")
            else:
                _LOGGER.info(f"  {attr}: {value}")


async def interactive_menu(vehicles):
    """Provide an interactive menu for testing vehicle commands."""
    selected_vehicle = vehicles[0] if len(vehicles) == 1 else None

    while True:
        if not selected_vehicle:
            print("\nAvailable vehicles:")
            for idx, vehicle in enumerate(vehicles):
                name = vehicle.nickname or vehicle.model_name or vehicle.vin
                print(f"{idx+1}. {name} (VIN: {vehicle.vin})")

            try:
                choice = int(input("\nSelect a vehicle (or 0 to quit): "))
                if choice == 0:
                    break
                if 1 <= choice <= len(vehicles):
                    selected_vehicle = vehicles[choice-1]
                else:
                    print("Invalid selection. Please try again.")
                    continue
            except ValueError:
                print("Invalid input. Please enter a number.")
                continue

        print("\nAvailable commands:")
        print("1. Update vehicle status")
        print("2. Lock vehicle")
        print("3. Unlock vehicle")
        print("4. Honk horn")
        print("5. Flash lights")
        print("6. Honk and flash")
        print("7. Start engine (10 min)")
        print("8. Stop engine")
        print("9. Open tailgate")
        print("10. Close tailgate")
        print("11. Open sunroof")
        print("12. Close sunroof")
        print("13. Select different vehicle")
        print("0. Exit")

        try:
            choice = int(input("\nEnter command: "))

            if choice == 0:
                break
            elif choice == 1:
                print("Updating vehicle status...")
                await selected_vehicle.update()
                print_vehicle_status(selected_vehicle)
            elif choice == 2:
                print("Locking vehicle...")
                await selected_vehicle.lock_vehicle()
                print("Command sent successfully.")
            elif choice == 3:
                print("Unlocking vehicle...")
                await selected_vehicle.unlock_vehicle()
                print("Command sent successfully.")
            elif choice == 4:
                print("Honking horn...")
                await selected_vehicle.honk()
                print("Command sent successfully.")
            elif choice == 5:
                print("Flashing lights...")
                await selected_vehicle.flash()
                print("Command sent successfully.")
            elif choice == 6:
                print("Honking and flashing...")
                await selected_vehicle.honk_and_flash()
                print("Command sent successfully.")
            elif choice == 7:
                print("Starting engine (10 min)...")
                await selected_vehicle.engine_start(10)
                print("Command sent successfully.")
            elif choice == 8:
                print("Stopping engine...")
                await selected_vehicle.engine_stop()
                print("Command sent successfully.")
            elif choice == 9:
                print("Opening tailgate...")
                await selected_vehicle.tail_gate_control_open()
                print("Command sent successfully.")
            elif choice == 10:
                print("Closing tailgate...")
                await selected_vehicle.tail_gate_control_close()
                print("Command sent successfully.")
            elif choice == 11:
                print("Opening sunroof...")
                await selected_vehicle.sunroof_control_open()
                print("Command sent successfully.")
            elif choice == 12:
                print("Closing sunroof...")
                await selected_vehicle.sunroof_control_close()
                print("Command sent successfully.")
            elif choice == 13:
                selected_vehicle = None
                continue
            else:
                print("Invalid command. Please try again.")
        except ValueError:
            print("Invalid input. Please enter a number.")
        except Exception as e:
            print(f"Error executing command: {e}")

        if choice != 13:
            input("\nPress Enter to continue...")


def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(description='Test Volvo On Call CN API')
    parser.add_argument('--username', required=True, help='Phone number (without country code)')
    parser.add_argument('--password', required=True, help='Password')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')

    args = parser.parse_args()

    # Run the test
    asyncio.run(test_api(args.username, args.password, args.debug))


if __name__ == "__main__":
    main()
