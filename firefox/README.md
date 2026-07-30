# Grokive Prompt Studio — Firefox Extension

A companion browser extension for **[Grokive](../)**, the self-hosted archiver + browser
for Grok Imagine media. Grokive's web app includes a **Prompt Studio** — a library of saved
prompts organized into folders, with optional AI enhancement. This extension brings that
library into Firefox so fresh prompts are one click away while you generate on Grok Imagine.

## What it is and why

When you're working in Grok Imagine, the slow part is coming up with the next good prompt.
This extension lets you, without leaving the browser:

- **Search every saved prompt** across all folders and copy or insert the one you want.
- Pull a **random prompt** from any folder in your Grokive Prompt Studio library.
- Edit it freely, **copy** it, or push it straight into the Grok input field.
- **Enhance** it with your Grokive server's AI (rewrite / punch up / adjust dialogue).
- Generate **variations** of a prompt.
- **Save** new or edited prompts back into Grokive — into a folder named **`Firefox`** by default.

Everything stays between your browser and your own self-hosted Grokive server. No third
parties, no cloud, no telemetry.

## Features

- **Popup workflow** — pick a source folder, roll a random prompt, edit, copy/enhance/vary, save.
- **Prompt search** — one box searches your **whole library, across every folder**. Type any
  words in any order (they're matched against each prompt's text, its folder, and its tags) and
  the full prompts come back as a list: **📋 Copy** one to the clipboard, **Load** it into the
  editor, or ★ star it. Optionally scope the search to the selected source folder.
- **Connection status** at a glance (connected + authed / needs login / offline).
- **Enhance** with three dialogue levels and an optional "dialogue only" mode.
- **Variations** — generate a handful of alternates and click one to load it.
- **Save** edited or brand-new prompts to your `Firefox` folder (server-side append +
  dedupe — it never clobbers your other prompts).
- **Keyboard shortcut** `Alt+Shift+R` — copy a random prompt to the clipboard from anywhere. The
  notification names the pool it drew from ("Copied from ★ Starred · 1 of 147").
- **grok.com toolbar** — a compact floating pill with Random / Find / Enhance / Save / Star buttons
  that write directly into the page's prompt field (falls back to clipboard if it can't find one).
- **Pick what 🎲 pulls from, on grok.com** — the **▾** beside the die (or right-click the die)
  opens a source picker: **★ Starred**, **All prompts**, **Unfiled**, or any folder, each with its
  count, filterable because a tagged library runs to hundreds of folders. Your choice is the same
  `sourceFolder` the popup and the `Alt+Shift+R` hotkey use, so all three stay in step. Every roll
  then says where it came from — *"Random from ★ Starred · 1 of 147"* — so an unlucky repeat out of
  a small folder is never mistaken for a broken shuffle. **↻** re-reads your folders when you've
  been starring or tidying in the Grokive web UI.
- **Find a prompt on grok.com** — the 🔎 button opens a search panel over your whole prompt
  library; click a result to read it in full, then **Insert** it into the Grok input or 📋 copy it.
- **Reference images** — a 📎 button on the grok.com toolbar opens your Grokive **collections**;
  pick one to browse its image thumbnails and **copy** any image to the clipboard as PNG, ready to
  paste into Grok Imagine as a reference image. Thumbnails and full images are fetched through the
  background (so auth + CORS never get in the way), and the full image is pre-warmed on hover so the
  copy is instant.
- **Weekly usage readout** — on Grok Imagine pages, the toolbar shows a compact ⚡ badge with
  the percentage of your weekly allowance used; hover (or tap) it to expand the full breakdown —
  a total usage bar, a per-product split (Imagine / Chat / Voice / …), the time until the weekly
  reset, and any extra usage credits — read straight from Grok's own usage/billing endpoint using
  your existing grok.com session.
- **Options page** with a "Test connection" button so you can confirm setup before relying on it.

## How it talks to Grokive

The extension's background page is the only thing that touches the network. It calls your
Grokive server at the configured **Server URL** using these endpoints:

