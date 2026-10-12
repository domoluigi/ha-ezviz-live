"""On-demand live view: VTM cloud stream -> decryption -> HTTP consumers.

One *hub* per device holds the single VTM session (a battery device serves one
stream at a time: two sessions make both stall, seen on a CP4). Every HTTP
consumer - go2rtc (WebRTC), the HA stream worker (HLS), a browser asking for
MJPEG - subscribes to the hub and runs its own ffmpeg on the shared decrypted
MPEG-PS bytes. The hub starts with the first subscriber and stops a few seconds
after the last one leaves, or when ``max_live_seconds`` is reached (then new
sessions are refused for ``LIVE_COOLDOWN_SECONDS`` so automatic reconnects do
not restart the battery drain).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from ipaddress import ip_address
import logging
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any

from aiohttp import web
from pyezvizapi.stream import (
    decrypt_hikvision_ps_video,
    detect_hikvision_ps_video_nalu_header_size,
    mpeg_ps_decryptable_prefix_length,
)

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    DOMAIN,
    LIVE_COOLDOWN_SECONDS,
    MAX_SUBSCRIBERS_PER_DEVICE,
    SIGNAL_LIVE_STATE,
    STREAM_VIEW_URL,
)

if TYPE_CHECKING:
    from .api import EzvizApi

_LOGGER = logging.getLogger(__name__)

_READ_CHUNK = 64 * 1024
_STALL_TIMEOUT = 4.0  # seconds without data, once streaming, that end a segment
_MIN_SEGMENT_SECONDS = 15  # do not wake the device again for less than this
_MAX_SHORT_SEGMENTS = 3  # consecutive segments shorter than the above end the session
_OPEN_ATTEMPTS = 2  # cloud bootstrap attempts (euauth sometimes stalls)
_FIRST_DATA_TIMEOUT = 30.0  # seconds a consumer waits for the device to wake
_IDLE_GRACE_SECONDS = 3.0  # keep the hub alive briefly when the last consumer leaves
_SUBSCRIBER_BACKLOG = 2000  # queued chunks before a stuck consumer is dropped

_FFMPEG_INPUT = ["-hide_banner", "-loglevel", "error", "-fflags", "+genpts", "-f", "mpeg", "-i", "pipe:0"]

MODE_TS = "ts"
MODE_MJPEG = "mjpeg"

# MPEG-TS: codec copy, no transcoding (HEVC + AAC as sent by the device).
# MJPEG: for players that cannot start HEVC on their own (an Android WebView
# kiosk needed a tap); decodes HEVC, small low-rate JPEGs, no audio.
_FFMPEG_OUTPUT = {
    MODE_TS: ["-c", "copy", "-f", "mpegts", "pipe:1"],
    MODE_MJPEG: [
        "-an",
        "-vf",
        "fps=5,scale=960:-2",
        "-c:v",
        "mjpeg",
        "-q:v",
        "7",
        "-f",
        "mpjpeg",
        "pipe:1",
    ],
}
MJPEG_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=ffmpeg"


class IncrementalDecryptor:
    """Decrypt an EZVIZ MPEG-PS byte stream as it arrives.

    A video NAL can span adjacent PES packets, so bytes are only decrypted up to
    the boundary reported by ``mpeg_ps_decryptable_prefix_length``; the rest
    stays buffered until the next chunk arrives. Verified on a CP4 capture to
    produce output byte-identical to decrypting the whole capture at once.
    """

    def __init__(self, key: str | None) -> None:
        self._key = key
        self._buffer = bytearray()
        self._nalu_header_size: int | None = None

    def feed(self, data: bytes) -> bytes:
        """Add stream bytes and return whatever can be decrypted now."""
        if self._key is None:
            return data
        self._buffer += data
        ready = mpeg_ps_decryptable_prefix_length(self._buffer)
        if not ready:
            return b""
        chunk = bytes(self._buffer[:ready])
        del self._buffer[:ready]
        return self._decrypt(chunk)

    def flush(self) -> bytes:
        """Decrypt and return whatever is still buffered."""
        if self._key is None or not self._buffer:
            return b""
        chunk = bytes(self._buffer)
        self._buffer.clear()
        return self._decrypt(chunk)

    def _decrypt(self, chunk: bytes) -> bytes:
        assert self._key is not None
        if self._nalu_header_size is None:
            self._nalu_header_size = (
                detect_hikvision_ps_video_nalu_header_size(chunk, self._key) or 2
            )
        return decrypt_hikvision_ps_video(
            chunk, self._key, nalu_header_size=self._nalu_header_size
        )


class DeviceHub:
    """The single VTM session of one device, fanned out to subscribers."""

    def __init__(self, manager: LiveManager, api: EzvizApi, serial: str, max_seconds: int) -> None:
        self._manager = manager
        self._hass = manager.hass
        self._api = api
        self.serial = serial
        self._max_seconds = max_seconds
        self._subscribers: set[asyncio.Queue[bytes | None]] = set()
        self._stop = threading.Event()
        self._stream: Any = None
        self._thread: threading.Thread | None = None
        self._idle_handle: asyncio.TimerHandle | None = None
        self.first_data = asyncio.Event()
        self.finished = asyncio.Event()
        self.started_at = time.monotonic()
        self.first_data_at: float | None = None
        self.bytes_in = 0
        self.end_reason: str | None = None

    # -- lifecycle ---------------------------------------------------------

    @callback
    def start(self) -> None:
        """Spawn the worker thread (bootstrap happens there)."""
        loop = asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._pump, args=(loop,), name=f"ezviz_live_{self.serial}", daemon=True
        )
        self._thread.start()

    @callback
    def stop(self) -> None:
        """Ask the worker to stop; closing the socket unblocks a pending read."""
        self._stop.set()
        if self._stream is not None:
            with suppress(Exception):
                self._stream.close()

    @callback
    def subscribe(self) -> asyncio.Queue[bytes | None]:
        """Add a consumer; it receives every decrypted chunk from now on."""
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        if self.finished.is_set():
            queue.put_nowait(None)
        self._subscribers.add(queue)
        return queue

    @callback
    def unsubscribe(self, queue: asyncio.Queue[bytes | None]) -> None:
        """Remove a consumer; stop the device shortly after the last one."""
        self._subscribers.discard(queue)
        if not self._subscribers and not self.finished.is_set() and self._idle_handle is None:
            self._idle_handle = self._hass.loop.call_later(_IDLE_GRACE_SECONDS, self._idle_stop)

    @callback
    def _idle_stop(self) -> None:
        self._idle_handle = None
        if not self._subscribers:
            self.stop()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # -- event loop side of the worker -------------------------------------

    @callback
    def _broadcast(self, data: bytes) -> None:
        if not self.first_data.is_set():
            self.first_data.set()
        for queue in list(self._subscribers):
            if queue.qsize() > _SUBSCRIBER_BACKLOG:
                _LOGGER.warning("Dropping a stuck live consumer of %s", self.serial)
                self._subscribers.discard(queue)
                queue.put_nowait(None)
                continue
            queue.put_nowait(data)

    @callback
    def _finish(self, reason: str) -> None:
        self.end_reason = reason
        self.finished.set()
        self.first_data.set()  # release consumers still waiting for data
        for queue in self._subscribers:
            queue.put_nowait(None)
        self._manager.hub_finished(self)

    # -- worker thread -----------------------------------------------------

    def _pump(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bootstrap, read segments, decrypt, hand bytes to the event loop.

        If the cloud ends a VTM session while consumers are still watching, a
        new session is opened, until ``max_live_seconds`` or no consumer is
        left. Consecutive short segments stop the hub so a device that refuses
        to wake is not hammered.
        """
        deadline = time.monotonic() + self._max_seconds
        reason = "no consumers left"
        segments = 0
        short_segments = 0
        try:
            decryptor = IncrementalDecryptor(self._api.get_key(self.serial))
            while not self._stop.is_set():
                segments += 1
                if segments > 1:
                    if deadline - time.monotonic() < _MIN_SEGMENT_SECONDS:
                        reason = "max_live_seconds reached"
                        break
                    if short_segments >= _MAX_SHORT_SEGMENTS:
                        reason = f"{short_segments} short segments in a row, giving up"
                        break
                self._stream = self._open_stream()
                if self._stop.is_set():
                    break
                segment_start = time.monotonic()
                segment_reason = self._read_segment(loop, decryptor, deadline)
                if time.monotonic() - segment_start < _MIN_SEGMENT_SECONDS:
                    short_segments += 1
                else:
                    short_segments = 0
                tail = decryptor.flush()
                if tail:
                    loop.call_soon_threadsafe(self._broadcast, tail)
                with suppress(Exception):
                    self._stream.close()
                if segment_reason != "device ended segment":
                    reason = segment_reason
                    break
                _LOGGER.debug(
                    "Live stream of %s: segment %d ended by the device after %.1f s, reconnecting",
                    self.serial,
                    segments,
                    time.monotonic() - self.started_at,
                )
        except Exception as err:  # noqa: BLE001 - surface any stream failure in the log
            reason = f"{type(err).__name__}: {err}"
            if not self._stop.is_set():
                _LOGGER.warning("Live stream of %s stopped: %s", self.serial, reason)
        finally:
            if self._stop.is_set() and reason != "max_live_seconds reached":
                # Errors raised because stop() closed the socket are not failures.
                reason = "no consumers left"
            if self._stream is not None:
                with suppress(Exception):
                    self._stream.close()
            loop.call_soon_threadsafe(self._finish, reason)
            _LOGGER.debug(
                "Live stream of %s ended (%s), %.1f s, %d segments, %d bytes, first data after %s s",
                self.serial,
                reason,
                time.monotonic() - self.started_at,
                segments,
                self.bytes_in,
                f"{self.first_data_at - self.started_at:.1f}" if self.first_data_at else "-",
            )

    def _open_stream(self) -> Any:
        """Bootstrap a VTM session, retrying once when the cloud stalls."""
        last_err: Exception | None = None
        for attempt in range(1, _OPEN_ATTEMPTS + 1):
            if self._stop.is_set():
                return None
            try:
                return self._api.open_stream(self.serial)
            except Exception as err:  # noqa: BLE001
                last_err = err
                _LOGGER.debug(
                    "Cloud bootstrap of %s failed (attempt %d): %s", self.serial, attempt, err
                )
        assert last_err is not None
        raise last_err

    def _read_segment(
        self, loop: asyncio.AbstractEventLoop, decryptor: IncrementalDecryptor, deadline: float
    ) -> str:
        """Read one VTM session; return why it ended."""
        stream = self._stream
        stream.start()
        got_data = False
        try:
            for packet in stream.iter_packets():
                if self._stop.is_set():
                    return "no consumers left"
                if time.monotonic() >= deadline:
                    return "max_live_seconds reached"
                if packet.encrypted:
                    _LOGGER.warning(
                        "%s: VTM transport encryption (ECDH) is not supported", self.serial
                    )
                    return "unsupported transport encryption"
                body = packet.body or b""
                if not body:
                    continue
                if not got_data:
                    got_data = True
                    if self.first_data_at is None:
                        self.first_data_at = time.monotonic()
                    # Waking the device can take several seconds; once data
                    # flows, a short timeout detects the end of the segment.
                    with suppress(Exception):
                        stream._socket.settimeout(_STALL_TIMEOUT)  # noqa: SLF001
                self.bytes_in += len(body)
                out = decryptor.feed(body)
                if out:
                    loop.call_soon_threadsafe(self._broadcast, out)
        except Exception as err:  # noqa: BLE001
            if self._stop.is_set():
                return "no consumers left"
            if got_data:
                _LOGGER.debug("Segment of %s ended: %s: %s", self.serial, type(err).__name__, err)
                return "device ended segment"
            raise
        return "device ended segment" if got_data else "no data"


