"""Base entity for EZVIZ Live."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .api import EzvizDevice
from .const import DOMAIN


class EzvizLiveEntity(Entity):
    """Common device info and naming."""

    _attr_has_entity_name = True

    def __init__(self, device: EzvizDevice, key: str) -> None:
        self._device = device
        self._attr_unique_id = f"{device.serial}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.serial)},
            manufacturer="EZVIZ",
            model=device.model,
            name=f"{device.name} Live",
            serial_number=device.serial,
            sw_version=device.firmware,
        )