| Method & path | Used for |
|---|---|
| `GET /api/auth/status` | Check whether auth is required and whether you're logged in. |
| `POST /api/login` | Log in with stored credentials (only when the server requires auth). |
| `GET /api/prompts/status` | Check if the AI (LLM) is configured, and which model. |
| `GET /api/prompts/responses` | Load your prompt library and its folders (also backs prompt search and the 🎲 pool). Cached in the background for a minute and shared by all four surfaces, so rolling repeatedly doesn't re-download it; the server sends a `Cache-Control: no-cache` + `ETag` pair, so even a cold re-read is usually an empty `304`. |
| `POST /api/prompts/responses/add` | Save a prompt into a folder (append + dedupe). |
| `POST /api/prompts/enhance` | Rewrite / enhance a prompt's text. |
| `POST /api/prompts/generate` | Generate variations of a prompt. |
| `GET /api/collections` | List your saved collections (covers + image/item counts). |
| `GET /api/media?collection=<id>&type=image` | List the images in a collection. |
| `GET /thumbnails/…` & `/media/…` | Fetch a thumbnail / full image as bytes (for the References panel). |

- A **folder** is just a label on each prompt. Prompts with no folder show up as **Unfiled**.
- New prompts are saved into the **`Firefox`** folder by default (configurable in Options).
- **AI features are gated on the server.** Enhance and Variations only work when your Grokive
  server has its LLM configured. If it isn't, those buttons are disabled with a tooltip and the
  status line says the AI isn't configured.

## Install

This is a **Manifest V2** extension, loaded as a temporary add-on (vanilla JS, no build step).

1. Open Firefox and go to `about:debugging`.
2. Click **This Firefox** in the left sidebar.
3. Click **Load Temporary Add-on…**.
4. Select **`manifest.json`** inside the `firefox/` folder of this repo.

```
firefox/
  manifest.json   <- pick this file
```

> **Temporary add-ons clear when Firefox restarts.** You'll need to load it again after a
> restart — unless you package and sign it for a permanent install (next section).

## Packaging & making it permanent

A temporary add-on is gone on the next Firefox restart. To install it permanently you need a
packaged `.xpi`. Build one with the included script (no Node required):

```powershell
pwsh -File firefox/package.ps1
# -> firefox/dist/grokive-promptstudio-<version>.xpi
```

The script zips only the runtime files (manifest at the archive root) and names the output
after the manifest `version`. It's **unsigned**. From there, pick one:

**Option A — Sign it via AMO (works in normal Firefox, recommended).**
Firefox release/Beta only installs *signed* extensions permanently. Mozilla will sign an
**unlisted** (self-distributed) build for you — automated, not publicly listed or reviewed:

