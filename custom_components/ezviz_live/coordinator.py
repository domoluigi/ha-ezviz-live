"""Alarm events: unified message list, refreshed by MQTT push and a slow poll."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any

from pyezvizapi.exceptions import EzvizAuthTokenExpired, EzvizAuthVerificationCode, PyEzvizError

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    ALARM_TYPE_PIR,
    DOMAIN,
    EVENT_TYPE_MOTION,
    EVENT_TYPE_OTHER,
    MESSAGE_FETCH_LIMIT,
    MESSAGE_POLL_INTERVAL,
    MESSAGE_SUBTYPE_ALL_ALARMS,
    SIGNAL_NEW_EVENT,
)

if TYPE_CHECKING:
    from .api import EzvizApi, EzvizDevice

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class AlarmEvent:
    """One alarm message of a device."""

    msg_id: str
    serial: str
    when: datetime
    alarm_type: int | None
    title: str
    event_type: str
    picture: bytes | None
    received_at: datetime
    source: str


def _event_type(alarm_type: int | None) -> str:
    if alarm_type == ALARM_TYPE_PIR:
        return EVENT_TYPE_MOTION
    return EVENT_TYPE_OTHER


def _parse_time(message: dict[str, Any]) -> datetime:
    """Return the alarm time.

    ``timeStr`` is the device's local wall-clock time. The numeric ``time``
    field encodes that same wall-clock time as if it were UTC (a CP4 event at
    14:09:44 local time, UTC+2, arrives as 14:09:44Z), so it is not used.
    """
    text = message.get("timeStr")
    if isinstance(text, str):
        parsed = dt_util.parse_datetime(text)
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.utcnow()


class EzvizEventCoordinator(DataUpdateCoordinator[dict[str, AlarmEvent]]):
    """Keep the latest alarm event per device and announce new ones."""

    def __init__(self, hass: HomeAssistant, api: EzvizApi, devices: dict[str, EzvizDevice]) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=MESSAGE_POLL_INTERVAL,
            always_update=False,
        )
        self.api = api
        self.devices = devices
        self._seen: set[str] = set()
        self._primed = False
        self._pending_source = "poll"
        self.push_messages = 0
        self.last_push: datetime | None = None

    @callback
    def handle_push(self, message: dict[str, Any]) -> None:
        """Called on the event loop for each MQTT push message."""
        ext = message.get("ext") if isinstance(message.get("ext"), dict) else {}
        serial = ext.get("device_serial")
        if serial not in self.devices:
            return
        self.push_messages += 1
        self.last_push = dt_util.utcnow()
        self._pending_source = "push"
        self.hass.async_create_task(self.async_request_refresh())

    async def _async_update_data(self) -> dict[str, AlarmEvent]:
        source, self._pending_source = self._pending_source, "poll"
        try:
            messages = await self.hass.async_add_executor_job(
                self.api.get_messages,
                list(self.devices),
                MESSAGE_SUBTYPE_ALL_ALARMS,
                MESSAGE_FETCH_LIMIT,
            )
        except (EzvizAuthTokenExpired, EzvizAuthVerificationCode) as err:
            raise ConfigEntryAuthFailed from err
        except PyEzvizError as err:
            raise UpdateFailed(f"Could not read EZVIZ messages: {err}") from err

        latest: dict[str, AlarmEvent] = dict(self.data or {})
        new_events: list[AlarmEvent] = []
        # API order is newest first; walk oldest first so events fire in order.
        for message in reversed(messages):
            msg_id = str(message.get("msgId") or "")
            serial = message.get("deviceSerial")
            if not msg_id or serial not in self.devices or msg_id in self._seen:
                continue
            self._seen.add(msg_id)
            ext = message.get("ext") if isinstance(message.get("ext"), dict) else {}
            alarm_type = ext.get("alarmType")
            try:
                alarm_type = int(alarm_type) if alarm_type is not None else None
            except (TypeError, ValueError):
                alarm_type = None
            event = AlarmEvent(
                msg_id=msg_id,
                serial=serial,
                when=_parse_time(message),
                alarm_type=alarm_type,
                title=str(message.get("title") or ext.get("alarmName") or ""),
                event_type=_event_type(alarm_type),
                picture=None,
                received_at=dt_util.utcnow(),
                source=source if self._primed else "startup",
            )
            previous = latest.get(serial)
            if previous is None or event.when >= previous.when:
                url = message.get("pic") or message.get("defaultPic")
                if url:
                    try:
                        event.picture = await self.hass.async_add_executor_job(
                            self.api.fetch_picture, url, serial, bool(message.get("picCrypt"))
                        )
                    except PyEzvizError as err:
                        _LOGGER.debug("Picture of %s not available: %s", msg_id, err)
                latest[serial] = event
            if self._primed:
                new_events.append(event)

        if len(self._seen) > 500:
            # Messages older than the current API window never come back.
            self._seen = {str(m.get("msgId")) for m in messages if m.get("msgId")}

        self._primed = True
        for event in new_events:
            async_dispatcher_send(self.hass, SIGNAL_NEW_EVENT.format(serial=event.serial), event)
        return latest
