#!/usr/bin/env python3
# ruff: noqa: E501  — long query/keyword literals + report strings are intentionally long
"""scripts/optimizer_scan.py — READ-ONLY "net-new optimizer" discovery tool.

Searches Hugging Face + PyPI + GitHub + arXiv for optimizer FRAMEWORKS relevant to the bot
(portfolio risk-parity, RL reward-shaping, AdamW/optimizer variants for non-stationary financial
data, deep-RL-finance), scores/ranks the candidates, and writes a human-reviewable REPORT to
`logs/optimizer_scan_<date>.{json,md}`.

WHAT IT DELIBERATELY DOES NOT DO (safety — Rafael + Claude 2026-09-13):
  - Downloads NO executable code (.py) and NO model weights (.bin/.pt/.safetensors). A pickle weight
    file is arbitrary-code-execution on load (`torch.load`); an arbitrary .py runs with this bot's
    .env (Alpaca/Groq/Gemini keys). So this tool fetches ONLY public metadata + README/abstract TEXT,
    treats that text as untrusted DATA (summarised, never executed/eval'd), and STAGES NOTHING into
    the bot. Any candidate that looks real is then re-implemented from its vetted source through the
    normal gate (full-read + board + Gro/GAI + cold-2nd + backtest + human approval) — never cloned.

DEPENDENCIES: Python stdlib only (urllib, json, xml.etree). No huggingface_hub, no requests — nothing
is added to the bot's venv. Runs anywhere with network. OCI py3.10 compatible.

OUTPUT: logs/ only (per the project output rule). Read-only; no execution imports; not on the trading
thread. Optional: set GITHUB_TOKEN in the environment to raise the GitHub rate limit (unauth works).

Design record: logs/design_records/optimizer_scan_discovery_2026-09-13.md
Usage: python3 scripts/optimizer_scan.py [--limit N] [--sources hf,pypi,github,arxiv]
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_LOGS = _ROOT / "logs"
_UA = "Mozilla/5.0 (optimizer-scan; research; +https://github.com/) Python-urllib"
_TIMEOUT = 25.0
_RETRIES = 3

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("optimizer_scan")

# ── Category → search queries ───────────────────────────────────────────────────────────────────
# Each category maps to the algorithmic-optimizer class we care about (NOT indicators/asset signals).
_QUERIES: dict[str, list[str]] = {
    "risk_parity":       ["risk parity optimizer", "hierarchical risk parity", "portfolio optimization convex"],
    "rl_finance":        ["reinforcement learning trading", "deep reinforcement learning finance", "reward shaping trading"],
    "optimizer_variant": ["adamw variant", "non-stationary optimizer", "pytorch optimizer financial", "adaptive optimizer time series"],
    "portfolio_opt":     ["mean variance optimization", "black litterman", "kelly criterion sizing", "portfolio allocation optimizer"],
    "multi_agent":       ["multi agent consensus trading", "ensemble trading agents"],
}

# PyPI has no stable search JSON API — seed the KNOWN, vetted optimizer libraries and read each's
# metadata (this is where production optimizers actually live; that is the whole point of the scan).
_PYPI_SEEDS: dict[str, str] = {
    "riskfolio_lib": "risk_parity", "PyPortfolioOpt": "portfolio_opt", "cvxpy": "portfolio_opt",
    "cvxportfolio": "portfolio_opt", "skfolio": "portfolio_opt", "scipy": "portfolio_opt",
    "stable-baselines3": "rl_finance", "sb3-contrib": "rl_finance", "finrl": "rl_finance",
    "qlib": "rl_finance", "torch-optimizer": "optimizer_variant", "pytorch-optimizer": "optimizer_variant",
    "ranger21": "optimizer_variant", "empyrical": "portfolio_opt", "vectorbt": "portfolio_opt",
}

# Tokens that flag a generic tutorial / toy / empty repo (down-rank, do not necessarily drop).
_TUTORIAL_TOKENS = ("tutorial", "example", "demo", "learn ", "beginner", "toy ", "my first", "homework",
                    "assignment", "course", "playground", "test repo", "hello world", "practice")


# ── HTTP helpers (stdlib, robust) ────────────────────────────────────────────────────────────────
def _http(url: str, headers: dict | None = None, timeout: float = _TIMEOUT) -> tuple[int, bytes]:
    """GET url -> (status, body). Retries on 429/5xx/transient errors with backoff. Never raises —
    returns (status, b'') on give-up so a dead source drops out instead of crashing the run."""
    hdrs = {"User-Agent": _UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    for attempt in range(_RETRIES):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return int(getattr(r, "status", 200) or 200), r.read()
        except urllib.error.HTTPError as e:
            code = int(getattr(e, "code", 0) or 0)
            if code in (429, 500, 502, 503, 504) and attempt < _RETRIES - 1:
                _wait = 2 ** attempt * 3
                logger.warning("HTTP %s on %s — backoff %ss", code, url.split("?")[0], _wait)
                time.sleep(_wait)
                continue
            logger.warning("HTTP %s (giving up) on %s", code, url.split("?")[0])
            return code, b""
        except Exception as e:
            if attempt < _RETRIES - 1:
                time.sleep(2 ** attempt * 2)
                continue
            logger.warning("request failed (giving up) on %s: %s", url.split("?")[0], e)
            return 0, b""
    return 0, b""


def _get_json(url: str, headers: dict | None = None):
    status, body = _http(url, headers)
    if status != 200 or not body:
        return None
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except Exception as e:
        logger.debug("json parse failed on %s: %s", url.split("?")[0], e)
        return None


def _clean(text, n: int = 320) -> str:
    """Untrusted text -> a short, single-line, safe summary snippet. Never executed/eval'd."""
    s = " ".join(str(text or "").split())
    return s[:n]


