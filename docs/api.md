# MoonKobra HTTP API reference

This document lists the HTTP and WebSocket surface exposed by
`kobrax_moonraker_bridge.py`. It is a reference for integrators and plugin
authors, not a tutorial. For day-to-day use see the user manual
([English](en/manual.md) · [Português](pt-BR/manual.md)); for installation see
the [README](../README.md). A short guide with ready-to-run examples is also in
the UI, under Settings → API.

The API has two parts:

1. **Moonraker-compatible surface**: a subset of the real
   [Moonraker](https://moonraker.readthedocs.io/) HTTP + WebSocket API,
   implemented just far enough for Mainsail, Fluidd, OrcaSlicer, Mobileraker
   and `moonraker-obico` to work with the Kobra X. **It is not a full Moonraker
   implementation**: many real endpoints and methods are missing, and some
   responses are static stubs that only keep a client from erroring out or
   looping (marked below).
2. **MoonKobra-specific surface**: `/api/...` and `/kx/...` endpoints for
   things Moonraker does not know about: several printers, AMS/ACE filament
   control, the G-code store, custom filament profiles, Spoolman, the smart-plug
   power switch, quotes, login and backup.

## Security

**Login is on by default.** On the first start MoonKobra creates the user
`kx` / `kx123` in the `[auth]` section of `config.ini` (the password is stored
only as a scrypt hash) and requires a password change on first access; until
then a session only reaches the login page, the password change and logout.
Login can be turned off in Settings → System (`enabled = 0`). Even with login,
MoonKobra is meant for the local network: do not expose port `7125` (or the
extra per-printer ports) to the internet.

With login on, the browser uses a session cookie after `/login`, and clients
such as OrcaSlicer and `moonraker-obico` send the `X-Api-Key` header (key in
Settings → API). `/access/api_key` then returns the configured key. Without
login it returns a fixed dummy value only so `moonraker-obico` does not
complain; it is not a real credential. Camera stream and snapshot also accept
`?token=` with a camera-only token (for OBS); it opens nothing else.

Requests coming from other websites are refused, and the `Host` header is
checked against DNS rebinding. Repeated wrong logins lock the client IP for a
while (HTTP 429).

---

## Moonraker-compatible endpoints (HTTP)

All responses use Moonraker's `{"result": {...}}` envelope unless noted.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/server/info` | Server/klippy status | Always reports `klippy_connected: true`, `klippy_state: "ready"` |
| GET | `/printer/info` | Printer identity | Static hostname/paths; `software_version` from `KLIPPER_VERSION` |
| GET | `/machine/system_info` | System info stub | Mostly static/placeholder fields |
| GET | `/printer/objects/list` | Available printer objects | Keys from `_build_printer_objects()` |
| GET | `/printer/objects/query?objects=...` | Query object status | Comma-separated `objects` parameter, or bare query keys |
| GET/POST | `/printer/objects/subscribe` | Subscription (HTTP polling variant) | Returns a full status snapshot right away |
| GET | `/server/files/list` | List G-code files | Only the file currently tracked (if any) |
| GET | `/server/files/metadata?filename=...` | File metadata (layers, estimated time, …) | Same logic as WS `server.files.metadata`; falls back to the G-code store / buried-report cache |
| POST | `/server/files/upload` | Upload a G-code file (multipart) | Same handler as `/api/files/local` |
| POST | `/printer/print/start?filename=...` | Start a print | Body may include `filament_assignments`, `excluded_objects`, `auto_leveling` |
| POST | `/printer/print/pause` | Pause the current print | |
| POST | `/printer/print/resume` | Resume the current print | |
| POST | `/printer/print/cancel` | Cancel the current print | |
| GET | `/access/api_key` | API key | The configured key with login on; a dummy without login |
| GET | `/machine/update/status` | Update-manager stub | Always `busy: false`, empty `version_info` |
| GET | `/server/history/list?limit=` | Print job history | From MoonKobra's own job database |
| GET | `/server/temperature_store` | Temperature history (nozzle and bed) | 1 sample per second, last 20 min: `temperatures`, `targets`, `powers` (always 0). Feeds the Mainsail/Fluidd/Mobileraker chart on open |
| GET | `/server/webcams/list` | Webcam descriptor | Replaces a `localhost`/`127.0.0.1` Host with MoonKobra's LAN IP so remote Obico/Mainsail get a reachable URL |
| POST | `/printer/gcode/script` | Run a G-code command (very limited) | See `_exec_gcode_script`; not a general G-code interpreter |
| GET | `/server/database/item?namespace=&key=` | Moonraker "database" KV read | Real payload only for `lane_data` (AMS/filament sync with OrcaSlicer); stub/empty answers for `AFC`, `afc-install`, `happy_hare`, `mainsail`; in-memory KV for `obico` |
| POST | `/server/database/item` | Moonraker "database" KV write | In memory only (lost on restart); used by `moonraker-obico` for its own settings |
| GET | `/server/database/list` | KV namespaces | Static: `["lane_data", "mainsail", "obico"]` |

### OctoPrint compatibility layer

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/version` | OctoPrint-style version probe (some tools check this instead of Moonraker) |
| POST | `/api/files/local`, `/api/files/{path}` | Alias of the multipart upload handler of `/server/files/upload` |
| GET | `/webcam/?action=stream` · `/webcam/?action=snapshot` | OctoPrint/mjpg-streamer camera URLs (OBS, apps); accept the camera-only `?token=` |

### WebSocket JSON-RPC (`/websocket`)

Moonraker's JSON-RPC 2.0 protocol over a single `/websocket` endpoint. On
connect MoonKobra sends `notify_klippy_ready` and the full status, then
`notify_status_update` with only the fields that changed since the last push,
as Moonraker does. Supported `method` values:

| Method | Purpose |
|---|---|
| `printer.info` / `printer_info` | Same payload as `/printer/info` |
| `server.info` / `server_info` | Same payload as `/server/info` |
| `printer.objects.list` | Same as the HTTP equivalent |
| `printer.objects.query` / `printer.objects.get` | Status of the requested object keys |
| `printer.objects.subscribe` | Returns a status snapshot (status pushes happen automatically through `notify_status_update`) |
| `printer.print.start` | `params.filename` |
| `printer.print.pause` / `.resume` / `.cancel` | |
| `machine.system_info` | Minimal stub |
| `server.files.list` | Always `[]` over WS (unlike the HTTP version) |
| `printer.gcode.script` | `params.script`, same limited executor as the HTTP endpoint |
| `server.connection.identify` | Returns a dummy `connection_id` for the Obico handshake |
| `connection.register_remote_method` | Accepted and ignored (Obico registers a remote event callback) |
| `server.webcams.list` | Same format as HTTP, using MoonKobra's own LAN IP |
| `server.history.list` | Job history, same source as `/server/history/list` |
| `machine.update.status` | Stub |
| `server.files.metadata` | Same logic as `/server/files/metadata` |
| `server.temperature_store` | Same payload as `/server/temperature_store` |

Any other method is logged and answered with an empty `result: {}`. It does
not error, so clients probing optional methods do not break.

---

## MoonKobra-specific endpoints

`/kx/...` (and most `/api/...`) responses use `{"result": ...}` on success and
`{"error": "..."}` with a 4xx/5xx status on failure, unless noted.

### Login (`/login`, `/api/...`)

| Method | Path | Purpose | Body |
|---|---|---|---|
| GET | `/login` | Login page | |
| POST | `/api/login` | Sign in; sets the session cookie | `{user, password}` |
| POST | `/api/logout` | Sign out | |
| POST | `/api/auth/change` | Change the password (required after the first login); restarts MoonKobra, the session stays valid | `{password}` (at least 8 characters, not the default) |
| POST | `/api/auth` | Turn login on/off and set user, password and API key; restarts MoonKobra | `{enabled, user, password, api_key}` (an empty password keeps the current one) |
| POST | `/api/auth/camera-token` | New camera-only token (old OBS links stop working); restarts MoonKobra | |

### Printer control (`/api/...`)

| Method | Path | Purpose | Body / Query |
|---|---|---|---|
| POST | `/api/light` | Chamber light on/off | `{on, brightness}` |
| POST | `/api/fan` | Part-cooling fan speed | `{speed}` (0–100) |
| POST | `/api/connect` | (Re)connect the MQTT client manually | — |
| POST | `/api/disconnect` | Disconnect manually | — |
| POST | `/api/restart` | Restart the MoonKobra process | — |
| POST | `/api/speed` | Print speed mode | `{mode}` (int) |
| POST | `/api/axis` | Move an axis, or `{"action":"turnoff"}` to disable the motors | `{axis, move_type, distance}` |
| POST | `/api/temperature` | Nozzle/bed target temperatures | `{nozzle?, bed?}`; uses a different MQTT path while printing and while idle |
| GET | `/api/state` | Full dashboard status snapshot | Main polling endpoint of the web UI |
| GET | `/api/camera` | Current camera stream URL | |
| GET | `/api/camera/stream` | Live MJPEG view | `multipart/x-mixed-replace`, fed by a shared ffmpeg fan-out |
| GET | `/api/camera/h264` | Raw H.264 stream (for Obico) | |
| GET | `/api/camera/snapshot` | Latest cached JPEG frame | Served from RAM |
| POST | `/api/camera/start` / `/api/camera/stop` / `/api/camera/reset` | Camera lifecycle | `reset` clears the 429 backoff and restarts ffmpeg |
| GET | `/api/settings` | Current settings from `config.ini` | |
| POST | `/api/settings` | Save settings and restart | See the fields below |
| POST | `/api/file_ready/clear` | Dismiss the "file ready to print" banner/dialog | |
| GET | `/api/log/stream` | Live log via Server-Sent Events | |
| GET | `/api/log/download` | Download the buffered log as plain text | |
| GET | `/serve/{filename}` | Internal file server that hands the printer a URL to download the G-code from | Not for direct browser use |

**`/api/settings` fields** (POST body, all optional, merged into the existing
`config.ini`): `printer_ip`, `mqtt_port`, `username`, `password`, `mode_id`,
`device_id`, `power_on_url`, `power_off_url`, `power_status_url`,
`default_ams_slot`, `auto_leveling`, `vibration_compensation`,
`camera_on_print`, `web_upload_warning`, `print_start_dialog`,
`poll_interval`, `verbose_http_log`, `printer_name`, `spoolman_server`,
`spoolman_sync_rate`, `ace_dry_presets`.

### AMS / ACE filament control (`/api/...`)

| Method | Path | Purpose | Body |
|---|---|---|---|
| POST | `/api/ams/set_slot` | Set a slot's material type + colour | `{index, type, color:[r,g,b]}` |
| POST | `/api/ams/feed` | Load/unload filament | `{slot_index, type}` (1 = load, 2 = unload) |
| POST | `/api/ace/auto_feed` | Auto-feed on/off for an ACE unit | `{ace_id, on}` |
| POST | `/api/ace/dry` | Start/stop the ACE dryer | `{action: "start"\|"stop", ace_id?, target_temp?, duration?}` |

### G-code store (`/kx/files...`, `/kx/history...`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/kx/files` | Stored files, with status/duration of the last print |
| DELETE | `/kx/files/{file_id}` | Delete a stored file |
| GET | `/kx/files/{file_id}/download` | Download a stored file |
| POST | `/kx/files/{file_id}/verify` | Clear the "web upload, not verified" flag |
| GET | `/kx/files/{id}/objects` | Print object list + SVG preview (for skipping before the print) |
| GET | `/kx/history?limit=&offset=` | Paged print job history |
| GET | `/kx/history/{id}/log` | One print's own log as text; `?download=1` downloads it as `.txt` |

### Files on the printer's own storage (`/kx/printer-files...`)

Different from the store above: these list/manage files kept on the printer's
internal storage (for example, printed straight from Anycubic Slicer Next).

| Method | Path | Purpose |
|---|---|---|
| GET | `/kx/printer-files` | List files through the printer's `file/listLocal` MQTT action |
| POST | `/kx/printer-files/delete` | Delete one or more files: `{"filenames": [...]}` |
| GET | `/kx/printer-files/{filename}/thumbnail` | Fetch (and cache) a file's embedded thumbnail through `file/fileDetails` |

### Printing (`/kx/print`)

| Method | Path | Purpose | Body |
|---|---|---|---|
| POST | `/kx/print` | Start a print from a stored file | `{file_id, filament_assignments?, excluded_objects?, auto_leveling?}` |

`filament_assignments` is `[{slot_index, material, color_hex}, ...]`; when
omitted, all currently loaded AMS slots are mapped automatically.

### Skip objects before/while printing (`/kx/skip...`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/kx/skip` | Skip objects by name while printing: `{"names": [...]}` |
| POST | `/kx/skip/query` | Ask the printer for the object list again and return the merged skip state |
| GET | `/kx/skip/state` | Current skip state (object list, already skipped names, SVG, file name) |

### Filament profiles (`/kx/filament/...`)

| Method | Path | Purpose | Body / Query |
|---|---|---|---|
| GET | `/kx/filament/slots` | Current AMS slot contents + any user profile override | |
| GET | `/kx/filament/profiles?type=&vendor=` | OrcaSlicer filament profile catalogue (system + user-imported) | Optional filters |
| GET | `/kx/filament/profiles/user` | Only the user-imported profiles | |
| POST | `/kx/filament/profiles/user` | Import profiles from a ZIP or `.json` file(s) (multipart) | Field `file`/`files`/`upload`; parsed by `orca_filaments.parse_profile_bytes` |
| DELETE | `/kx/filament/profiles/user?vendor=&name=` | Delete one user profile (both parameters) or all (no parameters) | |
| POST | `/kx/filament/slots/{idx}/profile` | Pin (or clear) a profile override for an AMS slot | `{vendor, name}`; empty strings clear it. The key is `(vendor, name)`, not `id`: IDs are not unique in Orca's catalogue |
| GET/POST | `/kx/filament/visible_vendors` | Read/set the visible-vendor filter of the slot profile list | POST `{"vendors": [...]}`; empty list = show all |

### Spoolman (`/kx/spoolman/...`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/kx/spoolman/status` | Whether Spoolman is configured/reachable, server URL, sync rate, current slot→spool map |
| GET | `/kx/spoolman/spools` | Spool list proxied from the configured Spoolman server |
| POST | `/kx/spoolman/active-spool` | Assign spool IDs to AMS slots: `{"slot_map": {"0": 42, "2": 17}}` |

### Several printers (`/kx/printers...`)

| Method | Path | Purpose | Body |
|---|---|---|---|
| GET | `/kx/printers` | All configured printers with rough online metadata | |
| POST | `/kx/printers/add` | Add a printer by IP (credentials fetched from the printer) | `{printer_ip, name?}`; restarts MoonKobra |
| DELETE | `/kx/printers/{pid}` | Remove a printer; renumbers the remaining `[printer_N]` sections | Restarts MoonKobra |
| POST | `/kx/printers/{pid}/power` | Turn a printer's external smart plug on/off | `{"action": "on"\|"off"}` |
| GET | `/kx/printers/{pid}/power-status` | Current on/off state of the smart plug | |

**The power switch is not the printer's own power state**: it is a plain
`GET` to a user-configured `power_on_url` / `power_off_url` /
`power_status_url` (for example a Tasmota `cmnd=Power%20on` URL). It exists
only for printers with `power_on_url` or `power_off_url` set; `/kx/printers`
exposes this as `has_power_control`.

### Quote (`/kx/pricing/...`), beta

| Method | Path | Purpose | Body |
|---|---|---|---|
| GET | `/kx/pricing/config` | Quote settings (`data/pricing.json`, merged with the defaults) | |
| POST | `/kx/pricing/config` | Save the quote settings (validated and coerced) | the settings object |
| POST | `/kx/pricing/calc` | Price a stored G-code | `{file_id, opt}`; `opt` holds `qty`, `per_plate`, `channel`, `tax`, `coupon`, `shipping`, `extras`, `design_fee`, `hours`, `discount_pct`, `discount_value`, `urgent`, `materials`, `price_kg`, `colours`, `glow`, `exclude`, `total_override` (see `pricing.compute`) |
| GET | `/kx/pricing/quotes` | Saved quotes (newest first) | |
| POST | `/kx/pricing/quotes` | Save a quote | `{file_id, opt, client?, notes?, snapshot?}` (`snapshot` = a `data:image/...` URL) |
| GET | `/kx/pricing/quotes/{id}` | One saved quote, with the stored result | |
| POST | `/kx/pricing/quotes/{id}/delete` | Delete a saved quote | |

### Settings backup (`/kx/backup/...`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/kx/backup/export` | `.zip` with `config.ini`, the quote settings and the imported filament profiles (it contains the printer password) |
| POST | `/kx/backup/import` | Restore that `.zip` (body = the file); every entry is validated before anything is written, then MoonKobra restarts |

### Misc

| Method | Path | Purpose |
|---|---|---|
| GET | `/kx/ui/{name}` | Theme assets (JS/CSS/bundled libraries) and translation files of the active UI theme |
| GET | `/`, `/printer{N}` | The web UI (index.html with inline CSS/JS, for embedded webviews such as OrcaSlicer's Device tab) |
| GET | `/favicon.ico` | Favicon |

---

## Response conventions

- Moonraker-compatible endpoints wrap results as `{"result": {...}}` (or
  `{"error": {"code": ..., "message": ...}}` for the 404 case of
  `/server/database/*`) to match Moonraker.
- `/api/...` and `/kx/...` endpoints usually return `{"result": ...}` on
  success and `{"error": "message"}` with a non-2xx status on failure, but not
  universally. Check the handler in `kobrax_moonraker_bridge.py` if the exact
  shape matters (routes are registered near the end of the file; search for
  `r.add_get(` / `r.add_post(` / `r.add_delete(`).
- Endpoints that write the config (`/api/settings`, `/api/auth`,
  `/api/auth/change`, `/kx/printers/add`, `/kx/printers/{pid}` DELETE,
  `/kx/backup/import`) restart the whole process right after answering, so
  clients should expect a short connection drop.
