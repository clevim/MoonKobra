<div align="center">

<img src="docs/assets/moonkobra-banner.png" alt="MoonKobra" width="720"/>

**Teach your Kobra to speak Moonraker.**

Control the Anycubic Kobra X from OrcaSlicer and a full web dashboard,
with no Klipper and no Raspberry Pi.

English · [Português (Brasil)](README.pt-BR.md)

<sub>MoonKobra is a fork of KX-Bridge by viewit.</sub>

</div>

---

## What it is

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/moko/ok-dark.png">
  <img src="docs/assets/moko/ok-light.png" alt="Moko giving a thumbs up" width="130" align="right">
</picture>

The Kobra X speaks Anycubic's own LAN protocol (MQTT). Slicers and tools from
the Klipper world speak Moonraker. MoonKobra sits in the middle and translates,
so the printer looks like a regular Moonraker host:

```
OrcaSlicer · Obico · Mobileraker · Home Assistant
            │  HTTP + WebSocket (Moonraker API)
            ▼
       MoonKobra  ── web dashboard on :7125
            │  MQTT over LAN
            ▼
     Kobra X + ACE (4 filament slots)
```

It runs on any machine on the same network (a PC, a NAS, a home server) as a
Docker container, a single binary or plain Python. Nothing is installed on the
printer.

**Meet Moko.** The little cobra is MoonKobra's mascot. On the dashboard, Moko
comments on what the printer is doing: heating, leveling, printing, waiting for
filament, done, or asleep when the printer is off.

---

## Features

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/moko/print-dark.png">
  <img src="docs/assets/moko/print-light.png" alt="Moko watching the nozzle" width="130" align="right">
</picture>

**Printing**
- Start, pause, resume and cancel; temperatures, speed, fans, movement, light.
- "Now" screen: the part in 3D *is* the progress bar (solid up to the current
  layer), plus the print phases the printer reports and temperature trends.
- Stopping a print needs a 1.4 s hold, so it never happens by accident.
- Skip objects, camera stream, Anycubic error codes shown as plain sentences.
- Day, Night and Late-night (red) display modes; works on desktop, phone,
  tablet and inside OrcaSlicer's *Device* tab.

**Filament and ACE**
- ACE slots with a profile picker per slot; material and colour are written
  back to the printer screen.
- Import your own OrcaSlicer filament profiles (ZIP) and match spools tagged
  with third-party RFID tools.
