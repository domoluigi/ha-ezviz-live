"""Decrypted picture of the latest alarm."""

from __future__ import annotations

from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EzvizLiveConfigEntry
from .api import EzvizDevice
from .coordinator import EzvizEventCoordinator
from .entity import EzvizLiveEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizLiveConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one picture entity per device."""
    data = entry.runtime_data
    async_add_entities(
        EzvizLastEventImage(hass, data.coordinator, device) for device in data.devices.values()
    )


class EzvizLastEventImage(CoordinatorEntity[EzvizEventCoordinator], EzvizLiveEntity, ImageEntity):
    """Picture of the latest alarm, decrypted with the device key."""

    _attr_content_type = "image/jpeg"

    def __init__(
        self, hass: HomeAssistant, coordinator: EzvizEventCoordinator, device: EzvizDevice
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        EzvizLiveEntity.__init__(self, device, "last_event_picture")
        ImageEntity.__init__(self, hass)
        self._msg_id: str | None = None
        self._update_from_event()

    def _event(self) -> Any:
        return (self.coordinator.data or {}).get(self._device.serial)

    def _update_from_event(self) -> None:
        event = self._event()
        if event is not None and event.msg_id != self._msg_id:
            self._msg_id = event.msg_id
            self._attr_image_last_updated = event.when

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_from_event()
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        event = self._event()
        return super().available and event is not None and event.picture is not None

    async def async_image(self) -> bytes | None:
        event = self._event()
        return event.picture if event is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        event = self._event()
        if event is None:
            return {}
        return {"alarm_type": event.alarm_type, "title": event.title}
