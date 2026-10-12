"""Whether a live session currently keeps the device awake."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EzvizLiveConfigEntry
from .api import EzvizDevice
from .const import SIGNAL_LIVE_STATE
from .entity import EzvizLiveEntity
from .live import LiveManager

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizLiveConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one live-state sensor per device."""
    data = entry.runtime_data
    async_add_entities(EzvizLiveActive(data.live, device) for device in data.devices.values())


class EzvizLiveActive(EzvizLiveEntity, BinarySensorEntity):
    """On while the device streams (and drains its battery)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, live: LiveManager, device: EzvizDevice) -> None:
        EzvizLiveEntity.__init__(self, device, "live_active")
        self._live = live

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_LIVE_STATE.format(serial=self._device.serial),
                self.async_write_ha_state,
            )
        )

    @property
    def is_on(self) -> bool:
        return self._live.is_live(self._device.serial)
