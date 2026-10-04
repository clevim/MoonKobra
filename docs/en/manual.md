# MoonKobra user manual

English · [Português (Brasil)](../pt-BR/manual.md)

Day-to-day guide for using MoonKobra once it is installed. To install it, see
the [README](../../README.md). To integrate other software, see the
[API reference](../api.md).

---

## Getting started

### Install and start

Follow the [Quick start](../../README.md#quick-start) in the README. The short
version is:

```bash
./start.sh
```

then open `http://HOST-IP:7125` in a browser.

### First login

The initial login is `kx` / `kx123`. On the first sign-in MoonKobra asks for a
new password, and nothing else works until it is changed. You can change the
user and password later in **Settings → System → Access / Login**, or turn the
login off (not recommended unless the network is yours alone).

### Connect to the printer

1. On the printer screen: **Settings → Enable LAN mode**.
2. In MoonKobra, open **Printers** and click **Add printer**. Type the
   printer's IP and confirm. Username, password, device ID and the TLS
   certificate are read from the printer itself; nothing has to be typed by
   hand.
3. MoonKobra restarts and connects on its own.

The printer's certificate is saved in `config/certs/`. If the connection ever
fails with a certificate error, delete that folder and restart MoonKobra: it
fetches a new one.

### Connect OrcaSlicer

In OrcaSlicer, create the printer with connection type **Moonraker** and host
`http://HOST-IP:7125` (with `http://` and the port). Paste the key from
**Settings → API** into the API key field. For the slicer to recognise the
filament brand of each slot, see the
[Recommended slicer](../../README.md#recommended-slicer) section.

---

## Now (main screen)

The **Now** screen shows the running print at a glance:

- **Progress**: the part in 3D works as the progress bar. Up to the current
  layer it is solid; above it, a hologram. Next to it: percentage, layer, Z
  height, elapsed and remaining time.
  - While printing: **Pause**, **Objects** (skip objects on a plate with
    several parts) and **Hold to stop**. Stopping needs a 1.4 s hold, so it
    never happens by accident.
  - With a file loaded but not started: **Print**, **Assign slots** and
    **Clear**.
- **Trajectory**: the phases the printer reports (check, heating, leveling,
  printing, done).
- **Moko's bulletin**: the mascot comments on the printer state (heating,
  leveling, waiting for filament, done, asleep when the printer is off).
- **Temperatures**: nozzle and bed with current and target values, the trend
  and a history chart. **Set** and **Off** for each.
- **Speed**: Silent, Normal and Sport (the printer's own modes).
- **Fan**: slider and shortcuts from 0 to 100%.
- **Axes**: X/Y and Z moves with 0.1 / 1 / 5 / 10 mm steps, **Home all** and
  **Disable motors**.
- **Camera**: live image, with the **Light** button on the card.
- **Filament**: the ACE slots, each with colour, material and profile. Click a
  slot to edit it (see [Filament](#filament)). The dryer of each ACE unit is
  shown here too.

At the top are the display modes **Day**, **Night** and **Late night** (red,
for a dark room).

When the printer pauses on its own (filament ran out, error), a banner shows
the reason as text, not only the error code.

---

## Printing

### Upload G-code

In **Files → Uploaded**, drag a `.gcode`/`.bgcode` onto the upload area or
click it to pick a file. Files sent from OrcaSlicer show up here too. The list
has thumbnails, search, a filter (All / Completed / Failed / New), sorting
(date, name, duration) and multi-select for bulk delete. The **On printer**
tab lists the files stored on the printer's own memory.

What happens after an upload depends on **Settings → Print → After upload**:
a **print dialog** opens right away, or a **banner** stays at the top of the
screen with the same options.

### Assign filament and start

For files that use several filaments, the assignment dialog opens on its own
(or through **Assign slots**). In it you:

- map each filament of the G-code to an ACE slot, with a warning when the
  slot's material or colour does not match;
- untick objects under **Skip objects** before starting;
- turn auto-leveling on or off for this print;
- pick the Spoolman spool for each slot, if Spoolman is configured.

Confirm with **Print**.

### Print defaults

In **Settings → Print**:

- **Default slot (single colour)**: automatic (all loaded slots) or a fixed
  slot.
- **Auto-leveling** and **Resonance compensation** before printing.
- **After upload**: dialog or banner, as above.
- **Turn the camera on when a print starts**.
- **Warn on web-uploaded prints**: one extra confirmation, to catch a file
  sliced for the wrong printer.
- **Delete the file from the printer after printing**: cleans the printer's
  memory when a print finishes successfully (the file stays in MoonKobra).

---

## Filament

### ACE slots

Click a slot on the **Now** screen to edit:

- **Colour**: colour picker, recent colours or copy another slot's colour.
- **Material**: shortcuts for common materials, or free text.
- **OrcaSlicer profile**: the profile sent to the slicer on sync, instead of
  the generic "Generic PLA".
- **Load / unload** the slot's filament.

The ACE dryer has presets (PLA, PLA+, PETG, TPU, ABS/ASA, PA/PC and three
custom ones with a free name), each with a temperature and a duration. Presets
can be edited and saved.

### OrcaSlicer profiles

In **Settings → Filament → OrcaSlicer profiles**, click **Import profiles** and
send a ZIP of OrcaSlicer's filament folder (**Help → Show configuration folder
→ user/<id>/filament/**) or loose `.json` files. Imported profiles show up in
the "★ My profiles" group of every slot's list. The Brazilian profiles in
`profiles/brasil/filamentos-brasil.zip` are imported the same way.

When you create your own profile in OrcaSlicer to use here, save it as
compatible with the **Anycubic Kobra X 0.4 nozzle** and restart OrcaSlicer once
after saving: only then does it store the profile's `filament_id`, which is
what lets the slot recognise it on sync.

Also in **Settings → Filament**:

- **Profile per slot**: pins a profile to each slot, whatever is loaded.
- **Visible vendors**: limits the vendors in the profile list. "Generic" and
  your imported profiles are always shown.

### Spoolman

In **Settings → Integrations → Spoolman**, enter the server URL (e.g.
`http://spoolman:7912`) and the sync interval in seconds (`0` = only when the
print ends). After that, **Settings → Filament → Spoolman · spool per slot**
links each slot to a spool, and usage is deducted as you print.

---

## Quote (beta)

The **Quote** tab is still in development: check the numbers before sending
them to a customer.

1. **Calculate**: pick a G-code from the list to see the part in 3D, the
   time, the weight and the colour changes. Adjust the order: quantity,
   **parts per plate**, sales channel (direct sale, card, Shopee, Mercado
   Livre), tax, coupon, shipping, extras, modelling, discount and rush. The
   price is drawn as a stack: material, ACE purge, power, machine, labour,
   failure reserve, packaging, shipping, fees, tax and, on top, your profit.
   Costs with a checkbox can be left out of this order only. The total can be
   edited by hand; fees and profit are recalculated on it.
2. **Save** stores the quote with a number (`MK-YYYY-NNNN`). **Receipt**
   creates the customer receipt as an image, PDF or WhatsApp text. The receipt
   never shows internal costs or margin.
3. **Quotes**: the saved list, to reopen, recreate the receipt or delete.
4. **Configure**: printer and power, labour and risk, margin and rounding,
   your business details (printed on the receipt), coupons, materials, sales
   channels, taxes and quantity discounts. Fees change: review them now and
   then (the last review date is shown at the top).

---

## Several printers

- **Add**: in **Printers**, **Add printer** with the IP. Each printer gets its
  own port (7126, 7127, …).
- **Switch**: through the selector in the header or the card in the
  **Printers** tab, which also shows each printer's live state and who is
  connected to it.
- **Remove**: the **✕** on the printer's card, with confirmation.

---

## Power switch

MoonKobra can turn a **smart plug** (for example, running Tasmota) between the
wall and the printer's power supply on and off. In **Settings → Connection →
Power switch**, enter three URLs:

- **Power-on URL**
- **Power-off URL**
- **Status URL** (to show whether it is on)

On Tasmota they usually look like:

```
http://192.168.x.x/cm?cmnd=Power%20on
http://192.168.x.x/cm?cmnd=Power%20off
http://192.168.x.x/cm?cmnd=Power
```

with the plug's IP, not the printer's. The power button appears on the
printer's card in **Printers**. Turning it off asks for confirmation, because
it cuts power to everything on the plug.

---

## Notifications

MoonKobra notifies you when a print **finishes**, **fails** or **pauses with an
error** (filament ran out, clog, heating). A pause you triggered yourself does
not notify. In **Settings → Connection → Notifications**, enter one URL; the
format is picked from the address:

- **ntfy**: `https://ntfy.sh/your-topic` (or your own ntfy server).
- **Discord**: the channel's webhook URL.
- **Telegram**: `https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<ID>`.
- **Any other URL** gets a JSON POST with `printer`, `message`, `state` and
  `filename` (Home Assistant, Node-RED, etc.).

Messages use the UI language that was active when you saved.

---

## Settings

- **Connection**: name, IP, MQTT port, MQTT username and password, Device ID
  and Mode ID (filled in by "Add printer"), power switch and notifications.
- **Print**: the defaults described in [Printing](#printing).
- **Appearance**: language, display mode, printer polling interval, verbose
  HTTP request log and how many print logs to keep.
- **Filament**: OrcaSlicer profiles, profile per slot, visible vendors and
  Spoolman per slot.
- **Integrations**: camera for OBS (camera-only links with their own token),
  Spoolman and Obico.
- **API**: the API key (generate and copy) and a quick guide with examples.
- **System**: login, **settings backup** (a `.zip` with connection, login,
  filaments, Spoolman, Quote settings and imported profiles; keep it safe,
  since it contains the printer password) and the version.

Most changes take effect after **Save and restart**.

---

## Log and troubleshooting

- **Log → Live**: the event log, with filters by direction (RX/TX), level
  (errors/warnings), topic and text, and a **Download** button.
- **Log → History**: every print keeps its own compressed log. Open it to read
  or download it as `.txt`.
- **Certificate / TLS error**: MoonKobra already fetches a new certificate
  when the saved one is rejected. If it still does not connect, delete
  `config/certs/` and restart.
- **"Wrong MQTT credentials"**: add the printer again or see
  [Troubleshooting](../../README.md#troubleshooting) in the README.
- **Printer not found**: LAN mode must be on, and the printer and MoonKobra
  must be on the same network.

---

## Camera and Obico

- **Camera**: the Camera card shows the printer's live image with no setup.
  For OBS, use the links in **Settings → Integrations → Camera for OBS**.
- **Obico**: failure detection and time-lapse run through the
  `moonraker-obico` container, configured in `moonraker-obico.cfg`. The
  [`docker-compose.stack.yml`](../../docker-compose.stack.yml) starts
  everything together.
