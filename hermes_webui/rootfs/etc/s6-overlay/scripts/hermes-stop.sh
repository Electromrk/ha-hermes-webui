#!/bin/sh
# Runs when the app stops, so also before every cold backup. The Supervisor's restore copies the app's
# folders with shutil.copytree, which stops at a named pipe, and a restore that stops in /data leaves /config
# missing too (measured). Pipes hold no data, and nothing uses them once the services are down.
# Nothing else is removed, and a failure here must not hold up the stop.
find -P /config/hermes /data/claude -type p -delete 2>/dev/null
exit 0
