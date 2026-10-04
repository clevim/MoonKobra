# NOTICE

MoonKobra is a fork of **KX-Bridge** by viewit, renamed and modified.

This repository contains code licensed under the **GNU General Public License
v3.0** (see [LICENSE](LICENSE)) and material that is **not** covered by that
license. Read this file before forking, distributing, or building from source.

## What is GPLv3-licensed

The original work in this repository:

- The Python sources (`*.py`) — bridge daemon, MQTT client, configuration
  loader, quote module, protocol implementation, the `fetch_credentials`
  utility, `tools/`, `profiles/brasil/` scripts
- `web/` — web UI, themes and translations
- `Dockerfile`, `docker-compose*.yml`, `start.sh`, `moonkobra.spec`
- Documentation files (`README*`, `CHANGELOG*`, `docs/`)

You are free to use, modify and redistribute this code under the terms of
GPLv3 — including for commercial purposes — provided downstream forks remain
under GPLv3 and source is made available to recipients.

## What is **not** GPLv3-licensed

The following items are **third-party material**, used only for
**interoperability purposes** as permitted under §69e UrhG (German Copyright
Act; equivalent: EU Software Directive Art. 6, US fair-use for reverse
engineering for interoperability):

- **TLS client certificate** — MoonKobra ships **no** Anycubic certificate or
  key. It uses the per-device certificate that the user's own printer
  generates and hands over in its LAN handshake, saved locally in
  `config/certs/`. (Older setups that still have `anycubic_slicer.crt` /
  `.key` next to the program keep working as a fallback; those files are
  Anycubic's and are not distributed here.)

- **MQTT protocol structures, payload formats and signature algorithms** —
  reverse-engineered from the Vue project embedded in `libWorkbench.so` of the
  Anycubic Slicer Next application. Any reproduction of code or
  protocol details serves interoperability only.

- **AMS material naming, slot numbering, GCode markers (`EXCLUDE_OBJECT_*`)**
  — follow conventions established by Anycubic and the wider Klipper
  ecosystem; usage here is purely interoperability-driven.

## Third-party code under other licenses

- **Kobra X slicer profiles** (`kobra_x_orcaslicer_preset.zip`) and the
  profile index `data/orca_filaments.json` — derived from the public
  [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer) presets, which are
  licensed under the **GNU AGPL v3.0**; these files stay under AGPL-3.0.
- **Lucide icons** (`web/themes/default/lib/icons.css`) — ISC License, part of
  the icons MIT (Feather). Full text: `web/themes/default/lib/LICENSE-lucide.txt`.
- **Pickr 1.9.1** (`web/themes/default/lib/pickr.min.js`, `pickr-nano.min.css`)
  — MIT License, Copyright (c) 2018 - present Simon Reinisch. Full text:
  `web/themes/default/lib/LICENSE-pickr.txt`.
- **Fonts Archivo and JetBrains Mono** (`web/themes/default/lib/fonts/`) —
  SIL Open Font License 1.1. Full texts: `OFL-Archivo.txt` and
  `OFL-JetBrainsMono.txt` in the same folder.
- **Anycubic error code table** (`ANYCUBIC_ERROR_MESSAGES` in
  `kobrax_moonraker_bridge.py`, and the English `err_<code>` texts in
  `web/translations/en.json`) — taken from
  [stribor/anycubic_kobrax](https://github.com/stribor/anycubic_kobrax),
  MIT License, Copyright (c) 2026 stribor. The MIT license permits inclusion in this
  GPLv3 project; its license text follows:

  > MIT License
  >
  > Copyright (c) 2026 stribor
  >
  > Permission is hereby granted, free of charge, to any person obtaining a copy
  > of this software and associated documentation files (the "Software"), to deal
  > in the Software without restriction, including without limitation the rights
  > to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  > copies of the Software, and to permit persons to whom the Software is
  > furnished to do so, subject to the following conditions:
  >
  > The above copyright notice and this permission notice shall be included in all
  > copies or substantial portions of the Software.
  >
  > THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
  > IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
  > FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
  > AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
  > LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
  > OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
  > SOFTWARE.

## What this means for forks

If you fork MoonKobra:

1. **You must keep this `NOTICE.md` and `LICENSE` file** in your fork.
2. Your modifications and additions to the bridge code, UI, tools and docs
   inherit GPLv3 — they must be made available under the same license if you
   distribute them.
3. The third-party material listed above is **not** something you can
   relicense; it stays under the original (implicit) rights of its owners.
4. Do not commit `config/certs/` or any Anycubic certificate to your fork.

## Disclaimer

This project is independent, non-commercial reverse-engineering work. It is
**not** affiliated with, endorsed by, or supported by Anycubic Technology
Co., Ltd. or any of their subsidiaries.

All trademarks are property of their respective owners.
