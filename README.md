<a href="https://buymeacoffee.com/starrlord"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="40" align="right"></a>

# Grokive

Download your Grok Imagine favorites and Agent canvases, then browse them in a modern, responsive web app — chain clips into a playlist and export them as one seamless video, auto-generate subtitles for every clip, and burn them straight into the merged file.

Grokive is a free, self-hosted archiver that keeps your Grok Imagine library entirely on hardware you control. You sign in once by pasting a cURL request copied from your browser session; from there the tool pulls your saved media down to local disk. Browsing happens in a **SvelteKit single-page web app** (run via Docker or `python server.py`) backed by a SQLite read-model, with full-text prompt search, favorites, stash, playlists, subtitle generation, themes, and an installable PWA. A small **CLI** handles the downloading and index builds.

## Screenshots

| Library | Lightbox player |
| :---: | :---: |
| [![The justified photo grid with filters and playlists](screenshots/web/main.jpg)](screenshots/web/main.jpg) | [![The lightbox video player with prompt and actions](screenshots/web/player.jpg)](screenshots/web/player.jpg) |
| **Tag browser** | **Playlist editor** |
| [![The searchable tag-cloud modal](screenshots/web/tags.jpg)](screenshots/web/tags.jpg) | [![The playlist editor with drag-to-reorder, play, and export](screenshots/web/playlist.jpg)](screenshots/web/playlist.jpg) |
| **Config** | **Login** |
| [![The Config panel: appearance, Grok account cURL, and Whisper subtitles](screenshots/web/config.jpg)](screenshots/web/config.jpg) | [![The themed login screen](screenshots/web/login.jpg)](screenshots/web/login.jpg) |

## Features

