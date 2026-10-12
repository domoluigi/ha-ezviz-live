"""Thread-safe wrapper around the synchronous pyezvizapi client."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
import threading
from typing import Any

from pyezvizapi.client import EzvizClient
from pyezvizapi.cloud_stream import open_cloud_stream
from pyezvizapi.stream import VtmStreamClient
from pyezvizapi.utils import decrypt_image

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class EzvizDevice:
    """Static description of one EZVIZ device."""

    serial: str
    name: str
    model: str
    category: str
    firmware: str | None
    has_local_rtsp: bool


@dataclass
class EzvizApi:
    """Serialize every call that touches the shared requests session.

    pyezvizapi is synchronous and refreshes its session token in place, so two
    executor threads using the client at once could race on the refresh. All
    calls go through one lock; long-running work (the VTM socket loop) only
    holds it while bootstrapping.
    """

    client: EzvizClient
    on_token_change: Callable[[dict[str, Any]], None] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _keys: dict[str, str] = field(default_factory=dict)
    _last_token: dict[str, Any] = field(default_factory=dict)

    def _call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            result = func(*args, **kwargs)
            self._check_token()
            return result

    def _check_token(self) -> None:
        token = dict(self.client.export_token())
        if token != self._last_token:
            first = not self._last_token
            self._last_token = token
            if not first and self.on_token_change is not None:
                self.on_token_change(token)

    def login(self) -> dict[str, Any]:
        """Refresh the session (blocking)."""
        with self._lock:
            self.client.login()
            token = dict(self.client.export_token())
            self._last_token = token
            return token

    def export_token(self) -> dict[str, Any]:
        """Return the current session token."""
        with self._lock:
            return dict(self.client.export_token())

    def load_devices(self) -> dict[str, EzvizDevice]:
        """Return every device that can stream video (blocking)."""
        infos = self._call(self.client.get_device_infos)
        devices: dict[str, EzvizDevice] = {}
        for serial, data in infos.items():
            if not data.get("resourceInfos"):
                continue  # no video resource (plugs, bulbs, ...)
            info = data.get("deviceInfos") or {}
            conn = data.get("CONNECTION") or {}
            devices[serial] = EzvizDevice(
                serial=serial,
                name=str(info.get("name") or serial),
                model=str(info.get("deviceSubCategory") or info.get("deviceType") or "EZVIZ"),
                category=str(info.get("deviceCategory") or ""),
                firmware=info.get("version"),
                has_local_rtsp=bool(conn.get("localRtspPort")),
            )
        return devices

    def get_key(self, serial: str) -> str | None:
        """Return the media encryption key of a device, cached (blocking)."""
        if serial not in self._keys:
            key = self._call(self.client.get_cam_key, serial)
            if isinstance(key, dict):
                key = key.get("encryptkey")
            if not key:
                return None
            self._keys[serial] = str(key)
        return self._keys[serial]

    def get_messages(self, serials: list[str], subtype: str, limit: int) -> list[dict[str, Any]]:
        """Return the most recent unified alarm messages (blocking)."""
        resp = self._call(
            self.client.get_device_messages_list,
            serials=",".join(serials),
            s_type=subtype,
            limit=limit,
        )
        messages = resp.get("message") or resp.get("messages") or []
        return [m for m in messages if isinstance(m, dict)]

    def fetch_picture(self, url: str, serial: str, encrypted: bool) -> bytes | None:
        """Download an alarm picture and decrypt it when needed (blocking)."""
        with self._lock:
            resp = self.client._session.get(url, timeout=20)  # noqa: SLF001
        if resp.status_code != 200 or not resp.content:
            _LOGGER.debug("Picture download for %s failed: HTTP %s", serial, resp.status_code)
            return None
        data = resp.content
        if encrypted or data[:2] != b"\xff\xd8":
            key = self.get_key(serial)
            if key is None:
                return None
            data = decrypt_image(data, key)
        return data if data[:2] == b"\xff\xd8" else None

    def open_stream(self, serial: str) -> VtmStreamClient:
        """Bootstrap a VTM cloud stream (blocking); the caller starts it."""
        return self._call(open_cloud_stream, self.client, serial)

    def get_mqtt_client(self, callback: Callable[[dict[str, Any]], None]) -> Any:
        """Return the pyezvizapi MQTT push client bound to this session."""
        with self._lock:
            self.client.mqtt_client = None
            return self.client.get_mqtt_client(on_message_callback=callback)

    def close(self) -> None:
        """Close the HTTP session."""
        with self._lock:
            self.client.close_session()
