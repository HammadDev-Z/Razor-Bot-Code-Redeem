#!/bin/sh
set -e

# Run Chromium normally (not headless), as on the Windows PC where login and
# purchases were verified, inside a virtual screen since the server has none.
if [ "${RAZER_HEADLESS:-true}" = "false" ] && [ -z "${DISPLAY:-}" ]; then
    # A container restart keeps /tmp, and a stale lock stops Xvfb starting.
    rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
    Xvfb :99 -screen 0 1440x1000x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
    export DISPLAY=:99
    # Wait (up to ~5s) for the virtual screen to accept connections.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        [ -e /tmp/.X11-unix/X99 ] && break
        sleep 0.5
    done
fi

# exec: the bot becomes the main process and receives stop signals directly.
exec "$@"
