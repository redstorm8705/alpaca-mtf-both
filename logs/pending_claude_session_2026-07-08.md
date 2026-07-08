# Pending Decision Package — Build F: HALT & Mass-Liquidation Architecture Redesign
**Session:** Autonomous scheduled session, 2026-07-08 (design-only — nothing shipped, no code changed)
**For:** Rafael — this is a decision package, not a patch. Nothing has been applied to any file.

---

## WHERE THIS CAME FROM

Twice this week the bot's news-reading module misfired and — before today's interim fix — dumped
the entire trading book. The interim fix (already live, commit `9d03be1`) stopped the bleeding: a
news "HALT" now just pauses new trades for a few minutes instead of selling everything. That was
step one. This session did the design work for the REAL fix — a full rebuild of how the bot decides
"something is wrong, stop trading" versus "something is wrong, SELL EVERYTHING RIGHT NOW."

I read the entire relevant source code (1,828 lines of the news-reading module, the 131-line
mass-sell function, and the relevant trading-cycle code), then had my internal review board — 4
independent reviewers, each with a different specialty (risk psychology, trade execution, statistical
rigor, and infrastructure reliability) — plus two external AI auditors (Gro and GAI) independently
analyze the same 4 open design questions. All 6 reviewed it *cold* — none saw each other's answers
before giving their own. Below is where they agree, where they don't, and what you need to decide.

---

## THE PROBLEM, IN ONE EXAMPLE

On the morning of 2026-07-08, a wire service ran a headline: **"Can Trump cut off all trade with
Spain?"** — a QUESTION, not a real announcement. The bot's news-reader has a list of "danger phrases"
that are supposed to catch real national emergencies. One of those phrases — "national emergency" —
also happens to appear constantly in ordinary tariff-policy news coverage, because tariffs are
legally enacted "under a national emergency declaration." The question headline contained that
phrase. The bot's code doesn't distinguish a question from an announcement, or "tariffs enacted
under emergency powers" from "an emergency is happening" — it just checks whether the phrase appears
anywhere in the text. Match found → the bot concluded "national emergency" → in 11 seconds, it
market-sold 6 stocks (MARA, MSTR, HOOD, SNOW, AVGO, MS) for a net loss of about **-$26**. No stock
price had actually crashed. No real circuit breaker had tripped. The bot sold because of a headline,
not because of what the market was actually doing.

We also found the opposite problem hiding in the same code: a real headline — **"Iran revokes oil
export license"** — describing an actual event that could move oil and energy stocks, matched
**nothing** in the bot's danger-phrase list. So the same simple word-matching approach that falsely
triggered on a fake emergency would have completely missed a real one.

---

## THE FOUR DECISIONS

### Decision 1 — Should a news headline EVER be allowed to sell the whole book?

**Board + Gro + GAI: unanimous NO, 6 for 6.** A headline should only ever be allowed to pause new
trades for a bit — never to sell existing positions. The reasoning, in plain terms: a stock price
is money that has actually changed hands — it's real. A headline is just words, and words can be
misread by a simple computer program (as just happened, twice, in the same week, in opposite
directions). The bot should never let "words on a screen" make a decision as final and expensive as
"sell everything right now." Existing stop-loss orders and the bot's price-based emergency-brake
system (which watches actual SPY/QQQ price moves, not headlines) are what should protect the book —
they already exist and already work independently of news.

**Recommendation: adopt as a permanent rule.** This confirms and hardens the fix already shipped today.

### Decision 2 — If a real "sell everything" button should exist, what should press it?

**All 6 agree: never a news headline alone.** There's minor variation in the exact recipe, but every
single reviewer says the trigger must be something REAL that has already happened in the market —
not an interpretation of a headline:
- A real, sharp SPY price drop crossing a specific, predefined level (like the market's own official
  circuit-breaker levels)
- Alpaca (the broker) itself reporting a real trading halt
- Some reviewers want BOTH of those to agree before pulling the trigger, as extra insurance

There's also a piece of leftover, unused code in the file — a "price confirmation" check that was
built once, then silently disconnected during a later rewrite (like a car with a seatbelt that looks
buckled but was never actually attached to anything). **All 4 board reviewers say: don't try to
reconnect that old, unused piece — build a fresh, clearly-named check instead**, because reusing
something that already failed once invisibly is asking for the same silent failure again.

