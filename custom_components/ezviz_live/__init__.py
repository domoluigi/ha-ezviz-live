"""EZVIZ Live: on-demand cloud live view and push events for EZVIZ cameras.

Complements the core ``ezviz`` integration for devices without local RTSP
(battery cameras, door viewers, doorbells). It never touches the core one.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import (
    EzvizAuthTokenExpired,
    EzvizAuthVerificationCode,
    PyEzvizError,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.event import async_track_time_interval

from .api import EzvizApi, EzvizDevice
from .const import (
    API_TIMEOUT_SECONDS,
    CONF_MAX_LIVE_SECONDS,
    CONF_REGION,
    CONF_TOKEN,
    DEFAULT_MAX_LIVE_SECONDS,
    DEFAULT_REGION,
    MQTT_WATCHDOG_INTERVAL,
)
from .coordinator import EzvizEventCoordinator
from .live import LiveManager, get_live_manager

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.CAMERA, Platform.EVENT, Platform.IMAGE]


@dataclass
class EzvizLiveData:
    """Runtime data of one config entry."""

    api: EzvizApi
    devices: dict[str, EzvizDevice]
    coordinator: EzvizEventCoordinator
    live: LiveManager
    mqtt: Any = None


type EzvizLiveConfigEntry = ConfigEntry[EzvizLiveData]


async def async_setup_entry(hass: HomeAssistant, entry: EzvizLiveConfigEntry) -> bool:
    """Set up EZVIZ Live from a config entry."""
    token: dict[str, Any] = dict(entry.data[CONF_TOKEN])
    region = entry.data.get(CONF_REGION, DEFAULT_REGION)

    @callback
    def _save_token(new_token: dict[str, Any]) -> None:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TOKEN: new_token}
        )

    def _on_token_change(new_token: dict[str, Any]) -> None:
        hass.loop.call_soon_threadsafe(_save_token, new_token)

    client = EzvizClient(
        token=token, url=token.get("api_url") or region, timeout=API_TIMEOUT_SECONDS
    )
    api = EzvizApi(client, on_token_change=_on_token_change)

    try:
        fresh = await hass.async_add_executor_job(api.login)
        devices = await hass.async_add_executor_job(api.load_devices)
    except (EzvizAuthTokenExpired, EzvizAuthVerificationCode) as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except PyEzvizError as err:
        raise ConfigEntryNotReady(str(err)) from err
    if fresh != token:
        _save_token(fresh)

    coordinator = EzvizEventCoordinator(hass, api, devices)
    await coordinator.async_config_entry_first_refresh()

    live = get_live_manager(hass)
    max_seconds = int(entry.options.get(CONF_MAX_LIVE_SECONDS, DEFAULT_MAX_LIVE_SECONDS))
    for serial in devices:
        live.register(serial, api, max_seconds)

    entry.runtime_data = EzvizLiveData(api=api, devices=devices, coordinator=coordinator, live=live)

    await _async_start_mqtt(hass, entry)

    async def _watchdog(_now: Any) -> None:
        mqtt = entry.runtime_data.mqtt
        client_obj = getattr(mqtt, "mqtt_client", None)
        if client_obj is None or not client_obj.is_connected():
            _LOGGER.info("EZVIZ push disconnected, reconnecting")
            await _async_stop_mqtt(hass, entry)
            await _async_start_mqtt(hass, entry)

    entry.async_on_unload(async_track_time_interval(hass, _watchdog, MQTT_WATCHDOG_INTERVAL))
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_start_mqtt(hass: HomeAssistant, entry: EzvizLiveConfigEntry) -> None:
    data = entry.runtime_data

    def _on_message(message: dict[str, Any]) -> None:
        # Called from the paho network thread.
        hass.loop.call_soon_threadsafe(data.coordinator.handle_push, message)

    def _connect() -> Any:
        mqtt = data.api.get_mqtt_client(_on_message)
        mqtt.connect()  # same call verified by tools/probe.py push (5 s latency)
        return mqtt

    try:
        data.mqtt = await hass.async_add_executor_job(_connect)
    except (PyEzvizError, OSError) as err:
        data.mqtt = None
        _LOGGER.warning(
            "EZVIZ push not available (%s); falling back to polling every %s",
            err,
            data.coordinator.update_interval,
        )


async def _async_stop_mqtt(hass: HomeAssistant, entry: EzvizLiveConfigEntry) -> None:
    mqtt = entry.runtime_data.mqtt
    entry.runtime_data.mqtt = None
    if mqtt is not None:
        try:
            await hass.async_add_executor_job(mqtt.stop)
        except (PyEzvizError, OSError) as err:
            _LOGGER.debug("Error stopping EZVIZ push: %s", err)


async def _async_options_updated(hass: HomeAssistant, entry: EzvizLiveConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EzvizLiveConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await _async_stop_mqtt(hass, entry)
        for serial in entry.runtime_data.devices:
            entry.runtime_data.live.unregister(serial)
        await hass.async_add_executor_job(entry.runtime_data.api.close)
    return unloaded
