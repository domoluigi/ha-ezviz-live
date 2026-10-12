"""Cameras with on-demand cloud live view."""

from __future__ import annotations

from aiohttp import web

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EzvizLiveConfigEntry
from .api import EzvizDevice
from .const import SIGNAL_LIVE_STATE
from .coordinator import EzvizEventCoordinator
from .entity import EzvizLiveEntity
from .live import MODE_MJPEG, LiveManager, async_serve_live

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizLiveConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the live cameras of every device."""
    data = entry.runtime_data
    entities: list[Camera] = []
    for device in data.devices.values():
        entities.append(EzvizLiveCamera(data.coordinator, data.live, device))
        entities.append(EzvizLiveCompatCamera(data.coordinator, data.live, device))
    async_add_entities(entities)


class _EzvizCameraBase(CoordinatorEntity[EzvizEventCoordinator], EzvizLiveEntity, Camera):
    """Shared behaviour: the still image never wakes the device."""

    def __init__(
        self,
        coordinator: EzvizEventCoordinator,
        live: LiveManager,
        device: EzvizDevice,
        key: str,
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        EzvizLiveEntity.__init__(self, device, key)
        Camera.__init__(self)
        self._live = live

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_LIVE_STATE.format(serial=self._device.serial),
                self._handle_live_state,
            )
        )

    @callback
    def _handle_live_state(self) -> None:
        self.async_write_ha_state()

    @property
    def is_streaming(self) -> bool:
        return self._live.is_live(self._device.serial)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the last alarm picture (does not wake a battery device)."""
        event = (self.coordinator.data or {}).get(self._device.serial)
        return event.picture if event is not None else None


class EzvizLiveCamera(_EzvizCameraBase):
    """HEVC + audio through the HA stream component (HLS/WebRTC)."""

    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_name = None  # the device name

    def __init__(
        self, coordinator: EzvizEventCoordinator, live: LiveManager, device: EzvizDevice
    ) -> None:
        super().__init__(coordinator, live, device, "live")

    async def stream_source(self) -> str | None:
        return self._live.stream_url(self._device.serial)


class EzvizLiveCompatCamera(_EzvizCameraBase):
    """Same live view as MJPEG, for browsers that cannot decode HEVC.

    No STREAM feature, so the frontend plays it through camera_proxy_stream
    (MJPEG) instead of HLS/WebRTC. 5 fps, 960 px wide, no audio; the host
    decodes HEVC only while someone watches. Meant for wall-mounted kiosk
    browsers: an Android WebView kept the HEVC player blank until it was
    tapped, while the MJPEG view starts on its own.
    """

    _attr_supported_features = CameraEntityFeature(0)

    def __init__(
        self, coordinator: EzvizEventCoordinator, live: LiveManager, device: EzvizDevice
    ) -> None:
        super().__init__(coordinator, live, device, "live_compat")

    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse | None:
        return await async_serve_live(self._live, request, self._device.serial, MODE_MJPEG)
