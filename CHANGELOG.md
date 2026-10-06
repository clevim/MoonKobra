# Changelog

## [1.1.0] – MoonKobra

### Added
- **Windows `.exe`**: every release now ships `MoonKobra-vX.Y.Z-windows.exe`
  next to the Docker image. Download, double-click, and the browser opens on
  the dashboard - no Docker, no Python. The black console window *is* the
  program: closing it stops everything, including the camera's ffmpeg
  (Windows Job Object), even after a crash or a kill from Task Manager.
  Double-clicking it again while it runs just opens the dashboard; a click
  inside the window no longer freezes the bridge (QuickEdit off); on a crash
  the window waits for Enter so the error can be read.
- **First-run setup**: with no printer configured, the dashboard asks only for
  the printer's IP and the language, fetches the credentials from the printer,
  restarts and comes back on its own.
- **OrcaSlicer guide**: a replica of OrcaSlicer's *Physical Printer* window
  with the host URL and API key filled in and copy buttons. Shown on the
  dashboard until "Don't show again"; reopen it under *Settings → API*.
- New installs get an API key with the default login, so OrcaSlicer gets past
  the login out of the box. `/api/settings` now reports the host's `lan_ip`.

### Fixed
- Windows restart after saving settings no longer leaves the bridge running
  hidden in the background: it reopens in a visible console, and the onefile
  binary unpacks fresh instead of reusing the parent's deleted temp folder.
- The single binary now bundles ffmpeg (camera stream did not work in it).

### Changed
- `NOTICE.md` lists the bundled FFmpeg (GPLv3) and the Python runtime and
  libraries inside the `.exe`. The Anycubic certificate is still never shipped:
  the bridge uses the one the printer hands over in its LAN handshake.

## [1.0.0] – MoonKobra

### Added
- **Per-print log (Log → History)**: every print keeps its own log, including
  the lines from just before it started (upload, `print/start`, ACE mapping).
  It is written as plain text while printing and compressed to `.log.xz` when
  the print ends (~20x smaller). Open or download it as `.txt` from the new
  History sub-tab (`GET /kx/history/{id}/log`, `?download=1`). The last 100
  are kept. Prints started from the printer's screen (not through the bridge)
  now also show up in the history.
- **`print_stats.message`** now carries the pause/error reason, like Klipper.
- **Quote module (new "Quote" tab)**: pick a G-code from the file list, see the
  whole part in 3D, and get a price. `pricing.py` reads grams per tool, slicer
  time, density and the ACE purge matrix from the G-code, then adds material,
  power, machine wear, labor, failure reserve, packaging, margin, rush fee,
  shipping, tax and sales-channel fees (Shopee/Mercado Livre two-tier fees,
  solved "inside out" so fees are charged on the final price). Quantity
  discounts, coupons (percent or fixed, minimum order, expiry, on/off) and
  manual discounts come off the list price, and the shown profit is what is
  left after them. The price breakdown is drawn as a stage stack (cost at the
  bottom, profit on top). Quotes are saved (`MK-YYYY-NNNN`) and can be reopened.
  **Receipt for the customer**: a paper "manifest" with the 3D snapshot,
  materials, time, lines, coupon, total and validity stamp - as a PNG image
  (share sheet on phones), print/PDF, or WhatsApp-ready text. No internal cost
  or margin appears on it. All rates live in `data/pricing.json`, editable in
  the Configure sub-tab. Endpoints: `/kx/pricing/config`, `/kx/pricing/calc`,
  `/kx/pricing/quotes`.
- **3DFila prices in the Brazilian profiles**: `profiles/brasil/precos_3dfila.py`
  reads the full (non-sale) 1 kg price of every 3DFila color from the store and
  `gerar.py` writes it as `filament_cost` (R$/kg). Imported profiles keep that
  price, and the quote module uses it automatically for the matching
  `filament_settings_id` in the G-code (the generic Anycubic `filament_cost`
  is never trusted).
- **Quote defaults for Sep/2026 (Muriaé-MG)**: Kobra X at R$ 3,899 (full price,
  official BR store), 200 W average (150-250 W measured), energy R$ 1.25/kWh
  (Energisa Minas Rio B1 R$ 0.919 + yellow flag + ICMS 18% and PIS/COFINS),
  3DFila 1 kg prices in the materials table. Per-order cost switches (purge,
  energy, machine, labor, failure reserve, packaging) in the price breakdown;
  "Quote this part" button on each file card; app logo in the receipt badge;
  "Copy image" on the receipt.
- **Settings backup**: Settings → System exports/imports a `.zip` with
  `config.ini`, the quote settings and the imported filament profiles
  (validated before writing, then restart). The Quote tab also exports/imports
  its own `pricing.json`.
- **Quote: "Parts per plate"**: a G-code that prints several parts at once
  splits its time, filament and purge among them; finishing, packaging, the
  minimum price and per-item channel fees stay per part.

### Fixed
- **Quote**: a non-object JSON body (`[]`, `null`) sent to
  `/kx/pricing/config` reset the saved quote settings to the defaults; it is
  now rejected with 400 (also on calc/save, which used to answer 500). A 100%
  quantity discount plus a hand-typed total no longer crashes the calculation.
  "Receipt" right after "Save" (or twice in a row) reuses the same quote
  instead of saving a duplicate. Saving the settings stamps today as the
  "last review" date.
- **G-code store**: deleting an older upload no longer deletes the file of a
  newer upload with the same name (both point to the same file on disk; the
  file is now removed only when no entry uses it anymore).
- **No more Anycubic slicer certificates.** MoonKobra now uses the TLS
  certificate the printer itself hands over in the LAN handshake
  (`devicecrt`/`devicepk`), fetched on the first connection and saved in
  `config/certs/<device_id>.crt/.key` (delete the folder to fetch again). If
  the printer rejects the saved one, a fresh one is fetched once.
  `anycubic_slicer.crt`/`.key` are no longer shipped or required (still used as
  a fallback if present).
