FROM python:3.13-slim-trixie

# ani-cli v5.1.5. Bump the commit and rebuild to update (or use "Update ani-cli" in the UI).
ARG ANI_CLI_COMMIT=21ed4a0a6354688622b5a967d93ba96d851d0ef2
ARG YT_DLP_VERSION=2026.8.19

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg ncurses-bin patch diffutils gosu tini \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "yt-dlp==${YT_DLP_VERSION}" \
 && mkdir -p /opt/ani-cli \
 && curl -fsSL -o /opt/ani-cli/ani-cli "https://raw.githubusercontent.com/pystardust/ani-cli/${ANI_CLI_COMMIT}/ani-cli" \
 && chmod 755 /opt/ani-cli/ani-cli

COPY app/ /app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /app/capmenu /app/server.py /entrypoint.sh

ENV ANIME_DIR=/anime CONFIG_DIR=/config PORT=8686 PUID=99 PGID=100 UMASK=002 PYTHONUNBUFFERED=1
EXPOSE 8686
VOLUME ["/anime", "/config"]
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