def _days_since(iso_ts) -> "int | None":
    """Whole days since an ISO-8601 timestamp; None if unparseable."""
    if not iso_ts:
        return None
    s = str(iso_ts).replace("Z", "+00:00")
    for parse in (s, s[:19] + "+00:00", s[:10]):
        try:
            dt = datetime.fromisoformat(parse)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0, int((datetime.now(timezone.utc) - dt).total_seconds() // 86400))
        except Exception:
            continue
    return None


# ── Source fetchers (each fail-safe: returns [] on any failure) ───────────────────────────────────
def search_huggingface(limit_per_query: int = 8) -> list[dict]:
    out: list[dict] = []
    for cat, queries in _QUERIES.items():
        for q in queries:
            for kind in ("models", "spaces"):
                url = f"https://huggingface.co/api/{kind}?search={urllib.parse.quote(q)}&limit={limit_per_query}&full=false"
                data = _get_json(url)
                if not isinstance(data, list):
                    continue
                for it in data:
                    if not isinstance(it, dict):
                        continue
                    rid = it.get("id") or it.get("modelId") or ""
                    if not rid:
                        continue
                    _pref = "spaces/" if kind == "spaces" else ""
                    out.append({
                        "source": f"huggingface:{kind[:-1]}", "id": rid,
                        "url": f"https://huggingface.co/{_pref}{rid}",
                        "name": rid.split("/")[-1], "category": cat,
                        "popularity": int(it.get("downloads") or 0) + int(it.get("likes") or 0) * 5,
                        "recency_days": _days_since(it.get("lastModified")),
                        "summary": _clean(" ".join(it.get("tags", [])) if isinstance(it.get("tags"), list) else ""),
                        "tags": it.get("tags", []) if isinstance(it.get("tags"), list) else [],
                        "_query": q,
                    })
    return out


def fetch_pypi() -> list[dict]:
    out: list[dict] = []
    for pkg, cat in _PYPI_SEEDS.items():
        data = _get_json(f"https://pypi.org/pypi/{urllib.parse.quote(pkg)}/json")
        if not isinstance(data, dict):
            continue
        info = data.get("info", {}) if isinstance(data.get("info"), dict) else {}
        # last release date = newest upload across releases
        recency = None
        rels = data.get("releases", {})
        if isinstance(rels, dict):
            uploads = [f.get("upload_time_iso_8601") for v in rels.values() if isinstance(v, list) for f in v if isinstance(f, dict)]
            days = [d for d in (_days_since(u) for u in uploads if u) if d is not None]
            recency = min(days) if days else None
        out.append({
            "source": "pypi", "id": pkg, "url": info.get("project_url") or f"https://pypi.org/project/{pkg}/",
            "name": pkg, "category": cat,
            # PyPI exposes no download count in this API; use a fixed "vetted-library" popularity floor
            # so the seed libraries rank on recency + relevance (they are authoritative by inclusion).
            "popularity": 5000,
            "recency_days": recency,
            "summary": _clean(info.get("summary") or ""),
            "tags": [k for k in (info.get("keywords") or "").replace(",", " ").split() if k][:8],
            "_query": "pypi-seed",
        })
    return out


def search_github(limit_per_query: int = 6) -> list[dict]:
    out: list[dict] = []
    token = os.getenv("GITHUB_TOKEN", "").strip()
    hdrs = {"Accept": "application/vnd.github+json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    for cat, queries in _QUERIES.items():
        for q in queries:
            url = f"https://api.github.com/search/repositories?q={urllib.parse.quote(q)}&sort=stars&order=desc&per_page={limit_per_query}"
            data = _get_json(url, headers=hdrs)
            if not isinstance(data, dict):
                continue
            for it in (data.get("items") or []):
                if not isinstance(it, dict):
                    continue
                out.append({
                    "source": "github", "id": it.get("full_name") or "",
                    "url": it.get("html_url") or "", "name": it.get("name") or "", "category": cat,
                    "popularity": int(it.get("stargazers_count") or 0) + int(it.get("forks_count") or 0),
                    "recency_days": _days_since(it.get("pushed_at") or it.get("updated_at")),
                    "summary": _clean(it.get("description") or ""),
                    "tags": it.get("topics", []) if isinstance(it.get("topics"), list) else [],
                    "_query": q,
                })
            if not token:
                time.sleep(6.5)   # unauth GitHub search: ~10 req/min — stay under it
    return out


def search_arxiv(limit_per_query: int = 5) -> list[dict]:
    out: list[dict] = []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    aq = {
        "risk_parity": "risk parity portfolio optimization",
        "rl_finance": "reinforcement learning trading reward",
        "optimizer_variant": "adaptive optimizer non-stationary deep learning",
        "portfolio_opt": "portfolio optimization machine learning",
    }
    for cat, q in aq.items():
        url = ("http://export.arxiv.org/api/query?search_query=" + urllib.parse.quote(f"all:{q}") +
               f"&start=0&max_results={limit_per_query}&sortBy=submittedDate&sortOrder=descending")
        status, body = _http(url, headers={"Accept": "application/atom+xml"})
        if status != 200 or not body:
            continue
        try:
            root = ET.fromstring(body)
        except Exception as e:
            logger.debug("arxiv XML parse failed: %s", e)
            continue
        for entry in root.findall("a:entry", ns):
            title = _clean((entry.findtext("a:title", default="", namespaces=ns) or ""), 200)
            summ = _clean((entry.findtext("a:summary", default="", namespaces=ns) or ""), 320)
            link = entry.findtext("a:id", default="", namespaces=ns) or ""
            pub = entry.findtext("a:published", default="", namespaces=ns) or ""
            if not title:
                continue
            out.append({
                "source": "arxiv", "id": link.split("/")[-1], "url": link, "name": title, "category": cat,
                "popularity": 0,   # arXiv exposes no citation count here; papers rank on recency + relevance
                "recency_days": _days_since(pub), "summary": summ, "tags": [], "_query": q,
            })
        time.sleep(3.0)   # arXiv asks for ~1 req / 3s
    return out


# ── Scoring + filtering ──────────────────────────────────────────────────────────────────────────
def _relevance(c: dict) -> int:
    """Keyword-overlap relevance in name+summary+tags against the optimizer vocabulary."""
    vocab = ("optimizer", "optimization", "risk parity", "portfolio", "kelly", "reinforcement",
             "reward", "adamw", "adam", "convex", "allocation", "sharpe", "drawdown", "black litterman",
             "mean variance", "non-stationary", "agent", "sizing", "cvxpy")
    hay = (str(c.get("name", "")) + " " + str(c.get("summary", "")) + " " + " ".join(map(str, c.get("tags", [])))).lower()
    return sum(1 for v in vocab if v in hay)


def _is_generic(c: dict) -> bool:
    hay = (str(c.get("name", "")) + " " + str(c.get("summary", ""))).lower()
    tutoriali = any(t in hay for t in _TUTORIAL_TOKENS)
    empty = not c.get("summary") and not c.get("tags")
    # a zero-traction tutorial/empty from HF/GitHub is noise; PyPI seeds + arXiv are kept (curated).
    weak = c.get("source", "").startswith(("huggingface", "github")) and (c.get("popularity") or 0) < 3
    return (tutoriali and weak) or (empty and weak)


def _score(c: dict) -> float:
    pop = math.log1p(max(0, c.get("popularity") or 0))                     # 0..~10, log-damped
    rd = c.get("recency_days")
    rec = 1.0 if (rd is not None and rd <= 90) else 0.55 if (rd is not None and rd <= 365) else 0.25 if rd is not None else 0.4
    rel = min(_relevance(c), 6) / 6.0                                       # 0..1
    src_bonus = {"pypi": 1.2, "arxiv": 0.7}.get(c.get("source", ""), 0.0)   # vetted libs / papers get a floor
    return round(pop * 0.9 + rec * 3.0 + rel * 3.0 + src_bonus, 3)


def _why(c: dict) -> str:
    bits = []
    if (c.get("popularity") or 0) > 0 and not c.get("source", "").startswith(("pypi", "arxiv")):
        bits.append(f"{c['popularity']} stars/downloads")
    if c.get("source") == "pypi":
        bits.append("vetted PyPI library (authoritative source)")
    if c.get("source") == "arxiv":
        bits.append("recent paper (method, not code)")
    rd = c.get("recency_days")
    if rd is not None:
        bits.append(f"updated {rd}d ago")
    r = _relevance(c)
    if r >= 3:
        bits.append(f"strong optimizer-vocab match ({r})")
    return "; ".join(bits) or "candidate"


# ── Report ───────────────────────────────────────────────────────────────────────────────────────
def build_report(cands: list[dict], top_n: int) -> tuple[dict, str]:
    for c in cands:
        c["score"] = _score(c)
        c["why"] = _why(c)
    kept = [c for c in cands if not _is_generic(c)]
    # de-dupe by (source,id), keep the highest score
    seen: dict[tuple, dict] = {}
    for c in sorted(kept, key=lambda x: x["score"], reverse=True):
        k = (c["source"], c["id"])
        if k not in seen:
            seen[k] = c
    ranked = sorted(seen.values(), key=lambda x: x["score"], reverse=True)

    by_cat: dict[str, list[dict]] = {}
    for c in ranked:
        by_cat.setdefault(c["category"], []).append(c)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "scanned": len(cands), "kept": len(ranked), "dropped_generic": len(cands) - len(kept),
        "note": "READ-ONLY discovery. No code/weights downloaded or staged. Re-implement any pick through the full gate.",
        "top": ranked[:top_n],
        "by_category": {k: v[:top_n] for k, v in by_cat.items()},
    }

    lines = [f"# Optimizer discovery scan — {today}",
             f"_Read-only. {len(ranked)} candidates kept of {len(cands)} scanned "
             f"({payload['dropped_generic']} generic/empty dropped). Nothing downloaded or staged — "
             f"any pick is re-implemented from its vetted source through the full gate._", ""]
    lines.append(f"## Top {top_n} overall")
    lines.append("| # | Source | Candidate | Cat | Pop | Age(d) | Score | Why | Link |")
    lines.append("|---|--------|-----------|-----|-----|--------|-------|-----|------|")
    for i, c in enumerate(ranked[:top_n], 1):
        lines.append(f"| {i} | {c['source']} | {c['name'][:40]} | {c['category']} | {c.get('popularity',0)} | "
                     f"{c.get('recency_days','?')} | {c['score']} | {c['why'][:60]} | {c['url']} |")
    lines.append("")
    for cat, items in by_cat.items():
        lines.append(f"## {cat} — top {min(top_n, len(items))}")
        for c in items[:top_n]:
            lines.append(f"- **{c['name']}** ({c['source']}, score {c['score']}) — {c['summary'][:160] or '(no summary)'}  \n  <{c['url']}>  · {c['why']}")
        lines.append("")
    lines.append("---")
    lines.append("_Next step for any candidate: read its source in-session, then re-implement the algorithm "
                 "through full-read + board + Gro/GAI + cold-2nd + a backtest + approval. No cloning, no staging._")
    return payload, "\n".join(lines)


def main() -> int:
    args = sys.argv[1:]
    top_n = 5
    if "--limit" in args:
        try:
            top_n = int(args[args.index("--limit") + 1])
        except Exception:
            pass
    want = {"hf", "pypi", "github", "arxiv"}
    if "--sources" in args:
        try:
            want = set(args[args.index("--sources") + 1].split(","))
        except Exception:
            pass

    logger.info("optimizer_scan starting — sources=%s top_n=%d", sorted(want), top_n)
    cands: list[dict] = []
    _sources = [
        ("hf", "HF", search_huggingface),
        ("pypi", "PyPI", fetch_pypi),
        ("github", "GitHub", search_github),
        ("arxiv", "arXiv", search_arxiv),
    ]
    for _key, _label, _fn in _sources:
        if _key not in want:
            continue
        try:
            _res = _fn()
            logger.info("%s: %d", _label, len(_res))
            cands += _res
        except Exception as e:
            logger.warning("%s source failed: %s", _label, e)

    if not cands:
        logger.warning("no candidates fetched (all sources empty/unreachable) — writing empty report.")
    payload, md = build_report(cands, top_n)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _LOGS.mkdir(parents=True, exist_ok=True)
    jpath = _LOGS / f"optimizer_scan_{today}.json"
    mpath = _LOGS / f"optimizer_scan_{today}.md"
    for path, content in ((jpath, json.dumps(payload, indent=2)), (mpath, md)):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)   # atomic
    logger.info("optimizer_scan done — %d kept -> %s / %s", payload["kept"], jpath.name, mpath.name)
    print(f"\nOptimizer scan: {payload['kept']} candidates kept of {payload['scanned']} scanned.")
    print(f"Report: logs/{jpath.name}  +  logs/{mpath.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
