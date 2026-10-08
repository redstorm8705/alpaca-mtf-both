# no_assume_gate — market-fact absence claims (2026-10-09, Claude)

**Trigger (Rafael 2026-10-09):** the assistant told the CEO "AMD has no liquid inverse ETF" — false. Alpaca
/v2/assets lists DAMD (Defiance 2x Short AMD, tradable, 30% maintenance), which traded 322,212 IEX shares on
2026-10-08 (the bot's own liquidity floor is 50K). The claim came from the bot's hardcoded ETF map
(strategy/day_tier_leverage.py `_DEFAULT_INVERSE` omits AMD): a CONFIG GAP reported as a MARKET FACT. Same message
also said "SNDK has no ETF" (false: SNXX, SNDG, SNDU long; SNDQ 2x short) and called DRAM "never tradeable".

**Why the existing gate missed it:** no_assume_gate.py only matched capability claims about the system ("the bot /
we / there is no <capability>"), and any file path anywhere in the message counted as search evidence.

**Change:** a market-fact absence check — a sentence that says a TICKER has no / lacks an ETF, history, bars, data,
liquidity, volume, borrow or options; or that anything has no / is without an ETF-type product; or that something
is not tradable/shortable — must carry source evidence (Alpaca / IEX / SIP / snapshot / a cited count / a file path)
or a hedge in the SAME sentence or the next one in the same paragraph. Conditional rule phrasing is exempt; common
acronyms (RSI, ATR, API, ET, ...) are not tickers; "bear market"/"bull run" are prose.

**Measured:** on this session's 76 long assistant messages it flags exactly one sentence ("SNDK has no ETF") — the
second false claim. Three cold-2nd rounds (fixes: prose false positives, acronym stoplist, a wrong test).

**Limits:** pattern-based (necessary, not sufficient). Macro acronyms (CPI, FOMC) may block once; a block costs one
redo with a cited source. The structural backstops remain the adversarial pass, the cold-2nd and the board.