- Bulk-download Grok Imagine saved/favorited images and videos.
- Bulk-download Agent canvases (`/imagine/agent/<id>`) — all canvases or specific ones.
- Download individual posts by link (`/imagine/post/<id>`) — grabs the root media plus all its child posts.
- Resume-safe: rerun anytime; existing IDs are skipped.
- Saves prompt metadata (including canvas name) with every media file.
- Builds a SQLite read-model (`index.db`) with FTS5 full-text search that the web UI queries.
- Incremental: only new thumbnails and records are generated on each sync.
- Groups media created from the same normalized prompt.
- Browse by canvas: a Files/Canvases view with one album per canvas.
- Search across prompts, tags, models, and local filenames.
- Filter by media type: all, images, or videos.
- Filter by generated prompt tags, model names, and canvas.
- Sort by newest, oldest, prompt A-Z, or model A-Z.
- Open a same-prompt view to see every image/video created from that prompt.
- Click/copy prompts for reuse.
- Show parent media when parent metadata is available.
- Build video **playlists** and play them back-to-back, with fullscreen auto-advance and drag-to-reorder.
- **Export a playlist** (or an ad-hoc selection) as one merged MP4 — lossless stream-copy when clips match, otherwise a high-fidelity re-encode (audio always kept).
- Optional **subtitle generation** via a [Whisper ASR](https://github.com/ahmetoner/whisper-asr-webservice) server: writes `.srt`/`.vtt` per video, shows captions in the player, and can burn them into merged exports.
- **Modern web app (Docker):** a SvelteKit SPA backed by a SQLite + FTS5 read-model — paginated browsing, full-text prompt search, a justified photo grid, infinite scroll, and an installable **PWA** (great on iPhone).
- **Favorites** and **Stash:** ♥ items into a Favorites view; stash items to hide them from the main views (their own Stashed view, reversible).
- **Delete:** permanently remove an item (file + thumbnail + subtitles) from a thumbnail, the viewer, or in bulk via select mode. Deleted IDs are blocklisted in `deleted_ids.json` so future syncs never re-download them.
- **Themes** (Violet default, Classic, Light) and **layouts** (Grid, Editorial) — switchable in Config.
- Self-hosted and local-first: everything stays on your own hardware — no analytics, no external services, no account required.

## Run As A Docker Container (Unraid / self-hosted)

Instead of the CLI you can run the archiver as a web app. The container serves the
modern SvelteKit UI at `/` (see *Web App* below) backed by a small Flask API, plus a
**Sync** action that downloads favorites + Agent canvases and rebuilds the index, and a
**Config** panel to paste your captured cURL — no shell access needed. When a Whisper
server is configured (see *Subtitles*), a **Generate Subtitles** button also appears.
Long jobs stream their progress into an on-page **Log** overlay.

All state (`grok_auth.txt`, `metadata.json`, `index.db` (the derived SQLite
read-model), `library.json` (favorites/stash), `deleted_ids.json` (delete blocklist),
`playlists.json`, `settings.json`,
media, thumbnails, subtitle `.srt`/`.vtt` sidecars, and the built gallery) is written
under one volume: the container's `/data` (set via the `GROK_DATA_DIR` env var), so it
survives container updates. `index.db` is purely derived from `metadata.json` and
on-disk files, and is rebuilt automatically on startup and after each sync.

### docker compose

```bash
docker compose up -d --build
# open http://<host>:8080
```

1. Open the web UI and click **Config**.
2. Paste your `Copy as cURL (posix/bash)` request (see *Capture Your Grok Auth Request*) and Save.
3. Click **Sync**. The status pill shows progress; the gallery refreshes when done.

### Unraid

The published image (`ghcr.io/starrlord/grokive:latest`) is pulled automatically — no
building on the server needed.

1. **Install the template** so it shows up in *Docker → Add Container → Template*: drop
   **`my-grokive.xml`** into the user-templates folder on the flash drive. From an Unraid
   terminal/SSH:
   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/my-grokive.xml \
     https://raw.githubusercontent.com/starrlord/grokive/main/my-grokive.xml
   ```
   (The `my-` prefix marks it as a user template; `dockerMan` is Unraid's Docker
   manager.) Then go to **Docker → Add Container** and pick **grokive** from the
   *Template* dropdown.
2. Or skip the file copy and just **Add Container** → fill in manually:
   - **Repository:** `ghcr.io/starrlord/grokive:latest`
   - **Port:** `8080`
   - **Path:** `/data` → `/mnt/user/appdata/grokive`
   - **PUID/PGID:** `99` / `100` (defaults; downloads are owned by `nobody:users`)
3. Apply, then open the WebUI, set **Config**, and click **Sync**.

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `GROK_DATA_DIR` | `/data` | Where all state is stored. |
| `PORT` | `8080` | Web UI port. |
| `PUID` / `PGID` | `99` / `100` | File ownership for downloaded media. |
| `ADMIN_USER` / `ADMIN_PASSWORD` | `admin` / _(auto)_ | Login credentials for the **themed login screen**. If `ADMIN_PASSWORD` is unset, a strong password is generated on first run and **printed to the container log** (and saved to `admin_password.txt`). |
| `AUTH_DISABLED` | `false` | Set `true` to turn auth **off** (open UI). Only do this on a fully trusted, isolated LAN. |
| `TRUST_PROXY` | `false` | Set `true` when behind a reverse proxy so the app trusts `X-Forwarded-*` (real client IPs for rate-limiting, HTTPS detection for secure cookies). |
| `SESSION_COOKIE_SECURE` | `auto` | `true`/`false`/`auto`. `auto` = secure cookies when `TRUST_PROXY` is on (i.e. HTTPS at the proxy). Don't force `true` on plain HTTP or login won't persist. |
| `BASIC_AUTH_USER` / `BASIC_AUTH_PASS` | _(unset)_ | Legacy HTTP Basic auth (used instead of the login screen when set). |
| `SESSION_SECRET` | _(derived)_ | Optional override for the session-cookie signing key (otherwise derived from the admin credentials). |
| `WHISPER_SERVER_URL` | _(unset)_ | Whisper ASR endpoint (e.g. `http://host:9000/asr`). Enables the **Generate Subtitles** button. Overrides the value saved in **Config**. |
| `VIDEO_ENCODER` | `auto` | Re-encoder for playlist merges and burned-in subtitles. `auto` uses the NVIDIA GPU (NVENC) when one is visible to the container, else CPU `libx264`. Force with `nvenc` or `cpu`. See *GPU video encoding* below. |
| `SPA_DIR` | `/app/web/build` | Where the built SvelteKit app lives (advanced; the image sets this for you). |

Log out from **Config → Account**. Your Grok cURL cookies expire periodically — when a
sync fails with an auth error the status pill says *"Auth failed — update Config"*;
re-capture the cURL and paste it into **Config** again.

### GPU video encoding (NVIDIA NVENC)

Exporting a playlist (or selection) re-encodes only when the clips differ in
codec/resolution/frame-rate — clips that already match are concatenated **losslessly**
with no encode (so the GPU doesn't change that fast path). When a re-encode *is* needed
(mixed-resolution merges, or burning in subtitles), Grokive can offload it to an NVIDIA
GPU via **NVENC**, which is far faster than CPU `libx264` and frees up your cores.

By default (`VIDEO_ENCODER=auto`) the app probes once whether NVENC can initialise and
uses the GPU if so, otherwise it transparently falls back to `libx264` — so the same
image runs everywhere. Force it with `VIDEO_ENCODER=nvenc` or `VIDEO_ENCODER=cpu`. The
bundled ffmpeg already includes `h264_nvenc`; no rebuild is needed. Encoding uses H.264
(widest compatibility), so any NVENC-capable card works (GTX 10-series and newer, incl.
the RTX 30-series). Only the encode runs on the GPU; decoding and scaling stay on the CPU.

**What you need:**

1. **A visible GPU + driver on the host.** On Unraid, install the **Nvidia Driver**
   plugin (Community Apps) and reboot; note your card's UUID with `nvidia-smi -L`.
2. **Pass the GPU into the container.** On Unraid, edit the grokive container (advanced
   view) and add:
   - **Extra Parameters:** `--runtime=nvidia`
   - **Variable** `NVIDIA_VISIBLE_DEVICES` = `GPU-<your-uuid>` (or `all`)
   - **Variable** `NVIDIA_DRIVER_CAPABILITIES` = `all` (must include `video` for NVENC)

   With `docker run`, that's simply `--gpus all`:
   ```bash
   docker run -d --gpus all -p 8080:8080 -v ./data:/data ghcr.io/starrlord/grokive:latest
   ```
   With `docker compose`, add a GPU reservation to the service:
   ```yaml
   services:
     grokive:
       # …existing config…
       deploy:
         resources:
           reservations:
             devices:
               - driver: nvidia
                 count: all
                 capabilities: [gpu, video]
   ```
3. Nothing else — `VIDEO_ENCODER` stays `auto`.

**Verify it's working:** the container log prints `video encoder: NVENC (GPU)` on the
first export (or `libx264 (CPU)` if no GPU was found). You can also run `nvidia-smi`
inside the container and watch the encoder engage during a merge. (This requires the
NVIDIA Container Toolkit, which the Unraid plugin provides; without a GPU the app simply
uses the CPU.)

## Security

**Auth is on by default.** On first run with no `ADMIN_PASSWORD` set, the app generates a
strong admin password, prints it to the container log, and saves it to
`admin_password.txt` on the `/data` volume — so check the logs (or that file) to sign in,
or set your own `ADMIN_USER`/`ADMIN_PASSWORD`. Login is brute-force-limited (5 failed
attempts per IP → a 5-minute lockout), credentials are compared in constant time, and the
session is a signed, `HttpOnly`, `SameSite=Lax` cookie. The Grok cURL cookies are never
returned to the browser, only stored under `/data`.

- **Trusted internal LAN.** Plain HTTP is usually acceptable. Keep auth on, or set
  `AUTH_DISABLED=true` if the network is fully trusted and isolated. Still protect the
  `/data` volume (it holds your Grok login cookies and media).
- **Exposed outside your LAN / over the internet.** Put it behind an **HTTPS reverse
  proxy** (Nginx Proxy Manager, Caddy, Traefik, Cloudflare Tunnel, …) — the app speaks
  plain HTTP and has no built-in TLS. Then set **`TRUST_PROXY=true`** (so it sees real
  client IPs and marks the session cookie `Secure`), use a **strong `ADMIN_PASSWORD`**,
  and consider IP allow-listing or the proxy's own auth as a second layer. Never expose
  it directly without TLS — the session cookie would travel in clear text.

> The same `GROK_DATA_DIR` mechanism works for the CLI too: set the env var and
> `grokive.py` reads/writes that directory instead of the repo folder.

## Web App (Modern UI)

When run in Docker, the archiver serves a **SvelteKit** single-page app at `/`, backed
by a SQLite read-model (`db.py` → `index.db`, with FTS5 full-text search) and a small
Flask API (`/api/media`, `/api/facets`, …). Highlights:

- **Views:** Files, Favorites, Stashed, and Canvases tabs.
- **Justified photo grid** with infinite scroll and lazy thumbnails (*Grid* mode), or a
  prompt-forward **Editorial** layout — switch in Config.
- **Themes:** Violet (default), Classic, and Light (Config → Appearance). The ☾/☀ button
  quick-toggles light.
- **Search & filters:** full-text prompt/tag/model search in the top bar; a searchable
  **tag-cloud** modal (*Browse all tags*); media-type and model filters; one-click reset
  (the "Grokive" wordmark or the *Reset filters* chip).
- **Favorites & Stash:** hover a card for ♥ (favorite) and the stash icon (hide into the
  Stashed view; reversible).
- **Select mode:** multi-select for bulk favorite/stash, **Save as playlist**, or a
  one-off **Export**.
- **Lightbox:** the media fills the window; press `i` / tap ⓘ for prompt + actions, `f`
  for fullscreen, arrows to navigate; subtitle track shown when available.
- **Installable PWA:** add to your home screen on iOS/Android for a full-screen app.
- **Mobile:** a **Filters** button opens the same tag/model/type modal.

## Playlists And Export

Playlists let you collect a set of videos and watch or export them as one sequence.

- **Create:** click **Select** in the top bar, pick videos (in the order you want them), name the playlist, and **Save**.
- **Play:** click ▶ on a playlist to play its clips back-to-back. Enter fullscreen and each clip auto-advances to the next.
- **Edit:** click a playlist's name to open the editor — drag the handle (or use ▲/▼) to reorder, rename, or remove clips.
- **Export:** click **Export** to merge the playlist into a single MP4 download. Clips that already share codec/resolution/frame-rate are concatenated **losslessly** (no re-encode); if they differ, each is re-encoded onto the largest frame size at high quality. Audio is always preserved (silent clips get a silent track so nothing desyncs).

Export and merging use the server's `ffmpeg`. The merged file is created in a temporary directory, streamed to your browser, and deleted — nothing extra is left on the volume.

## Subtitles (Whisper)

The app can generate subtitles for your videos using a
[whisper-asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice) server.

1. Run a Whisper ASR server reachable from the app (default endpoint shape: `http://<host>:9000/asr`).
2. Open **Config** and set **Whisper Server URL** (or set the `WHISPER_SERVER_URL` env var). A **Generate Subtitles** button appears once it's configured.
3. Click **Generate Subtitles**. It transcribes every video without a matching `.srt`, writing `.srt` + `.vtt` next to the video. Progress streams into the **Log** overlay.

- Captions appear as a toggleable track in the lightbox.
- **Burn Subtitles** (a checkbox in **Config**): when enabled, exporting a playlist transcribes the merged video and burns the subtitles in (a re-encode at CRF 18; audio copied through). If transcription fails the export still completes without burned-in subtitles.
- Silent clips get an empty `.srt` (so they aren't re-processed) and no caption track. Whisper can hallucinate text on near-silent audio.
- Audio is extracted locally (16 kHz mono) before upload, so only a small file is sent to the Whisper server.

## Capture Your Grok Auth Request

**Both** the Docker app and the CLI need a copied **cURL** request from your logged-in browser — it carries your Grok login cookies. You only capture it once.

1. Open `https://grok.com/imagine/saved` (or `/imagine/favorites`) and sign in.
2. Open DevTools (`F12`) → **Network** tab → enable **Preserve log** → filter to **Fetch/XHR**.
3. Refresh the page and find a request to `https://grok.com/rest/media/post/list`.
4. Right-click it → **Copy** → **Copy as cURL (posix/bash)**.

- **Docker / web app:** paste it into the **Config** panel and **Save**, then click **Sync**.
- **CLI / from source:** save it to a file named `grok_auth.txt` next to the scripts.

Treat that cURL like a password — it embeds your active login cookies. (`grok_auth.txt` is git-ignored, so it never lands in the repo.)

---

## Running From Source (without Docker)

> **Using Docker? You can skip this entire section.** The container already bundles
> Python, ffmpeg, and the built web app, and its **Sync** / **Config** buttons do
> everything below for you. The steps here are only for **development**, or for running
> on a host **without Docker**.

From source you run the same two pieces directly: `grokive.py` (the CLI that downloads
and indexes) and `server.py` (the Flask + SvelteKit web app).

### Requirements

- Python 3.10 or newer.
- `ffmpeg` — for video thumbnails, playlist merge/export, and subtitle audio extraction.
- Node.js 18+ — only to build the web UI; `server.py` serves the prebuilt SPA from `web/build`.
- Python packages from `requirements-server.txt` (it includes `requirements.txt`).

### Set up and run

Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-server.txt
python grokive.py check                      # verify dependencies
# create grok_auth.txt (see "Capture Your Grok Auth Request" above)
python grokive.py download                   # favorites
python grokive.py agents                     # optional: Agent canvases
python grokive.py index                      # thumbnails + index.db
cd web; npm install; npm run build; cd ..    # build the SPA (one-time / after UI changes)
python server.py                             # then open http://localhost:8080
```

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-server.txt
python grokive.py check
# create grok_auth.txt (see above)
python grokive.py download
python grokive.py agents
python grokive.py index
( cd web && npm install && npm run build )
python server.py
```

Auth works the same as Docker (see *Security*): on first run a generated admin password
is printed to the console and saved to `admin_password.txt`. Set `ADMIN_USER`/`ADMIN_PASSWORD`,
or `AUTH_DISABLED=true`, before starting if you prefer.

### Downloading

`python grokive.py download` fetches favorites; `python grokive.py agents` fetches Agent
canvases (all of them, or pass specific IDs / `/imagine/agent/<id>` URLs). Both write to
`media/images/`, `media/videos/`, and `metadata.json`, and skip anything already
downloaded. Shortcut: `python grokive.py all` runs download → index in one go.

To grab a single post rather than your whole library, `python grokive.py post <id-or-url> [...]`
downloads one or more posts by id or `/imagine/post/<id>` link (the root media plus its
child posts) — same resume-safe, skip-existing behavior. Run `python grokive.py index`
afterwards if you want it in the web UI.

### CLI-only mode (no web UI)

If you just want your media as local files — no browsing interface — you only need the
downloader:

```powershell
python grokive.py download      # add `python grokive.py agents` for canvases
```

That leaves your images/videos under `media/` and a `metadata.json` describing them, and
nothing else runs (no server, no Node, no index). There is no standalone HTML gallery; to
*browse* in the app you additionally run `python grokive.py index` and `python server.py`
as shown above.

### Developing the web UI

For live-reload development, run the Vite dev server (it proxies the API to Flask), with
`python server.py` running in another terminal:

```bash
cd web && npm run dev    # http://localhost:5173 ; proxies /api, /media, /thumbnails -> :8080
```

### Updating later

```powershell
python grokive.py download
python grokive.py index
```

Existing media and thumbnails are skipped. Reuse the same `grok_auth.txt` while it
works; re-capture it if Grok auth starts failing. (In the web app the **Sync** button does
all of this for you.)

### Run from an IDE

The same commands work from any IDE terminal (VS Code, PyCharm, Cursor, …): open the
folder, create a venv, install `requirements-server.txt`, create `grok_auth.txt`, then
run `grokive.py download` / `index` and `server.py`.

## Privacy

Everything runs on hardware you control. The app keeps your media, prompts, cookies, and metadata on local disk and never ships them to a third party — the only outbound traffic is the calls to Grok it makes on your behalf, signed with the cURL session you supply. The lone exception is optional subtitle generation, which reaches out to a Whisper server only when you choose to configure one (and that can be a box on your own LAN).

## Disclaimer

This is an independent project with no affiliation to xAI, Grok, or X. It depends on Grok's private endpoints, which can change at any time and break it without warning. Archive only content your own account can access, and use it responsibly.