@dataclass
class LiveManager:
    """Access tokens, hubs and cooldowns of every streamable device."""

    hass: HomeAssistant
    devices: dict[str, tuple[EzvizApi, int]] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)
    hubs: dict[str, DeviceHub] = field(default_factory=dict)
    cooldown_until: dict[str, float] = field(default_factory=dict)

    def register(self, serial: str, api: EzvizApi, max_seconds: int) -> None:
        """Make a device streamable and give it a fresh access token."""
        self.devices[serial] = (api, max_seconds)
        self.tokens.setdefault(serial, secrets.token_urlsafe(32))

    def unregister(self, serial: str) -> None:
        """Forget a device and stop its hub."""
        self.devices.pop(serial, None)
        self.tokens.pop(serial, None)
        hub = self.hubs.pop(serial, None)
        if hub is not None:
            hub.stop()

    def stream_url(self, serial: str) -> str:
        """Return the loopback URL the stream worker / go2rtc should open."""
        api_conf = self.hass.config.api
        scheme = "https" if api_conf is not None and api_conf.use_ssl else "http"
        port = api_conf.port if api_conf is not None else 8123
        path = STREAM_VIEW_URL.format(serial=serial)
        return f"{scheme}://127.0.0.1:{port}{path}?token={self.tokens[serial]}"

    def is_live(self, serial: str) -> bool:
        """Return True while the device streams."""
        hub = self.hubs.get(serial)
        return hub is not None and not hub.finished.is_set()

    @callback
    def get_hub(self, serial: str) -> DeviceHub:
        """Return the running hub of a device, starting one if needed."""
        hub = self.hubs.get(serial)
        if hub is not None and not hub.finished.is_set():
            return hub
        wait = self.cooldown_until.get(serial, 0.0) - time.monotonic()
        if wait > 0:
            _LOGGER.debug("Live stream of %s refused: cooldown for %.0f s", serial, wait)
            raise web.HTTPServiceUnavailable(text="Live view limit reached, retry later")
        api, max_seconds = self.devices[serial]
        hub = DeviceHub(self, api, serial, max_seconds)
        self.hubs[serial] = hub
        hub.start()
        async_dispatcher_send(self.hass, SIGNAL_LIVE_STATE.format(serial=serial))
        return hub

    @callback
    def hub_finished(self, hub: DeviceHub) -> None:
        """Called on the event loop when a hub's worker has ended."""
        if hub.end_reason == "max_live_seconds reached":
            self.cooldown_until[hub.serial] = time.monotonic() + LIVE_COOLDOWN_SECONDS
        if self.hubs.get(hub.serial) is hub:
            del self.hubs[hub.serial]
        async_dispatcher_send(self.hass, SIGNAL_LIVE_STATE.format(serial=hub.serial))


