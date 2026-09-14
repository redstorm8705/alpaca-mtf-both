# Optimizer discovery tool — design record (2026-09-13)

**Feature:** `scripts/optimizer_scan.py` — a READ-ONLY discovery tool that searches Hugging Face +
PyPI + GitHub + arXiv for algorithmic-optimizer FRAMEWORKS relevant to the bot (portfolio
risk-parity, RL reward-shaping, AdamW/optimizer variants for non-stationary financial data, deep-RL
finance), scores/ranks the candidates, and writes a human-reviewable report to
`logs/optimizer_scan_<date>.{json,md}`.

**Origin:** Rafael 2026-09-13 asked for a Hugging Face optimizer scanner-and-stager. Claude declined
the download/stage half on security grounds (arbitrary `.py` runs with the bot's Alpaca/Groq/Gemini
`.env` keys; pickle model weights are RCE-on-load via `torch.load`; staging into the bot bypasses the
mandatory gate) and proposed this read-only, report-only alternative pointed at the sources where
production optimizers actually live (PyPI/GitHub/arXiv, not just HF). Rafael approved: "Yes let's
build it … the broader [multi-source] one."

## Feature Design Protocol — the 5 questions

1. **Data source / tier.** Public research APIs, read-only, no market data: HF REST
   (`huggingface.co/api/models|spaces`), PyPI (`pypi.org/pypi/<pkg>/json`, curated seed list — PyPI's
   search JSON API is deprecated), GitHub (`api.github.com/search/repositories`, unauth OK; optional
   `GITHUB_TOKEN` raises the rate limit), arXiv (`export.arxiv.org/api/query`, Atom XML). NOT one of
   the market-data tiers (T1–T4) — this is code/paper discovery, not price/quote data, so the tier
   rules do not apply. No `huggingface_hub`/`requests` dependency added — Python stdlib only
   (`urllib`, `json`, `xml.etree`), so nothing enters the bot's venv.
2. **Output.** `logs/optimizer_scan_<YYYY-MM-DD>.json` (structured) + `.md` (human report), atomic
   write (tmp→replace). `logs/` only, per the project output rule.
3. **Integration point.** NONE. Standalone research script — NOT imported by the bot, NOT wired into
   any trading path, NOT on the run_cycle thread. Run manually (or an occasional cron) for a report a
   human reviews. No execution/strategy/broker imports.
4. **Failure mode.** Every source is wrapped so a failure drops just that source (never crashes the
   run); HTTP helper retries 429/5xx with backoff and returns empty on give-up; missing fields are
   defaulted; total failure writes an empty report. All fetched text (README/abstract/description) is
   treated as UNTRUSTED DATA — cleaned to a short single-line snippet, summarised, **never executed or
   eval'd**.
5. **Board vote required?** **NO.** Read-only, off the trading thread, zero impact on sizing / scoring
   / entries / exits / orders / risk. Not a risk-path change. It is gated as ordinary new bot-code
   (statics + cold-2nd + Gro/GAI preship + this design record + log-exempt), but needs no risk-path
   board.

## The hard safety line (why this is the SAFE version)

- Downloads **zero** executable artifacts: no `.py`, no model weights (`.bin`/`.pt`/`.safetensors`).
  Fetches only public METADATA + README/abstract TEXT.
- **Stages nothing** into the bot. There is no `/staging/` path, no clone, no install.
- Any candidate that looks worth pursuing is **re-implemented from its vetted source** through the
  normal pipeline: read the source in-session → full-read + board (if it becomes risk-path) + Gro/GAI
  + cold-2nd + a real backtest + Rafael's approval → preship gate. Never cloned into the bot.

## Scoring / ranking

Per-candidate composite = `log1p(popularity)·0.9 + recency·3.0 + relevance·3.0 + source_floor`.
- popularity: HF downloads+likes / GitHub stars+forks / PyPI seeds get a vetted-library floor /
  arXiv 0 (no citation count exposed) → papers rank on recency+relevance.
- recency: ≤90d = 1.0, ≤365d = 0.55, older = 0.25.
- relevance: keyword overlap with an optimizer vocabulary (optimizer, risk parity, kelly, reward,
  adamw, convex, sharpe, …) in name+summary+tags.
- source_floor: +1.2 PyPI (authoritative libs), +0.7 arXiv.
Filter: a zero-traction tutorial/empty repo from HF/GitHub is dropped; PyPI seeds + arXiv are kept.
De-duped by (source,id), grouped by category, top-N reported overall + per category.

## Gate plan

statics (py3.14 + OCI py3.10) → cold-2nd PASS → Gro/GAI preship APPROVE → this design record →
log-exempt (not a logged-issue fix). Then commit + PR + merge. It is a research script (no OCI
service restart; run on demand).
