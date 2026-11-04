"""Constants for the Tuya Vacuum Local Map integration."""

DOMAIN = "tuya_vacuum_local_map"

CONF_HOST = "host"
CONF_DEVICE_ID = "device_id"
CONF_LOCAL_KEY = "local_key"
CONF_VERSION = "version"

CONF_HIDDEN_ROOMS = "hidden_rooms"
CONF_BRUSH = "brush"
CONF_UNITS_PER_METRE = "units_per_metre"

VERSIONS = ["3.1", "3.3", "3.4", "3.5"]
DEFAULT_VERSION = "3.3"
DEFAULT_BRUSH = 300
DEFAULT_UNITS_PER_METRE = 1000

ISSUE_STALLED = "connection_stalled"