- Multiple daisy-chained ACE units, and the dryer of each unit.
- [Spoolman](https://github.com/Donkie/Spoolman): assign spools to slots,
  usage is tracked as you print.
- Brazilian filament profiles ready to import (see [below](#brazilian-filament-profiles)).

**Files and history**
- G-code browser for uploaded files and for files stored on the printer, with
  thumbnails, search and bulk delete.
- Print history with a separate log for every print, downloadable as text.

**Quote (beta)**
- Pick a G-code and get a price: material, ACE purge, power, machine wear,
  labour, failure reserve, packaging, margin, tax and sales-channel fees
  (Shopee and Mercado Livre two-tier fees). Coupons, quantity discounts and
  parts per plate.
- Saved quotes and a customer receipt as an image (WhatsApp), PDF or text.
  No internal cost or margin appears on the receipt.

**Everything else**
- Several printers in one instance; add a printer with just its IP (the
  credentials are fetched from the printer automatically).
- Print notifications through ntfy, Discord, Telegram or any JSON webhook.
- Login with a mandatory password change on first access, API key for
  slicers, camera-only token for OBS.
- Smart-plug power switch, settings backup and restore.
- UI in Portuguese (Brazil) and English, plus partial German, Spanish,
  French, Italian and Chinese.

---

## Quick start

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/moko/upload-dark.png">
  <img src="docs/assets/moko/upload-light.png" alt="Moko carrying a file" width="130" align="right">
</picture>

**1. Enable LAN mode on the printer.**
Printer screen → Settings → Enable LAN mode.

**2. Start MoonKobra.**
There is no published image yet, so Docker builds it from this folder:

```bash
git clone https://github.com/clevim/MoonKobra.git
cd MoonKobra
./start.sh
```
`docker compose up -d --build` works too.

**3. Open the dashboard** at `http://HOST-IP:7125`.
The first login is `kx` / `kx123`, and you will be asked to choose a new
password right away.

**4. Add the printer.**
In *Printers*, click *Add printer* and type the printer's IP. Username,
password, device ID and the printer's own TLS certificate are read from the
printer automatically. The certificate is saved in `config/certs/`; delete that
folder to fetch it again.

**5. Connect OrcaSlicer.**
Printer → Connection → type **Moonraker**, host `http://HOST-IP:7125` (with
`http://` and the port). Paste the API key from *Settings → API* into the
API key field.

> More than one printer? Add it the same way: each one gets its own port
> (7125, 7126, …).

<details>
<summary><b>Other ways to run it</b></summary>

**Python directly**
```bash
pip install -r requirements.txt
python kobrax_moonraker_bridge.py
```

**Single binary (Linux or Windows)**
```bash
pip install pyinstaller
pyinstaller moonkobra.spec      # result: dist/moonkobra (or moonkobra.exe)
```
`config/` and `data/` are created next to the binary, so copying the folder
moves the whole installation.

**Full stack (Spoolman + self-hosted Obico)**
[`docker-compose.stack.yml`](docker-compose.stack.yml) runs MoonKobra,
Spoolman, Obico and moonraker-obico together. The setup steps are in the
file's header.
</details>

---

## Recommended slicer

Plain OrcaSlicer works for slicing and printing. For filament sync per slot
(the slicer picking *your* brand profile instead of `Generic PLA`), you need an
OrcaSlicer build with these pull requests:

- [#13372](https://github.com/SoftFever/OrcaSlicer/pull/13372): AMS sync keeps
  slot positions even with empty slots.
- [#13719](https://github.com/SoftFever/OrcaSlicer/pull/13719): vendor + name
  matching for Moonraker, by [@LordGuenni](https://github.com/LordGuenni).
- [#13315](https://github.com/SoftFever/OrcaSlicer/pull/13315): unique
  `filament_id` for user presets, by [@mrnoisytiger](https://github.com/mrnoisytiger).

The community build OrcaSlicer-KX already bundles all three.

---

## Brazilian filament profiles

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/moko/level-dark.png">
  <img src="docs/assets/moko/level-light.png" alt="Moko with a spirit level" width="130" align="right">
</picture>

[`profiles/brasil/`](profiles/brasil/) has OrcaSlicer profiles for the
Kobra X (0.4 nozzle) built from what each manufacturer publishes:
**3DFila** (132 colours, one profile per colour, with the official colour and
the store price), **GTMax3D** and **3D Lab**. Import
`filamentos-brasil.zip` in OrcaSlicer or straight into MoonKobra
(*Settings → Filament → Import profiles*). The Quote tab uses the 3DFila
prices automatically. Details in
[profiles/brasil/README.md](profiles/brasil/README.md).

---

## Integrations

- **[Home Assistant](https://github.com/gangoke/kobrax-lan-hass-component)**
  by [@gangoke](https://github.com/gangoke): sensors, controls, light, camera
  and thumbnail as native entities.
- **[Obico](https://github.com/TheSpaghettiDetective/obico-server)** (self-hosted)
  through [moonraker-obico](https://github.com/TheSpaghettiDetective/moonraker-obico):
  time-lapse and WebRTC stream. Failure detection is experimental on the
  Kobra X (the camera looks from the top, not what the model was trained on).
- **Mainsail, Fluidd, Mobileraker**: anything that speaks Moonraker can read
  the printer state and the temperature graph.

These are community projects, not maintained by MoonKobra.

---

## Troubleshooting

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/moko/empty-dark.png">
  <img src="docs/assets/moko/empty-light.png" alt="Moko confused, holding an empty spool" width="130" align="right">
</picture>

<details>
<summary><b>"Wrong MQTT credentials" on start</b></summary>

Add the printer again with *Add printer*, or fetch the credentials by hand and
restart:
```bash
python fetch_credentials.py --ip 192.168.x.x --write-config
```
Type only the IP, without a port (✗ `192.168.1.102:9883`, ✓ `192.168.1.102`).
</details>

<details>
<summary><b>Printer not found</b></summary>

LAN mode must be on (printer screen → Settings), and the printer and
MoonKobra must be on the same network.
</details>

<details>
<summary><b>OrcaSlicer cannot connect</b></summary>

The connection type must be **Moonraker** (not Bambu or Klipper), the host
must include `http://` and `:7125`, and the API key from *Settings → API* must
be filled in.
</details>

<details>
<summary><b>Docker: permission denied</b></summary>

```bash
sudo usermod -aG docker $USER   # then log out and back in
```
</details>

More in the [user manual](docs/en/manual.md). Developers: see the
[API reference](docs/api.md).

---

## Security

- Keep MoonKobra on your local network. **Do not expose port 7125 to the
  internet.**
- `config/config.ini` holds the printer credentials: do not share it.
- These credentials do not give access to any Anycubic cloud service.

---

## Credits and license

[![License: GPL v3](https://img.shields.io/badge/License-GPL_v3-blue.svg)](LICENSE)

MoonKobra is released under the **GNU General Public License v3.0**
([LICENSE](LICENSE)). It started as a fork of
KX-Bridge by viewit and
its contributors; the Anycubic error-code table comes from
[stribor/anycubic_kobrax](https://github.com/stribor/anycubic_kobrax) (MIT).

The MQTT protocol support is the result of independent reverse engineering for
interoperability. MoonKobra ships no Anycubic files: the TLS certificate used
to talk to the printer is generated by the printer itself and handed over on
the local network. Details in [NOTICE.md](NOTICE.md).

MoonKobra is independent and not affiliated with Anycubic.

<div align="center">
<br>
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/moko/sleep-dark.png">
  <img src="docs/assets/moko/sleep-light.png" alt="Moko asleep" width="110">
</picture>
<br>
<sub>Moko is asleep. Go print something.</sub>
</div>