- `docker-compose.yml` no longer mounts `.env`: without that file (fresh clone,
  plain `docker compose up`) Docker created an empty `.env` folder.
- **Log panel**: selecting text to copy no longer gets wiped by incoming
  lines; the view waits while text is selected.
- **Bottom nav (phone)**: the Log badge showed the word "Log" instead of the
  count after the language was applied.
- **Date inputs** now use the same style as the other fields.
- **3D preview colors**: with two uploads of the same file name, the preview
  loaded the oldest one (wrong colors); it now uses the newest, and the quote
  preview uses the exact file picked.
- **Live log stream**: closing or reloading the tab no longer logs a long
  "Cannot write to closing transport" error every 25 s, and the stream stays
  open between keepalives instead of reconnecting and re-sending the whole log
  buffer.

### Changed
- **Project renamed from KX-Bridge to MoonKobra** (nickname: Moko), starting
  at version 1.0.0. There is no published image yet: `docker-compose.yml` and
  `start.sh` build it locally (`moonkobra:local`); the Portainer and full-stack
  compose files expect that local image. The PyInstaller build is
  `moonkobra.spec` / `moonkobra(.exe)` and the container is `moonkobra`.
- **Automatic updates removed** ("Check for updates" in Settings,
  `/api/update/check` and `/api/update/apply`): they downloaded the original
  project's releases. The old release workflows and the nightly compose files were
  removed too.
- The full-stack compose is now `docker-compose.stack.yml` and builds every
  image locally (no private registry). Portainer volumes are now
  `moonkobra-config` / `moonkobra-data`.
- **Settings → API**: new tab with the API key (generate / copy) and a quick
  API guide with ready-to-run curl examples.
- **New interface: "Mission Control".** The dashboard is now a fixed "Now"
  screen: the part itself is the progress (solid up to the current layer,
  hologram above it), a trajectory shows the print phases the printer reports,
  and Moko, the mascot, comments on the printer state. Temperatures show their
  trend, stopping a print requires holding the button for 1.4 s, the light
  switch sits on the camera, and the ACE lists its slots with a fixed symbol
  per slot plus the dryer of each unit. Display modes: Day, Night and Late
  night (red, for dark rooms). Type is Archivo; Barlow is gone.
- The customizable dashboard (GridStack, drag/resize, presets) was removed in
  favor of the fixed layout.
- **New app icon**: Moko peeking out of a crescent moon. Used as favicon,
  home-screen icon (`web/themes/default/lib/icon/`) and in the READMEs
  (`docs/assets/moonkobra-icon.png`).

### Added
- **Print notifications** (Settings → Connection → Notifications): one URL for
  ntfy, a Discord webhook, Telegram or any JSON webhook. Warns when a print
  finishes, fails or pauses with an error, in the UI language.
- **Anycubic error codes as text.** Known `code` values from `print/report`
  (e.g. 10121) are shown as a translated sentence in the pause/failure banner
  and in Moonraker's `display_status.message`. Failed prints now show the
  banner too. Code table from stribor/anycubic_kobrax (MIT, see NOTICE.md).
- **`server.temperature_store`** (HTTP + WebSocket): Mainsail, Fluidd and
  Mobileraker open the temperature graph with the last 20 minutes instead of
  an empty chart.

### Changed
- **Printer polling no longer blocks.** The poll loop sent `info`, `print` and
  `multiColorBox` queries one after another and waited up to 5 s for each, so a
  slow printer stretched a cycle past 15 s, and every reply was processed twice
  (callback + loop). Queries are now sent without waiting, replies go only
  through the MQTT callbacks, and a dead session is detected by the time since
  the last received message. `tempature` and `fan` are queried as well.
- **`notify_status_update` sends only changed fields**, like Moonraker, instead
  of every object on every tick.

### Security
- **Upload session token no longer ends up in the log.** `info/report` carries
  `fileUploadurl?s=<token>`; it was logged in full and could be downloaded via
  `/api/log/download`. Secret query parameters are now masked in all log output.

### Fixed
- **Slot kept showing/printing a stale filament type after a spool swap.** The
  per-slot profile override (config.ini `[filament_profiles]`) stores only
  vendor+name and was sticky: swapping the physical filament updated the AMS
  colour and type live, but the saved profile persisted, so a slot that held
  e.g. "KINGROON PETG Basic" kept showing/sending PETG in the panel and the
  OrcaSlicer lane hint even after yellow PLA was loaded — and survived restarts.
  The override is now applied only while its material *family* still matches the
  loaded AMS material (PLA / PLA+ / PLA SILK / PLA MATTE are one family, so
  within-family swaps never invalidate a valid profile). On a family change the
  slot falls back to the generic default; the override is not deleted, so
  reloading the original material reactivates it.
- **Filament profiles not isolated between printers in a multi-printer bridge**
  (issue #74). The slot→profile mapping and `visible_vendors` were stored in a
  single global `[filament_profiles]` section, so configuring one printer
  overwrote the other and after a restart both loaded the same mapping. Each
  printer now persists to its own `[filament_profiles_<id>]` section, with a
  read-fallback to the legacy global section (single-printer setups unchanged).
- **Printer dropdown showed the other printer's filament profiles** (issue #74).
  The header dropdown and the printers-management "switch" link navigated within
  the same port (`/printerN`), so viewing another printer pulled its profile
  names cross-instance from the local origin. The links now point at each
  printer's own `bridge_url`, so every printer is viewed same-origin on its own
  port.

---

Earlier history (up to 0.9.26) belongs to the original project this fork came
from: KX-Bridge by viewit.
