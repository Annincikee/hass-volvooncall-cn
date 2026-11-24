"""Regression tests for the audit hardening pass.

Each test pins one behaviour that was previously wrong. Naming a test after
the symptom keeps the reason for the fix discoverable from a failure message.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.volvooncall_cn import (
    CHARGE_LIMIT_STOP_MAX_FAILURES,
    FAILURE_GRACE_CYCLES,
    UPDATE_CYCLE_TIMEOUT,
    VEHICLE_BATCH_TIMEOUT,
    VEHICLE_ABSENCE_GRACE_CYCLES,
    VolvoCoordinator,
    _card_resources,
    async_confirm_vehicle_state,
    async_refresh_after_control,
)
from custom_components.volvooncall_cn.store import VolvoStore, store_key_for
from custom_components.volvooncall_cn.volvooncall_base import (
    PASSWORD_LOGIN_COOLDOWN,
    REQUEST_BUDGET,
    VehicleBaseAPI,
    VolvoAuthExpiredError,
    VolvoRateLimitError,
    VolvoAuthThrottledError,
)
from custom_components.volvooncall_cn.volvooncall_cn import (
    GRPC_CHANNEL_OPTIONS,
    SOURCE_REFRESH_EVERY_CYCLES,
    Vehicle,
)

VIN = "TESTVIN0000000001"


class FakeVehicle:
    def __init__(self, vin=VIN):
        self.vin = vin
        self.is_unbound = False
        self._consecutive_failures = 0
        self.update = AsyncMock()
        self.battery_charge_level_percentage = 92
        self.electric_range = 300
        self.charge_data_source = "pile"
        self.battery_charging_status = "charging"
        self.charging_power = 7.0
        self.home_charge_status = "charging"
        self.stop_home_charge = AsyncMock()

    def apply_vehicle_info(self, info):
        pass


def _coordinator(hass, api=None, powertrain="b4_b5_b6"):
    api = api or AsyncMock()
    api.login = AsyncMock()
    api.update_token = AsyncMock()
    api.get_vehicles_vins = AsyncMock(return_value={VIN: {"modelYear": 2024}})
    return VolvoCoordinator(hass, api, 30, powertrain)


# ---------------------------------------------------------------------------
# Coordinator: forced refresh
# ---------------------------------------------------------------------------


async def test_concurrent_forced_refreshes_both_fetch(hass: HomeAssistant):
    """Two overlapping force refreshes must not cancel each other out.

    With a shared boolean flag the first request through the update body
    cleared the flag in its finally-clause, so the second caller silently
    degraded to a throttled no-op.
    """
    coordinator = _coordinator(hass)
    vehicle = FakeVehicle()
    coordinator._vehicles[VIN] = vehicle
    coordinator._vehicle_order.append(VIN)
    coordinator._stores[VIN] = MagicMock()

    gate = asyncio.Event()

    async def blocked_login():
        await gate.wait()

    with patch.object(VolvoCoordinator, "_vehicle_for", return_value=vehicle):
        await coordinator.async_refresh()  # seed
        coordinator.volvo_api.login = AsyncMock(side_effect=blocked_login)

        first = asyncio.create_task(coordinator.async_force_refresh())
        await asyncio.sleep(0.01)
        second = asyncio.create_task(coordinator.async_force_refresh())
        await asyncio.sleep(0.01)
        gate.set()
        await asyncio.gather(first, second)

    assert vehicle.update.await_count == 3  # seed + both forced polls
    assert coordinator._pending_force_refreshes == 0


async def test_control_confirmation_timeout_removes_its_queued_token(
    hass: HomeAssistant, monkeypatch
):
    """Cancellation while waiting to refresh must not leak a force token."""
    coordinator = _coordinator(hass)
    never_finishes = asyncio.Event()

    async def blocked_refresh():
        await never_finishes.wait()

    coordinator.async_refresh = AsyncMock(side_effect=blocked_refresh)
    monkeypatch.setattr(
        "custom_components.volvooncall_cn."
        "CONTROL_CONFIRMATION_TIMEOUT",
        0.01,
    )

    with pytest.raises(HomeAssistantError, match="could not be confirmed"):
        await async_confirm_vehicle_state(
            coordinator,
            lambda: False,
            retry_delay=0,
            description="Test control",
        )

    assert coordinator._pending_force_refreshes == 0
    assert coordinator._force_refresh_tokens == []


async def test_force_refresh_reports_false_when_coordinator_is_shutting_down(
    hass: HomeAssistant,
):
    """A no-op refresh must not confirm a cached pre-command value."""
    coordinator = _coordinator(hass)
    coordinator._shutdown_requested = True

    assert await coordinator.async_force_refresh() is False
    assert coordinator._pending_force_refreshes == 0


async def test_control_confirmation_retries_after_transport_failure():
    """One transient failed poll must not consume all confirmation attempts."""
    coordinator = MagicMock()
    coordinator.async_force_refresh = AsyncMock(
        side_effect=[False, True]
    )

    with patch("asyncio.sleep", new=AsyncMock()):
        await async_confirm_vehicle_state(
            coordinator,
            lambda: True,
            retry_delay=0,
            description="Test control",
        )

    assert coordinator.async_force_refresh.await_count == 2


async def test_control_confirmation_requires_current_source_generation():
    """An optimistic value needs source data from a post-command poll."""
    coordinator = MagicMock()
    generation = [7]
    calls = 0

    async def refresh():
        nonlocal calls
        calls += 1
        if calls == 2:
            generation[0] += 1
        return True

    coordinator.async_force_refresh = AsyncMock(side_effect=refresh)
    with patch("asyncio.sleep", new=AsyncMock()):
        await async_confirm_vehicle_state(
            coordinator,
            lambda: True,
            retry_delay=0,
            description="Test control",
            source_is_fresh=lambda: True,
            source_generation=lambda: tuple(generation),
        )

    assert coordinator.async_force_refresh.await_count == 2


async def test_control_confirmation_rejects_stale_matching_state():
    """A missing vehicle must not confirm from historical source health."""
    coordinator = MagicMock()
    coordinator.async_force_refresh = AsyncMock(return_value=True)

    with patch("asyncio.sleep", new=AsyncMock()):
        with pytest.raises(
            HomeAssistantError, match="could not be confirmed"
        ):
            await async_confirm_vehicle_state(
                coordinator,
                lambda: True,
                retry_delay=0,
                description="Test control",
                source_is_fresh=lambda: True,
                source_generation=lambda: (3,),
            )

    assert (
        coordinator.async_force_refresh.await_count
        == 3
    )


async def test_post_control_refresh_surfaces_transport_failure():
    coordinator = MagicMock()
    coordinator.async_force_refresh = AsyncMock(return_value=False)

    with pytest.raises(HomeAssistantError, match="could not be retrieved"):
        await async_refresh_after_control(
            coordinator,
            description="Test setting",
        )


async def test_throttled_refresh_does_not_resurrect_failed_entities(
    hass: HomeAssistant,
):
    """A throttled refresh must not report success once entities went away.

    Returning cached data from the throttle branch counts as a successful
    update, which flipped every entity back to available with stale values
    without any request having been made.
    """
    coordinator = _coordinator(hass)
    coordinator.data = [FakeVehicle()]
    coordinator.volvo_api.login = AsyncMock(side_effect=Exception("backend down"))

    for _ in range(FAILURE_GRACE_CYCLES + 1):
        coordinator._pending_force_refreshes += 1
        try:
            await coordinator._async_update_data()
        except UpdateFailed:
            pass
    assert coordinator._consecutive_failures > FAILURE_GRACE_CYCLES

    # Now a throttled (non-forced) refresh arrives inside the interval.
    coordinator._last_update_started_at = time.monotonic()
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_update_cycle_timeout_covers_the_transport_budget():
    """The cycle cap has to exceed what the retry ladder can legitimately use.

    A 60s cap against a ~33s worst case per REST call meant a degraded
    network killed every cycle in the auth + vehicle-listing phase alone.
    """
    # login/update_token, vehicle listing, three sequential home-charge REST
    # calls, and one bounded charge-stop mutation.
    assert UPDATE_CYCLE_TIMEOUT >= REQUEST_BUDGET * 6
    assert VEHICLE_BATCH_TIMEOUT >= REQUEST_BUDGET * 5


async def test_multi_vehicle_updates_use_bounded_concurrency(
    hass: HomeAssistant,
):
    """A slow first car must not prevent the second car from starting."""
    api = AsyncMock()
    api.login = AsyncMock()
    api.update_token = AsyncMock()
    vins = ("TESTVIN0000000001", "TESTVIN0000000002")
    api.get_vehicles_vins = AsyncMock(
        return_value={vin: {"modelYear": 2024} for vin in vins}
    )
    coordinator = VolvoCoordinator(hass, api, 30, "b4_b5_b6")
    both_started = asyncio.Event()
    release = asyncio.Event()
    started = 0

    async def blocked_update():
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await release.wait()

    vehicles = {}
    for vin in vins:
        vehicle = FakeVehicle(vin)
        vehicle.update = AsyncMock(side_effect=blocked_update)
        vehicles[vin] = vehicle
    coordinator._vehicles.update(vehicles)
    coordinator._vehicle_order.extend(vins)

    update_task = asyncio.create_task(coordinator._async_update_data())
    await asyncio.wait_for(both_started.wait(), timeout=1)
    release.set()
    result = await update_task

    assert [vehicle.vin for vehicle in result] == list(vins)


async def test_all_vehicle_updates_failing_marks_coordinator_failed(
    hass: HomeAssistant,
):
    """Swallowed source failures must not turn into a successful poll."""
    coordinator = _coordinator(hass)
    vehicle = FakeVehicle()
    vehicle.update = AsyncMock(side_effect=RuntimeError("all sources failed"))
    coordinator._vehicles[VIN] = vehicle
    coordinator._vehicle_order.append(VIN)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert vehicle._consecutive_failures == 1
    assert coordinator._consecutive_failures == 1


async def test_coordinator_respects_server_retry_after(hass: HomeAssistant):
    api = AsyncMock()
    api.login = AsyncMock(side_effect=VolvoRateLimitError("busy", 120))
    coordinator = VolvoCoordinator(hass, api, 30)
    cached = [FakeVehicle()]
    coordinator.data = cached

    assert await coordinator._async_update_data() is cached
    coordinator._pending_force_refreshes += 1
    assert await coordinator._async_update_data() is cached

    api.login.assert_awaited_once()
    assert coordinator._server_retry_after_monotonic > time.monotonic()


def test_new_vehicle_schedules_one_reload_and_tolerates_bad_model_year(
    hass: HomeAssistant,
):
    entry = MagicMock()
    entry.entry_id = "entry-id"
    coordinator = VolvoCoordinator(
        hass, AsyncMock(), 30, config_entry=entry
    )
    coordinator.data = [FakeVehicle("EXISTINGVIN")]

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as reload_entry:
        first = coordinator._vehicle_for(
            "NEWVIN1", {"modelYear": "not-a-year"}
        )
        coordinator._vehicle_for("NEWVIN2", {"modelYear": None})

    assert first.isAaos is False
    reload_entry.assert_called_once_with("entry-id")


# ---------------------------------------------------------------------------
# Coordinator: charge limit and vehicle presence
# ---------------------------------------------------------------------------


async def test_charge_limit_stop_backs_off_after_repeated_failures(
    hass: HomeAssistant,
):
    """A pile that keeps refusing must not be re-sent a stop every poll."""
    coordinator = _coordinator(hass, powertrain="t8")
    vehicle = FakeVehicle()
    vehicle.stop_home_charge = AsyncMock(side_effect=Exception("pile busy"))
    store = MagicMock()
    store.get_charge_limit.return_value = 80

    for _ in range(10):
        await coordinator._async_enforce_charge_limit(vehicle, store)

    assert vehicle.stop_home_charge.await_count == CHARGE_LIMIT_STOP_MAX_FAILURES


async def test_charge_limit_backoff_resets_after_a_success(hass: HomeAssistant):
    """A recovered pile must not stay stuck in the backoff window."""
    coordinator = _coordinator(hass, powertrain="t8")
    vehicle = FakeVehicle()
    vehicle.stop_home_charge = AsyncMock(side_effect=Exception("pile busy"))
    store = MagicMock()
    store.get_charge_limit.return_value = 80

    await coordinator._async_enforce_charge_limit(vehicle, store)
    assert VIN in coordinator._charge_limit_stop_failures

    vehicle.stop_home_charge = AsyncMock()
    await coordinator._async_enforce_charge_limit(vehicle, store)
    assert VIN not in coordinator._charge_limit_stop_failures


async def test_charge_limit_state_cleared_when_charging_stops(
    hass: HomeAssistant,
):
    coordinator = _coordinator(hass, powertrain="t8")
    vehicle = FakeVehicle()
    store = MagicMock()
    store.get_charge_limit.return_value = 80
    coordinator._charge_limit_stop_failures[VIN] = (2, time.monotonic() + 60)

    vehicle.home_charge_status = "completed"
    await coordinator._async_enforce_charge_limit(vehicle, store)

    assert VIN not in coordinator._charge_limit_stop_failures


async def test_unbound_vehicle_goes_unavailable_but_is_not_removed(
    hass: HomeAssistant,
):
    """A car that leaves the account stops being served as fresh data.

    The object itself stays in place: VolvoEntity falls back to a positional
    lookup, so dropping it would make the removed car's entities display a
    different car's values.
    """
    coordinator = _coordinator(hass)
    vehicle = FakeVehicle()
    coordinator._vehicles[VIN] = vehicle
    coordinator._vehicle_order.append(VIN)

    for _ in range(VEHICLE_ABSENCE_GRACE_CYCLES - 1):
        coordinator._note_vehicle_presence(set())
        assert vehicle.is_unbound is False

    coordinator._note_vehicle_presence(set())
    assert vehicle.is_unbound is True
    assert coordinator._vehicles[VIN] is vehicle

    coordinator._note_vehicle_presence({VIN})
    assert vehicle.is_unbound is False


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_password_login_makes_exactly_one_request():
    """The transport must not turn one login into MAX_RETRIES password posts.

    The local cooldown accounts for a single attempt, so replaying it put
    three real password posts on the auth endpoint per cooldown window.
    """
    api = VehicleBaseAPI(MagicMock(), "test-account", "pw")
    seen = []

    async def fake_request(method, url, headers, *, max_attempts=3, **kwargs):
        seen.append(max_attempts)
        return {"success": True, "data": {
            "refreshToken": "r",
            "globalAccessToken": "g",
            "accessToken": "a",
            "jwtToken": "j",
            "expiresIn": 3600,
        }}

    api._request_digitalvolvo = fake_request
    await api.login()

    assert seen == [1]


async def test_password_cooldown_survives_a_restart():
    """A rebuilt API object must inherit the cooldown, not reset it.

    async_setup_entry builds a fresh VehicleAPI on every ConfigEntryNotReady
    retry, so an in-memory-only timestamp let a restart loop hammer the auth
    endpoint.
    """
    api = VehicleBaseAPI(MagicMock(), "test-account", "pw")
    api._request_digitalvolvo = AsyncMock(side_effect=Exception("boom"))
    with pytest.raises(Exception):
        await api.login()

    exported = api.export_tokens()
    assert exported["last_password_login_at"] > 0

    restarted = VehicleBaseAPI(MagicMock(), "test-account", "pw")
    restarted.import_tokens(exported)
    with pytest.raises(VolvoAuthThrottledError):
        await restarted.login()


async def test_imported_cooldown_from_the_future_is_clamped():
    """Clock skew or a restored backup must not wedge logins shut forever."""
    api = VehicleBaseAPI(MagicMock(), "test-account", "pw")
    api.import_tokens(
        {"last_password_login_at": int(time.time()) + 10 * PASSWORD_LOGIN_COOLDOWN}
    )
    assert api._last_password_login_at <= int(time.time())


async def test_rejected_refresh_token_is_dropped_from_storage():
    """A dead refresh token must not survive a restart in the auth store."""
    api = VehicleBaseAPI(MagicMock(), "test-account", "pw")
    persisted = {}
    api.set_token_listener(persisted.update)

    api._refresh_token = "DEAD"
    api._access_token_expire_at = 0
    api._notify_token_listener()
    assert persisted["refresh_token"] == "DEAD"

    api.digitalvolvo_get = AsyncMock(
        side_effect=VolvoAuthExpiredError("401")
    )
    api._request_digitalvolvo = AsyncMock(
        side_effect=VolvoAuthExpiredError("401")
    )
    with pytest.raises(Exception):
        await api.login()

    assert api._refresh_token == ""
    assert persisted["refresh_token"] == ""


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------


async def test_store_key_does_not_contain_the_vin():
    """The store key becomes a filename under .storage."""
    key = store_key_for(VIN)
    assert VIN not in key
    assert key.startswith("volvooncall_cn.")


async def test_legacy_vin_named_store_is_migrated(hass: HomeAssistant):
    """Settings saved under the old plaintext key must survive the rename."""
    legacy_payload = {"engine_duration_number": 12, "charge_limit": 70}
    store = VolvoStore(hass, VIN)

    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()
    store._legacy_store.async_load = AsyncMock(return_value=legacy_payload)
    store._legacy_store.async_remove = AsyncMock()

    data = await store.load_create_data()

    assert data == legacy_payload
    store.async_save.assert_awaited_once_with(legacy_payload)
    store._legacy_store.async_remove.assert_awaited_once()


async def test_legacy_store_read_error_does_not_log_vin(
    hass: HomeAssistant, caplog
):
    """Exception strings can contain the legacy plaintext storage path."""
    store = VolvoStore(hass, VIN)
    store._legacy_store.async_load = AsyncMock(
        side_effect=OSError(f"/config/.storage/volvooncall_cn.{VIN}")
    )

    with caplog.at_level("WARNING"):
        assert await store._async_migrate_legacy() is None

    assert VIN not in caplog.text
    assert "OSError" in caplog.text


async def test_legacy_store_remove_error_does_not_log_vin(
    hass: HomeAssistant, caplog
):
    """A successful migration stays usable if deleting the old path fails."""
    payload = {"engine_duration_number": 9}
    store = VolvoStore(hass, VIN)
    store.async_save = AsyncMock()
    store._legacy_store.async_load = AsyncMock(return_value=payload)
    store._legacy_store.async_remove = AsyncMock(
        side_effect=OSError(f"/config/.storage/volvooncall_cn.{VIN}")
    )

    with caplog.at_level("WARNING"):
        assert await store._async_migrate_legacy() == payload

    assert VIN not in caplog.text
    assert "OSError" in caplog.text


async def test_invocation_responses_are_not_logged_verbatim():
    """invocationData carries vin and deviceid; debug logs must be redacted."""
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "custom_components"
        / "volvooncall_cn"
        / "volvooncall_cn.py"
    ).read_text(encoding="utf-8")

    assert "_LOGGER.debug(res)" not in source


# ---------------------------------------------------------------------------
# Vehicle data
# ---------------------------------------------------------------------------


async def test_missing_location_is_none_not_the_gulf_of_guinea():
    """A zeroed fix must read as unknown, not as 0N 0E."""
    api = AsyncMock()
    api.get_location = AsyncMock(
        return_value=MagicMock(latitude=0.0, longitude=0.0)
    )
    vehicle = Vehicle(VIN, api, isAaos=True, supports_electric=False)

    await vehicle._parse_location()

    assert vehicle.position == {"latitude": None, "longitude": None}
    assert vehicle.position_wgs84 == {"latitude": None, "longitude": None}


async def test_real_location_is_still_converted():
    api = AsyncMock()
    api.get_location = AsyncMock(
        return_value=MagicMock(latitude=31.2304, longitude=121.4737)
    )
    vehicle = Vehicle(VIN, api, isAaos=True, supports_electric=False)

    await vehicle._parse_location()

    assert vehicle.position == {"latitude": 31.2304, "longitude": 121.4737}
    # Shanghai: the GCJ02 offset is a few hundred metres, never zero.
    assert vehicle.position_wgs84["latitude"] != 31.2304
    assert abs(vehicle.position_wgs84["latitude"] - 31.2304) < 0.01


@pytest.mark.parametrize("source", ["health", "odometer", "preference"])
async def test_slow_moving_sources_are_not_polled_every_cycle(source):
    """Nickname and health data do not need a request every 30 seconds."""
    vehicle = Vehicle(VIN, AsyncMock(), isAaos=True, supports_electric=False)

    # First cycle always runs so entities populate at setup.
    assert vehicle._should_refresh(source) is True

    vehicle._cache[source] = {}
    vehicle._poll_cycle = 1
    assert vehicle._should_refresh(source) is False
    vehicle._poll_cycle = SOURCE_REFRESH_EVERY_CYCLES[source]
    assert vehicle._should_refresh(source) is True

    # Sources with no entry stay on every cycle.
    assert vehicle._should_refresh("exterior") is True


@pytest.mark.parametrize("source", ["health", "odometer", "preference"])
async def test_failed_slow_source_retries_on_the_next_cycle(source):
    """Cadence throttling must not defer recovery from a source failure."""
    vehicle = Vehicle(VIN, AsyncMock(), isAaos=True, supports_electric=False)
    vehicle._cache[source] = {"cached": True}
    vehicle._poll_cycle = 1
    vehicle._data_source_status[source] = False

    assert vehicle._should_refresh(source) is True

    vehicle._data_source_status[source] = True
    assert vehicle._should_refresh(source) is False


async def test_vehicle_info_is_reused_instead_of_refetched():
    """Missing names must not trigger a duplicate listBindCar request."""
    api = AsyncMock()
    api.get_channel = AsyncMock()
    api.get_vehicles = AsyncMock(return_value=[{"vinCode": VIN}])
    vehicle = Vehicle(VIN, api, isAaos=True, supports_electric=False)
    vehicle._should_refresh = MagicMock(return_value=False)

    vehicle.apply_vehicle_info({"vinCode": VIN})
    await vehicle.update()

    assert vehicle.series_name == ""
    assert vehicle.model_name == ""
    api.get_vehicles.assert_not_awaited()
    api.get_channel.assert_awaited_once()


async def test_invocation_failures_raise_home_assistant_error():
    """Vehicle-side refusals are expected; they must not dump a stack trace."""
    from custom_components.volvooncall_cn.proto.invocation_pb2 import (
        invocationStatus,
    )
    from custom_components.volvooncall_cn.volvooncall_cn import VehicleAPI

    api = VehicleAPI(MagicMock(), "test-account", "pw")
    with pytest.raises(HomeAssistantError):
        api.raise_invocation_fail(invocationStatus.CAR_OFFLINE)

    api.raise_invocation_fail(invocationStatus.SUCCESS)  # no raise


# ---------------------------------------------------------------------------
# Packaging / frontend
# ---------------------------------------------------------------------------


async def test_grpc_channel_has_keepalive():
    """An idle channel dropped by a NAT middlebox costs a whole poll cycle."""
    options = dict(GRPC_CHANNEL_OPTIONS)
    assert options["grpc.keepalive_time_ms"] > 0
    assert options["grpc.keepalive_permit_without_calls"] == 1


async def test_card_urls_are_derived_from_file_content():
    """A hand-written version query drifts and serves a cached stale card."""
    resources = _card_resources()
    assert len(resources) == 2

    urls = {url for _, url in resources}
    assert len(urls) == 2  # the two cards must not share a cache-buster
    for path, url in resources:
        assert url.startswith(f"{path}?v=")
        assert len(url.split("?v=")[1]) >= 8


async def test_component_setup_registers_versioned_card_resources(
    hass: HomeAssistant,
):
    """End-to-end check of async_setup after the card-URL rewrite.

    _card_resources() does blocking file reads, so it has to run in an
    executor; calling it on the event loop would trip Home Assistant's
    blocking-IO detection at startup.
    """
    from custom_components.volvooncall_cn import async_setup
    from custom_components.volvooncall_cn.volvooncall_cn import DOMAIN

    registered = []

    async def fake_register(paths):
        registered.extend(paths)

    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock(side_effect=fake_register)

    assert await async_setup(hass, {})
    await hass.async_block_till_done()

    assert len(registered) == 1
    assert registered[0].url_path == f"/{DOMAIN}/frontend"


async def test_component_setup_survives_http_not_being_ready(
    hass: HomeAssistant,
):
    """hass.http exists but is None before the http component is set up."""
    from custom_components.volvooncall_cn import async_setup

    hass.http = None
    assert await async_setup(hass, {}) is True


async def test_translations_cover_every_entity_key():
    """A missing translation_key leaves the entity with a broken name."""
    import json
    import pathlib
    import re

    root = (
        pathlib.Path(__file__).resolve().parents[1]
        / "custom_components"
        / "volvooncall_cn"
    )
    body = (root / "__init__.py").read_text(encoding="utf-8").split("metaMap = {", 1)[1]
    meta_keys = set(re.findall(r'^    "([a-z0-9_]+)":', body, re.M))
    assert meta_keys, "metaMap keys could not be parsed"

    for name in ("translations/en.json", "translations/zh-Hans.json", "strings.json"):
        data = json.loads((root / name).read_text(encoding="utf-8"))
        translated = set()
        for platform in data.get("entity", {}).values():
            translated |= set(platform)
        assert not meta_keys - translated, f"{name} is missing keys"


async def test_manifest_does_not_require_grpcio_tools():
    """grpcio-tools is a proto-generation tool, never imported at runtime."""
    import json
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (root / "custom_components" / "volvooncall_cn" / "manifest.json")
        .read_text(encoding="utf-8")
    )

    assert not any(
        req.startswith("grpcio-tools") for req in manifest["requirements"]
    )


async def test_manifest_declares_generated_protobuf_runtime():
    """Generated modules need protobuf itself, not only grpcio."""
    import json
    import pathlib
    import re

    from packaging.requirements import Requirement
    from packaging.version import Version

    component = (
        pathlib.Path(__file__).resolve().parents[1]
        / "custom_components"
        / "volvooncall_cn"
    )
    requirements = json.loads(
        (component / "manifest.json").read_text(encoding="utf-8")
    )["requirements"]

    generated_versions = set()
    for path in (component / "proto").glob("*_pb2.py"):
        match = re.search(
            r"^# Protobuf Python Version: ([0-9.]+)$",
            path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        assert match, f"{path.name} has no protobuf generator version"
        generated_versions.add(match.group(1))

    generated_minimum = max(Version(value) for value in generated_versions)
    protobuf_requirement = next(
        Requirement(requirement)
        for requirement in requirements
        if Requirement(requirement).name == "protobuf"
    )
    declared_lower_bounds = [
        Version(specifier.version)
        for specifier in protobuf_requirement.specifier
        if specifier.operator in {">", ">=", "==", "==="}
    ]

    assert declared_lower_bounds
    assert max(declared_lower_bounds) >= generated_minimum