1. Create a free account at [addons.mozilla.org](https://addons.mozilla.org/developers/) and
   generate API credentials under **Manage API Keys**.
2. Set the two credentials as environment variables and run the included helper, which wraps
   [`web-ext`](https://extensionworkshop.com/documentation/develop/web-ext-command-reference/)
   (via `npx`, nothing to install) with the right flags and fails fast if anything's missing:
   ```powershell
   $env:WEB_EXT_API_KEY    = "user:1234567:89"     # JWT issuer
   $env:WEB_EXT_API_SECRET = "your-long-secret"    # JWT secret
   pwsh -File firefox/sign.ps1
   ```
   It uploads, AMO signs it, and the signed `.xpi` is downloaded into `firefox/dist/`.
   *(The helper just adds the right flags. Equivalent raw command: `npx web-ext sign
   --channel=unlisted --source-dir=firefox --artifacts-dir=firefox/dist
   --ignore-files=README.md --ignore-files=package.ps1 --ignore-files=sign.ps1 --ignore-files=dist/**`
   — web-ext reads the two env vars automatically. Or upload the signed `.xpi`
   manually: AMO → Submit a New Add-on → “On your own”.)*
3. Install the signed `.xpi`: `about:addons` → the gear ⚙ → **Install Add-on From File…**.
   Because the manifest sets a stable id (`grokive-promptstudio@local`), updates keep the same
   add-on and your settings.

**Option B — Unsigned, on an unsigned-capable build (no AMO account).**
Only **Firefox Developer Edition, Nightly, or ESR** can disable signature enforcement:

1. `about:config` → set **`xpinstall.signatures.required` = `false`**.
2. `about:addons` → gear ⚙ → **Install Add-on From File…** → pick the `.xpi` from `package.ps1`.

> Regular release / Beta Firefox **cannot** disable signature checks — use Option A there.

**Develop without rebuilding:** `npx web-ext run --source-dir=firefox` launches a throwaway
Firefox profile with the extension auto-loaded and live-reloading on file changes;
`npx web-ext lint --source-dir=firefox` validates the manifest and code.

## Configuration

Open the extension's **Options** page (the gear in the popup, or via `about:addons` →
Grokive Prompt Studio → Preferences) and set:

- **Server URL** — your Grokive origin. Default `http://localhost:8080`. A trailing slash is
  trimmed automatically.
- **Username / Password** — *only needed if your server requires login.* On a trusted LAN the
  simplest setup is to run the server with **`AUTH_DISABLED=true`** for zero-config access.
- **Save folder** — the folder new prompts are saved into. Default **`Firefox`**.
- **Default dialogue level** — the Enhance intensity used by default, shown in the popup as
  **Natural / Suggestive / Unfiltered**.
- **Show toolbar on grok.com** — toggles the floating toolbar on Grok pages.

Use **Test connection** to verify: whether the server is reachable, whether auth is required
and whether you're authed, whether the AI is ready (and which model), and your folder / prompt
counts.

## Usage

### Popup

Click the toolbar button to open the popup, then:

1. **Pick a source folder** — *All*, *★ Starred*, *Unfiled*, or a specific folder (with its prompt
   count). Your choice is remembered, and it's the same setting the grok.com toolbar's **▾** and the
   `Alt+Shift+R` hotkey use.
2. **🎲 Random prompt** — pulls a random prompt from that folder into the editable box.
   **↻ Another** re-rolls.
3. Or **🔎 search** the library — the box under the folder select searches **every folder** by
   default (tick *This folder only* to scope it). Results show the full prompt text (click a
   result to expand it), with **📋 Copy**, **Load** (into the editor) and **★** per row, and
   **Show more** to page through the rest.
4. **Edit** the text freely (there's a live character count).
5. **Copy** it, **✨ Enhance** it in place (with an **Undo** to restore the pre-enhance text),
   or get **Variations** and click one to load it.
6. **💾 Save** the current text to your `Firefox` folder.
7. Or use the **New prompt** box at the bottom to write something from scratch and save it.

### Keyboard shortcut

Press **`Alt+Shift+R`** anywhere to pull a random prompt from your last-picked source folder,
copy it to the clipboard, and show a notification titled with the pool it drew from
("Grokive — Copied from ★ Starred · 1 of 147") over a short preview. (You can rebind this in
`about:addons` → ⚙ → **Manage Extension Shortcuts**.)

### grok.com toolbar

On `grok.com`, a compact floating pill appears (bottom-right) with seven buttons:

- **🎲 Random** — inserts a random prompt into the Grok input field (or copies it to the
  clipboard if no field is found). The toast names the pool: *"Random from ★ Starred · 1 of 147"*.
- **▾ Source** — chooses which pool 🎲 draws from (see below). Right-clicking 🎲 does the same.
- **🔎 Find** — opens a search panel over every saved prompt (see below).
- **✨ Enhance** — enhances the current field text in place, using your default dialogue level.
- **💾 Save** — saves the current field text to your save folder.
- **⭐ Star** — saves *and* stars the current field text (favorites it in Grokive).
- **📎 References** — opens the reference-image browser (see below).

**Choose the Random source.** Click **▾** (or right-click **🎲**) to open the source picker,
anchored to the toolbar like the other panels. It lists **★ Starred** and **All prompts** first,
then **Unfiled** and every folder A–Z, each with its prompt count; the current one carries a ✓.
Type in the filter box to narrow it — Enter takes the single remaining match. Picking a source
**saves it and stops**; it deliberately doesn't roll, so a stray click can never overwrite what
you've typed into Grok. **↻** re-reads your folders from Grokive, for when you've been starring or
re-filing prompts in the web UI and want the counts to catch up. Because this is the same stored
`sourceFolder` the popup and the hotkey read, changing it anywhere updates everywhere — live.

**Find a prompt.** Click **🔎** to open a search panel anchored to the toolbar. Type any words —
in any order — and it matches against each prompt's text, folder, and tags across your **entire
library**. Each result shows the prompt clamped to a few lines — **click the text** (or the **⌄**) to expand it
and read the whole thing, click again to collapse. **Insert** writes it straight into Grok's prompt
field (and closes the panel); **📋** copies it instead. **Show more** pages through the rest. The
panel closes on **Esc** or an outside click.

**Reference images.** Click **📎** to open a panel of your Grokive **collections** (each with a
cover and image count; locked collections show 🔒 and need unlocking in Grokive first). Click a
collection to see its image thumbnails, then click any thumbnail to **copy that image to the
clipboard** — then paste it (Ctrl/Cmd+V) into Grok Imagine as a reference image. Images are copied
as PNG. The panel opens anchored to the toolbar (above it by default), closes on **Esc** or an
outside click, and lazy-loads thumbnails as you scroll.

**Weekly usage readout.** On Grok Imagine pages (`grok.com/imagine`), the pill shows a compact
**⚡ badge** carrying the **% of your weekly allowance used**, colour-coded (green = comfortable,
under ~90% used; amber = nearly spent, 90%+; red = at the cap, 100%). **Hover** the badge — or
**tap** it on touch — to expand
the full breakdown: a **total usage bar**, a **per-product split** (Imagine · Chat · Voice · …,
each with its own share), the time until the **weekly reset**, and any **extra usage credits**
(prepaid balance / on-demand spend). It reads from Grok's own usage endpoint using your existing
grok.com session (no API key), refreshes every 5 min, and stays hidden on non-Imagine pages.

> Grok moved from per-generation counts to a single **weekly (or monthly) allowance shared across
> products**, with a per-product breakdown — this readout mirrors what you see in Grok's own
> **Usage** panel. There are no longer separate image-vs-480p-vs-720p counts.

**Move it:** drag the violet **G** handle to reposition the pill anywhere on the page — it
stays on-screen and remembers where you put it across page loads. A quick **click** (no drag)
on the G collapses/expands it; the **✕** hides it until the next reload.

You can turn this toolbar off in Options.


## Privacy

The extension sends your prompts, library, collection images, and AI requests **only** to the
Grokive server you configure — nothing goes to any third party, and there is no analytics or telemetry. The one
other request it makes is the **weekly usage read**: a same-origin call to `grok.com`'s own
usage/billing endpoint, made only while you're on a Grok Imagine page, using the session you're
already logged in with. It reads your usage percentages and sends nothing new anywhere.

## How it works (architecture)

- **Manifest V2**, vanilla JS, no bundler or framework.
- The **background page does all *Grokive* network requests.** The Grokive server sends no CORS
  headers, but a background-page `fetch` made with host permission isn't subject to CORS. The
  popup, options page, and content script never fetch the Grokive server directly — they send
  messages to the background, which returns a consistent `{ ok, data?, error? }` shape.
- **One exception:** the grok.com content script reads the weekly usage with a *same-origin*
  `fetch` to grok.com directly (`credentials:'include'`) — it's already running on that origin
  with the page's session, so there's no background round-trip or CORS issue. The endpoint is a
  gRPC-Web unary RPC, so the content script frames the request and decodes the small proto
  response by hand (no proto codegen / dependency). It polls only on `/imagine*` pages and stays
  out of the Grokive message path entirely.
- **Search runs in the background too.** `GET /api/prompts/responses` returns the whole library in
  one shot (there's no server-side prompt search — the Grokive web app filters client-side as well),
  so the background keeps a short-lived cache of it and does the matching there. A keystroke costs
  one small message and returns a single page of rows, not the whole library; saving or starring
  refreshes the cache immediately. Very long prompts come back preview-capped, and the full text is
  fetched by id only when you copy/insert/expand one.
- **Auth is handled in the background:** before a protected call (or on a `401`), it checks
  `/api/auth/status` and, if needed and credentials are configured, logs in and retries once.
- Settings live in `ext.storage.local` under a single `settings` key, with defaults filled in so
  a missing key never breaks the UI.

## Troubleshooting

- **"Can't reach Grokive…"** — the server isn't running or the Server URL is wrong. Confirm
  Grokive is up and the URL/port match (default `http://localhost:8080`).
- **"Login failed…" / keeps asking to log in** — check your username/password in Options. The
  simplest fix on a trusted LAN is to run the server with **`AUTH_DISABLED=true`**. If you do use
  login, note that session cookies are subject to SameSite rules — point the extension at the
  same origin the server actually serves.
- **"AI not configured on the server"** — Enhance/Variations need the server's LLM configured.
  Set up the server's LLM (e.g. an Ollama / LLM endpoint via the server's `LLM_SERVER_URL`) and
  re-check with **Test connection**.
- **grok.com field not found** — if the toolbar can't locate the prompt input (the page DOM
  changed), Random falls back to copying the prompt to your clipboard so you can paste it.

## Roadmap / possible enhancements

- **"More like this"** — semantic similarity using the server's embeddings
  (`/api/prompts/similar`).
- **Scene-beats import** — pull structured beats into the prompt flow.
- **Tags on save / auto-tag** — attach tags when saving, or auto-tag via `/api/prompts/autotag`.
- **MV3 port** — migrate to Manifest V3.
- **AMO signing** — a signed, permanent build distributed through Mozilla Add-ons.
