#!/bin/sh
# Drop root and run as the Unraid user (nobody:users = 99:100 by default)
# so downloaded files get normal share permissions.
set -e
umask "$UMASK"
mkdir -p "$CONFIG_DIR"
chown -R "$PUID:$PGID" "$CONFIG_DIR"
# Docker creates a missing host folder as root; hand just that folder (not its contents) to the app user.
[ "$(stat -c %u "$ANIME_DIR")" = 0 ] && chown "$PUID:$PGID" "$ANIME_DIR"
exec gosu "$PUID:$PGID" python3 /app/server.py