def get_live_manager(hass: HomeAssistant) -> LiveManager:
    """Return the shared live manager, registering the HTTP view once."""
    domain_data: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    manager = domain_data.get("live")
    if manager is None:
        manager = LiveManager(hass)
        domain_data["live"] = manager
        hass.http.register_view(EzvizLiveStreamView(manager))
    return manager


class _Remux:
    """One consumer's ffmpeg, fed from a hub queue."""

    def __init__(self, hass: HomeAssistant, queue: asyncio.Queue[bytes | None], mode: str) -> None:
        self._hass = hass
        self._queue = queue
        self._mode = mode
        self._process: asyncio.subprocess.Process | None = None
        self._writer: asyncio.Task[None] | None = None

    async def start(self) -> None:
        binary = get_ffmpeg_manager(self._hass).binary
        self._process = await asyncio.create_subprocess_exec(
            binary,
            *_FFMPEG_INPUT,
            *_FFMPEG_OUTPUT[self._mode],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._writer = asyncio.create_task(self._feed())

    async def _feed(self) -> None:
        assert self._process is not None and self._process.stdin is not None
        stdin = self._process.stdin
        try:
            while (chunk := await self._queue.get()) is not None:
                stdin.write(chunk)
                await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with suppress(Exception):
                stdin.close()

    async def output(self) -> AsyncIterator[bytes]:
        assert self._process is not None and self._process.stdout is not None
        while data := await self._process.stdout.read(_READ_CHUNK):
            yield data

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.cancel()
            with suppress(asyncio.CancelledError):
                await self._writer
        if self._process is not None and self._process.returncode is None:
            with suppress(ProcessLookupError):
                self._process.kill()
            with suppress(Exception):
                await self._process.wait()


async def async_serve_live(
    manager: LiveManager, request: web.Request, serial: str, mode: str
) -> web.StreamResponse:
    """Subscribe to the device hub and stream it as the HTTP response.

    Shared by the MPEG-TS view and the MJPEG camera.
    """
    if serial not in manager.devices:
        raise web.HTTPNotFound
    hass = manager.hass
    hub = manager.get_hub(serial)
    if hub.subscriber_count >= MAX_SUBSCRIBERS_PER_DEVICE:
        raise web.HTTPServiceUnavailable(text="Too many live consumers")
    queue = hub.subscribe()
    started = time.monotonic()
    _LOGGER.debug(
        "Live stream of %s (%s) requested by %s (%s), %d consumers",
        serial,
        mode,
        request.remote,
        request.headers.get("User-Agent", "-"),
        hub.subscriber_count,
    )
    remux = _Remux(hass, queue, mode)
    bytes_out = 0
    outcome = "stream ended"
    try:
        with suppress(TimeoutError):
            async with asyncio.timeout(_FIRST_DATA_TIMEOUT):
                await hub.first_data.wait()
        if not hub.first_data.is_set() or (hub.finished.is_set() and hub.bytes_in == 0):
            outcome = f"no data ({hub.end_reason or 'timeout'})"
            raise web.HTTPServiceUnavailable(text="Live stream unavailable")
        await remux.start()
        content_type = MJPEG_CONTENT_TYPE if mode == MODE_MJPEG else "video/mp2t"
        response = web.StreamResponse(
            headers={"Content-Type": content_type, "Cache-Control": "no-store"}
        )
        await response.prepare(request)
        try:
            async for data in remux.output():
                await response.write(data)
                bytes_out += len(data)
        except ConnectionResetError:
            outcome = "consumer disconnected"
        return response
    except asyncio.CancelledError:
        outcome = "request cancelled (consumer disconnected)"
        raise
    finally:
        # Unsubscribe first: the awaits below can be interrupted when the
        # handler task is cancelled, and a lingering subscriber would keep the
        # device awake.
        hub.unsubscribe(queue)
        _LOGGER.debug(
            "Live stream of %s (%s) closed: %s, %.1f s, %d bytes sent",
            serial,
            mode,
            outcome,
            time.monotonic() - started,
            bytes_out,
        )
        cleanup = hass.async_create_background_task(remux.close(), f"ezviz_live_close_{serial}")
        with suppress(asyncio.CancelledError):
            await asyncio.shield(cleanup)


class EzvizLiveStreamView(HomeAssistantView):
    """Serve the decrypted MPEG-TS stream to local consumers only."""

    url = STREAM_VIEW_URL
    name = "api:ezviz_live:stream"
    requires_auth = False  # consumers (go2rtc, stream worker) cannot send HA auth

    def __init__(self, manager: LiveManager) -> None:
        self._manager = manager

    async def get(self, request: web.Request, serial: str) -> web.StreamResponse:
        """Stream MPEG-TS for one device."""
        manager = self._manager
        # Every rejection is a 404, never a 401: a 401 counts as a failed login
        # for the IP-ban middleware.
        if not _is_local(request.remote):
            raise web.HTTPNotFound
        expected = manager.tokens.get(serial)
        given = request.query.get("token", "")
        if expected is None or not secrets.compare_digest(expected, given):
            raise web.HTTPNotFound
        return await async_serve_live(manager, request, serial, MODE_TS)


def _is_local(remote: str | None) -> bool:
    if not remote:
        return False
    try:
        address = ip_address(remote)
    except ValueError:
        return False
    return address.is_loopback or address.is_private
