# Third-party notices

## Integration provenance

- Base: [Trught/x2d-printer-monitor](https://github.com/Trught/x2d-printer-monitor),
  commit `5bef57dc80e44c3cd5138ee12e033ad791f85b72`.
- Imported discovery/CLI and JSON catalogs:
  [Trught/bambu-lan-monitor](https://github.com/Trught/bambu-lan-monitor),
  commit `dadf2ba4e14a51eb5a7fdfbb57ceb6e031255f5d`.
  `bambu_monitor.py` is extended locally to share incremental state and TLS policy.
- Query formats and field research checked against
  [Bambu Studio v02.09.02.62](https://github.com/bambulab/BambuStudio/tree/23a33758933e712c6041e1f8f135f7d6b50004aa),
  commit `23a33758933e712c6041e1f8f135f7d6b50004aa` (AGPL-3.0).
  The base project's Bambu Studio table attribution and license discussion in
  README.md continue to apply.

## Bundled catalogs and original LAN monitor mappings

The files `data/hms_errors.json` and `data/wiki_links.json`, the error
formatting/lookup behavior, and the MQTT field interpretations for fan and
packed temperature telemetry derive from the community project
[greghesp/ha-bambulab](https://github.com/greghesp/ha-bambulab), retrieved on
2026-09-01.

MIT License

Copyright (c) 2023 ha-bambulab contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
