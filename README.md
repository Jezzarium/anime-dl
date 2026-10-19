For my unraid server, uses ani-cli to download animes to my jellyfin. entirely vibe coded. i just want to watch anime



# anime-dl

A small web UI + API around [ani-cli](https://github.com/pystardust/ani-cli) that downloads
anime straight into a Jellyfin library:

```
<anime folder>/Frieren Beyond Journey's End/Season 02/Frieren Beyond Journey's End S02E05.mp4
                                                     .../Frieren Beyond Journey's End S02E05.en.vtt
```

The container only sees the one folder you map to `/anime`. The API can search, queue
downloads, cancel/retry and list what's in the library. It has no endpoint that deletes,
renames or overwrites anything, and existing episodes are always skipped.

## Install on Unraid

The image is built by GitHub Actions and published as `ghcr.io/jezzarium/anime-dl`.

**1. Install the template and generate a token** (Unraid terminal, top-right `>_` icon)

```sh
wget -O /boot/config/plugins/dockerMan/templates-user/my-anime-dl.xml \
  https://raw.githubusercontent.com/Jezzarium/anime-dl/main/my-anime-dl.xml
openssl rand -hex 24      # copy this, it's your password for the UI and API
```

**2. Add the container**

Docker tab → **Add Container** → Template: **anime-dl** (under User templates).

- **Anime folder**: the host folder for anime, e.g. `/mnt/user/media/anime` (create it first if needed)
- **API token**: paste the token from step 1
- **Jellyfin URL / API key** (optional): e.g. `http://192.168.1.10:8096` and a key from
  Jellyfin → Dashboard → API Keys. Lets the app trigger a library scan after downloads.

Click **Apply**, then open `http://<unraid-ip>:8686` and enter the token.

**3. Jellyfin**

Add a library: Content type **Shows**, folder = the same anime folder (as Jellyfin's
container sees it). Under the library's settings you can enable **Real-time monitoring**
if you didn't set the Jellyfin API key.

Tip: Jellyfin only needs this path read-only. Mapping it with access mode "Read Only"
in the Jellyfin container is a cheap extra safety net.

## Updating

- **Site changed / downloads break:** click **Update ani-cli** in the UI. The updated
  script is kept in appdata and survives restarts.
- **Everything else:** every push to `main` publishes a new `:latest` image. In the Docker tab,
  click **Check for Updates**, then **apply update** on anime-dl.

To build locally instead: `git clone` the repo, `docker build -t anime-dl:latest .`, and set
the template's Repository to `anime-dl:latest`.

## Tips

- Seasons are often separate search results ("Frieren Season 2"). The UI guesses the show
  name and season number from the title so they land in the same show folder; adjust if needed.
- "All episodes" on an ongoing show is safe to repeat: episodes you already have are skipped.
- Half episodes like `12.5` go into a `Specials` folder.

## API

Every `/api/*` call needs `Authorization: Bearer <API_TOKEN>`. Bodies are JSON.

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/search` | `{"query": "frieren"}` | `{"results": [{"index": 1, "title": "..."}]}` |
| POST | `/api/episodes` | `{"query", "index"}` | `{"episodes": ["1", "2", ...]}` |
| POST | `/api/download` | `{"query", "index", "title", "episodes", "show"?, "season"?, "dub"?}` | the job |
| GET | `/api/jobs` | | `{"jobs": [...]}` newest first |
| POST | `/api/jobs/<id>/cancel` | | the job |
| POST | `/api/jobs/<id>/retry` | | the job (requeues failed/cancelled) |
| POST | `/api/jobs/clear` | | removes finished jobs from the list (not files) |
| GET | `/api/library` | | `{"shows": [{"show", "episodes"}]}` |
| GET | `/api/status` | | ani-cli version, queue size |
| POST | `/api/update-ani-cli` | | before/after version |

`episodes` is `"all"`, `"latest"`, a range string like `"1-12"` or `"1,3,5"`, or a list.
`title` must be the title from the search result: the download is rejected if the site's
results changed in between, so you never get the wrong show.

```sh
T="Authorization: Bearer $API_TOKEN"; U=http://<unraid-ip>:8686
curl -s -H "$T" -d '{"query":"dandadan"}' $U/api/search
curl -s -H "$T" -d '{"query":"dandadan","index":1,"title":"Dandadan","episodes":"all"}' $U/api/download
```

## Settings

| Variable | Default | |
|---|---|---|
| `API_TOKEN` | (required) | 16+ characters |
| `QUALITY` | `best` | `best`, `worst`, or e.g. `1080` |
| `MAX_QUEUED_EPISODES` | `500` | queue cap |
| `SUBTITLE_LANG` | `en` | language tag on downloaded `.vtt` subtitles |
| `JELLYFIN_URL`, `JELLYFIN_API_KEY` | | optional library refresh |
| `PUID`, `PGID`, `UMASK` | `99`, `100`, `002` | file ownership (Unraid's nobody:users) |
