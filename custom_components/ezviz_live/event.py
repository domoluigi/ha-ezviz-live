"""Alarm events (motion, doorbell, ...) pushed by the EZVIZ cloud."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EzvizLiveConfigEntry
from .const import EVENT_TYPE_DOORBELL, EVENT_TYPE_MOTION, EVENT_TYPE_OTHER, SIGNAL_NEW_EVENT
from .coordinator import AlarmEvent
from .entity import EzvizLiveEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizLiveConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one alarm event entity per device."""
    async_add_entities(EzvizAlarmEvent(device) for device in entry.runtime_data.devices.values())


class EzvizAlarmEvent(EzvizLiveEntity, EventEntity):
    """Fires once per new alarm message."""

    _attr_device_class = EventDeviceClass.MOTION
    _attr_event_types = [EVENT_TYPE_MOTION, EVENT_TYPE_DOORBELL, EVENT_TYPE_OTHER]

    def __init__(self, device) -> None:
        EzvizLiveEntity.__init__(self, device, "alarm")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_NEW_EVENT.format(serial=self._device.serial),
                self._handle_event,
            )
        )

    @callback
    def _handle_event(self, event: AlarmEvent) -> None:
        delay = (event.received_at - event.when).total_seconds()
        self._trigger_event(
            event.event_type,
            {
                "event_time": event.when.isoformat(),
                "alarm_type": event.alarm_type,
                "title": event.title,
                "message_id": event.msg_id,
                "has_picture": event.picture is not None,
                "source": event.source,
                "delay_seconds": round(delay, 1),
            },
        )
        self.async_write_ha_state()
