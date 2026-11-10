# QHM-REENTRY-NEWS-1109 build notes (2026-11-09, signed: Claude)

How the build implements the approved design, after the cold review of 11-09 (findings: unknown-hour earnings on the exit day, a full news read every cycle, catch-up tranches after an expired first order). The approved record is unchanged; these notes record where the build is more specific or more conservative.

the "reaching the page cap." bullet and the "The page cap is derived" bullet with:

- **A new read-only function** `fetch_news_since(symbol, start_utc)` in `events/catalyst_engine.py`. It returns `NewsFetch(items, complete, error, pages, end)`; `end` is the window end sent to the API (UTC, whole second).
  - It calls the same Alpaca News endpoint (T1) with the same authentication. The window starts at the start of the exit day in ET (the build does not use the exact fill time; anything earlier on the exit day is read too, which can only keep the lock).
  - QHM reads incrementally: the first read covers the exit-day start to now; each later read starts at the end of the complete read before the last one (see Late-indexed articles below; `reentry_news_through` holds the last end). An incomplete read does not move that point. Any negative headline seen latches `reentry_news_negative` on the hold, and later checks return NEGATIVE without reading. Both are saved in QHM's state and reset when a new QHM exit arms a new lock.
  - It follows `next_page_token` until a page carries the field with a null or empty value. A page without the field is incomplete.
- **When `complete` is False,** so QHM stays locked:
  - any non-200 response, timeout, network error, or unreadable JSON;
  - a page missing the `news` list, or missing the `next_page_token` field;
  - a repeated page token;
  - reaching the page cap;
  - spending the time budget `_NEWS_READ_TIMEOUT_SECONDS` (30 s, plumbing: the read runs on the trading thread), checked before every page request; each request's own timeout is the smaller of 10 s and the budget left.
  - **The page cap** is derived from the stock's own news, re-derived after every page from all pages read so far: those pages cover (now - the oldest article seen); the window needs about window / (that span / pages read) pages; the cap is that estimate times 2. One dense first page therefore cannot set a huge cap. When no article time has been seen yet, the cap cannot be derived and the read is incomplete. In practice, while the pages stay inside the window the cap stays above the pages read; it binds when pages run past the window start, and the time budget is what bounds an endless or slow listing.

- **Earnings on the exit day:** a report dated on the exit day counts as after the exit only when its hour is "amc"; an unknown hour or "bmo" keeps the lock to the next report.
- **Tranche clock:** when a dip-released first order expires with nothing filled, `entry_day` is cleared so later tranches are spaced from the real first fill (no catch-up).
- **Late-indexed articles:** each read starts at the end of the complete read BEFORE the last one (`reentry_news_reread_from`), so consecutive reads overlap by one read's span; an article indexed after a read but timestamped inside it is seen on the next read. (Cold review 11-09, NIT 1.)
