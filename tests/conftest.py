import importlib.util

# The Home Assistant tests need the HA test plugin. Without Home Assistant installed
# they are skipped, so the rest still runs (core must work without it).
collect_ignore_glob = [] if importlib.util.find_spec("homeassistant") else ["ha/*"]
