# Canonical fill-notification state timestamps — 2026-10-13

**Author:** OpenAI Codex

## Post-live finding

The targeted timestamp-precision recovery delivered exactly the five previously
missed BUY/SELL cards and increased acknowledged fill IDs from 38 to 43. The
immediate verification run delivered zero duplicates. It also showed that
variable-precision timestamps already stored in `seen` are parsed on every poll,
causing repeated normalization INFO lines even though no trade card repeats.

## Decision

- Log variable-precision normalization only while rendering an unseen broker
  fill, where it is useful execution evidence.
- Store every new acknowledgment timestamp in canonical timezone-aware ISO
  microsecond form.
- Canonicalize legacy `seen.ts` values during state load and persist them through
  the poller's existing atomic state write. Emit one structured migration log
  with the count changed.

No activity ID, payload hash, cursor, or delivery acknowledgment changes. The
migration cannot add or remove a seen ID and therefore cannot suppress or
duplicate a Slack trade notification.
