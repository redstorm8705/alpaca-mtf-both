# Alpaca fill timestamp precision compatibility — 2026-10-13

**Author:** OpenAI Codex

## Post-live finding

The first production run of the broker-fill Slack notifier delivered 38 normal
trade cards but classified five valid fills as malformed. OCI runs Python
3.10.12. Alpaca supplied those five timestamps with four or five fractional
digits, such as `2026-10-09T14:40:15.6787Z`; Python 3.10's
`datetime.fromisoformat` rejected them. The local Python 3.12 replay accepted the
same values, so the preflight did not expose the compatibility gap.

## Correction

Normalize an ISO timestamp's fractional component to six microsecond digits
before calling `datetime.fromisoformat`. Preserve the original timezone and
reject strings that do not contain an explicit `Z` or numeric UTC offset. Use
the same parser for broker activities and durable state so validation remains
consistent.

The existing state recorded the five payload fingerprints as malformed but did
not acknowledge their activity IDs. After deployment, move the successful poll
cursor back to 2026-10-09 for one recovery run. The 38 acknowledged IDs will
deduplicate; only the five previously missed fills should generate new trade
cards. A second run must deliver zero.

## Gate result required

Board review, cold mechanical review, Groq and Google AI Studio approval,
Python 3.10 compilation and exact-value parsing, focused tests, CI, then the
targeted production replay described above.
