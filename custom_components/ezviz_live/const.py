"""Constants for EZVIZ Live."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "ezviz_live"

CONF_TOKEN: Final = "token"
CONF_REGION: Final = "region"
CONF_MAX_LIVE_SECONDS: Final = "max_live_seconds"

DEFAULT_REGION: Final = "apiieu.ezvizlife.com"
DEFAULT_MAX_LIVE_SECONDS: Final = 300
MIN_MAX_LIVE_SECONDS: Final = 30
MAX_MAX_LIVE_SECONDS: Final = 1800

# Safety net for missed push messages; push triggers an immediate refresh.
MESSAGE_POLL_INTERVAL: Final = timedelta(seconds=60)
MQTT_WATCHDOG_INTERVAL: Final = timedelta(minutes=10)

# "All alarms" bundle of the unified message API.
MESSAGE_SUBTYPE_ALL_ALARMS: Final = "92"
MESSAGE_FETCH_LIMIT: Final = 10

# A battery device serves one VTM stream at a time (a second session makes both
# stall, seen on a CP4), so live.py keeps one session per device and fans it out
# to this many HTTP consumers (go2rtc, HLS worker, MJPEG browsers).
MAX_SUBSCRIBERS_PER_DEVICE: Final = 4
# EZVIZ API calls (pyezvizapi defaults to 25 s): the auth server sometimes
# stalls, and a live-view bootstrap should retry quickly instead of waiting.
API_TIMEOUT_SECONDS: Final = 10
# After max_live_seconds, refuse new sessions for this long so the HA stream
# worker's automatic reconnect does not restart the battery drain.
LIVE_COOLDOWN_SECONDS: Final = 120
STREAM_VIEW_URL: Final = "/api/ezviz_live/stream/{serial}.ts"

EVENT_TYPE_MOTION: Final = "motion"
EVENT_TYPE_DOORBELL: Final = "doorbell"
EVENT_TYPE_OTHER: Final = "other"

# alarmType codes observed in the unified message list.
ALARM_TYPE_PIR: Final = 10000

SIGNAL_NEW_EVENT: Final = f"{DOMAIN}_new_event_{{serial}}"
SIGNAL_LIVE_STATE: Final = f"{DOMAIN}_live_state_{{serial}}"
