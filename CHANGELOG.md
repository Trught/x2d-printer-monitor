# Changes

## 2026-11-26

- Reject malformed telemetry before committing merged live state; keep polling after a bad report.
- Stop only raw recording on write, flush or close errors; monitoring continues.
- Bound message-drain batches so bursts yield to the GUI.
- Clear live state and invalidate unfinished switch measurements after disconnects or stale telemetry; retain history.
- Display telemetry age and disable controls when reports are stale.
- Measure switch durations and gaps with a monotonic clock.
- Wait for sufficient hotend information before enabling nozzle controls.
- Freeze a snapshot of chart data, clip the displayed time range and preserve short peaks when reducing long histories.
- Store sampled history in bounded circular buffers; avoid per-sample history copies and unused default lists.
- Update Inspector rows in place to retain selection and avoid rebuilding the entire table.
- Save existing local JSON preferences atomically; keep the original optional plaintext access-code storage without keyring or new GUI dependencies.
- Restore a selected certificate path only for the same printer IP and serial.
- Add offline JSONL replay, regression tests, a synthetic recording and GitHub Actions.
- Document current passive-mode, certificate and bundled HMS behavior.
