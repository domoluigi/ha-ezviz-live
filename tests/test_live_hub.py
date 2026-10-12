"""DeviceHub: one device session shared by every live-view consumer.

A fake device replaces the EZVIZ cloud: no network, no ffmpeg.
"""

from __future__ import annotations

import asyncio
import time
import types

from aiohttp import web
import pytest

from custom_components.ezviz_live import live


class _Packet:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.encrypted = False


class _FakeStream:
    def __init__(self, api: _FakeApi) -> None:
        self._api = api
        self.closed = False
        self._socket = types.SimpleNamespace(settimeout=lambda _t: None)

    def start(self) -> None:
        """Nothing to negotiate."""

    def iter_packets(self):
        sent = 0
        while not self.closed:
            if self._api.segment_packets and sent >= self._api.segment_packets:
                raise TimeoutError("device ended segment")
            sent += 1
            yield _Packet(b"x" * 100)
            time.sleep(0.01)
        raise OSError("VTM socket is not connected")

    def close(self) -> None:
        self.closed = True


class _FakeApi:
    def __init__(self, segment_packets: int = 0) -> None:
        self.opens = 0
        self.segment_packets = segment_packets

    def get_key(self, _serial: str) -> None:
        return None  # bytes pass through undecrypted

    def open_stream(self, _serial: str) -> _FakeStream:
        self.opens += 1
        return _FakeStream(self)


@pytest.fixture(autouse=True)
def _fast_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "_MIN_SEGMENT_SECONDS", 0.3)
    monkeypatch.setattr(live, "_IDLE_GRACE_SECONDS", 0.3)
    monkeypatch.setattr(live, "LIVE_COOLDOWN_SECONDS", 5)


async def _drain(queue: asyncio.Queue, seconds: float) -> int:
    got = 0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            item = await asyncio.wait_for(queue.get(), 0.05)
        except TimeoutError:
            continue
        if item is None:
            break
        got += len(item)
    return got


async def _finish(hub: live.DeviceHub, timeout: float = 3) -> None:
    await asyncio.wait_for(hub.finished.wait(), timeout)
    await asyncio.get_running_loop().run_in_executor(None, hub._thread.join, 2)  # noqa: SLF001


async def test_consumers_share_one_device_session(hass) -> None:
    api = _FakeApi()
    manager = live.LiveManager(hass)
    manager.register("S1", api, 60)

    hub = manager.get_hub("S1")
    first = hub.subscribe()
    await asyncio.wait_for(hub.first_data.wait(), 2)
    same = manager.get_hub("S1")
    second = same.subscribe()
    got_first, got_second = await asyncio.gather(_drain(first, 0.5), _drain(second, 0.5))

    assert same is hub
    assert api.opens == 1
    assert got_first > 0 and got_second > 0
    assert manager.is_live("S1")

    hub.unsubscribe(first)
    hub.unsubscribe(second)
    await _finish(hub)


async def test_stops_after_grace_when_last_consumer_leaves(hass) -> None:
    manager = live.LiveManager(hass)
    manager.register("S2", _FakeApi(), 60)
    hub = manager.get_hub("S2")
    queue = hub.subscribe()
    await asyncio.wait_for(hub.first_data.wait(), 2)

    hub.unsubscribe(queue)
    await asyncio.sleep(0.1)
    assert not hub.finished.is_set()  # still inside the grace period

    await _finish(hub)
    assert hub.end_reason == "no consumers left"
    assert not manager.is_live("S2")
    assert "S2" not in manager.cooldown_until


async def test_rejoin_during_grace_reuses_the_session(hass) -> None:
    api = _FakeApi()
    manager = live.LiveManager(hass)
    manager.register("S3", api, 60)
    hub = manager.get_hub("S3")
    queue = hub.subscribe()
    await asyncio.wait_for(hub.first_data.wait(), 2)

    hub.unsubscribe(queue)
    await asyncio.sleep(0.1)
    again = manager.get_hub("S3").subscribe()
    await asyncio.sleep(0.5)

    assert not hub.finished.is_set()
    assert api.opens == 1
    hub.unsubscribe(again)
    await _finish(hub)


async def test_limit_then_cooldown_refuses_new_sessions(hass) -> None:
    manager = live.LiveManager(hass)
    manager.register("S4", _FakeApi(), 1)
    hub = manager.get_hub("S4")
    queue = hub.subscribe()

    await _finish(hub)
    assert hub.end_reason == "max_live_seconds reached"
    assert await _drain(queue, 0.3) >= 0
    with pytest.raises(web.HTTPServiceUnavailable):
        manager.get_hub("S4")


async def test_reconnects_when_the_device_ends_a_segment(hass) -> None:
    api = _FakeApi(segment_packets=40)
    manager = live.LiveManager(hass)
    manager.register("S5", api, 60)
    hub = manager.get_hub("S5")
    queue = hub.subscribe()

    assert await _drain(queue, 2.0) > 0
    assert api.opens >= 2
    hub.unsubscribe(queue)
    await _finish(hub)


async def test_gives_up_after_short_segments(hass) -> None:
    api = _FakeApi(segment_packets=2)
    manager = live.LiveManager(hass)
    manager.register("S6", api, 60)
    hub = manager.get_hub("S6")
    hub.subscribe()

    await _finish(hub, 5)
    assert "short segments" in (hub.end_reason or "")
    assert api.opens <= 4


def test_incremental_decryptor_passes_through_without_key() -> None:
    decryptor = live.IncrementalDecryptor(None)
    assert decryptor.feed(b"abc") == b"abc"
    assert decryptor.flush() == b""