**Recommendation: build a brand-new, explicitly-named "sell everything" trigger based on real price
moves and/or a real broker-reported halt — never news. Retire the old disconnected code rather than
trying to patch it back in.**

### Decision 3 — Can the "danger phrase" list be improved instead of removed?

**All 6 agree: no — retire it entirely for the "sell everything" job.** The reasoning: this week's
two failures (missed a real threat, wrongly triggered on a fake one) aren't two separate small bugs
to patch one at a time — they're the same underlying problem showing up twice. A simple word-matching
list can never be made reliable enough to trust with something as expensive and irreversible as
selling the whole account, no matter how many more phrases you add or how cleverly you phrase-match.
Simple word matching is fine for something low-stakes, like flagging a headline for the dashboard —
it's not fine for something that costs real money and can't be undone.

**Recommendation: keep the danger-phrase list for dashboard display only (already the case for the
"just worth noting" tier) — remove its ability to trigger a sell-everything action.**

### Decision 4 — Any leftover cross-wiring risk before we build this?

The board found one thing worth flagging (not a new emergency, but something to double-check before
building the new system): the bot already has a rule that protects two long-term buy-and-hold
positions (GOOGL and NVDA) from ever being accidentally sold during a mass-sell event. That
protection works by name — it checks "is this GOOGL or NVDA," not "who owns this position." A
different bot strategy ("Movers") that used to run on this same account was shut down last week
after a separate incident, and its old stock positions are sitting there without any special tag.
If a real "sell everything" event ever fires, those old Movers positions will get sold just like
everything else — which is probably exactly what you'd want, but the board's point is: **that should
be an explicit, written-down decision, not something we just assume works correctly because it
happens to work today.** The board's added recommendation: test the new sell-everything code against
that exact scenario (an old, unclaimed position sitting in the account) before it ships, not after.

---

## 3-POINT AI SUMMARY

**Point 1 — Alignment:** All 4 findings above are 3/3 — my internal board, Gro, and GAI all agree,
independently, with no leading from me (each was given the same raw facts and the same 4 questions,
with zero conclusions suggested).

**Point 2 — What Gro/GAI both caught that my board missed:** Nothing — this time it ran the other way.
My internal board (which reasons in more depth per seat) surfaced several extra points neither Gro
nor GAI raised on their own (see Point 3). Nothing in Gro's or GAI's answers pointed at a gap in the
board's analysis.

**Point 3 — New points my board raised that go beyond the 4 questions (informational, feed the eventual
build, not needed for you to decide right now):**
- One reviewer flagged that market-selling 6 stocks all at once, all as instant market orders, is
  itself a minor extra cost regardless of whether the trigger was legitimate — spacing the sells out
  slightly could save a small amount of money on any future real sell-everything event. (Low priority.)
- One reviewer recommends that whatever new trigger exists should also alert you a step BEFORE it
  fires (a "getting close" warning), not just log after the fact. (Worth folding into the eventual
  build — no decision needed from you now.)
- One reviewer suggested that since we'll already be inside this same file making changes, we could
  also fix a small, already-known, separate reliability issue (a background news-checking process
  that can occasionally get stuck) in the same pass rather than reopening the file again later. This
  is a scope question — entirely your call, not urgent.

---

## WHAT I NEED FROM YOU

All 4 decisions above are unanimous across all 6 reviewers — there is no unresolved disagreement to
bring to you. This is not really "pick option A or B" — it's confirmation that the direction is right
before I schedule the actual build:

1. **Confirm:** news headlines should permanently be entries-only (never allowed to sell positions) — YES/NO
2. **Confirm:** build a fresh, real price-move / broker-halt-based "sell everything" trigger, and retire
   the old disconnected code rather than reconnect it — YES/NO
3. **Confirm:** retire the danger-phrase list's ability to trigger a sell-everything action; keep it for
   dashboard display only — YES/NO
4. **Confirm:** before shipping, explicitly test the new trigger against "an old, unclaimed stock
   position sitting in the account" so that behavior is a documented decision, not an assumption — YES/NO
5. **Your call, no urgency:** should the small background news-checking reliability issue be bundled
   into this same build, or handled separately later?

Once you confirm 1-4 (and answer 5), this becomes a fully-scoped, pre-approved patch package that goes
straight to the API-side implementation (per the interactive-vs-API cost protocol) — board + Gro + GAI
will re-review the actual code diff before anything ships, same as every other change.

**Nothing was applied this session. No files were changed except documentation/logs.**
