#!/usr/bin/env python3
# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
no_static_scan.py — the DYNAMIC-OPTIMAL scanner (remediation Gate 1+2, BGGN 2026-08-25; rebuilt 2026-10-14).

WHY: the MRI redesign shipped hardcoded VIX/TLT/level cutoffs, and on 2026-10-14 the Swing capacity change
shipped a fixed 1.9x overnight cap, a fixed 3% one-share risk and a fixed 2-slot reserve. Name heuristics
("does this variable look like a threshold?") leaked every time (KELLY_FRACTION, stop ATR multiples,
`equity * 1.9`, `len(lots) >= 4`). Rafael 2026-10-14 (standing + permanent): build it dynamic from the start.

RULE (GUILTY BY DEFAULT): in a scanned (risk/sizing/scoring) file, EVERY numeric literal is a violation —
including a negated one (`-0.5`) and one on either side of a comparison — UNLESS it is provably mechanics:
  1. 0, 1 or -1 (identity / sign / index);
  2. a literal passed DIRECTLY to a statistical estimator (quantile/percentile/rolling/ewm/mean/median/std/var/
     pct_change/diff/shift/expanding, or tail(n) only when an estimator follows) that is FED DATA — `np.quantile(vols, 0.9)` passes;
     `np.float64(0.25)`, `np.clip(x, 0.5, 1.9)`, `np.where(vix > 25, ...)` and `derive_stop(atr, 2.5)` do not;
  3. a mechanics call argument: range(), sleep(), round()'s ndigits, int()'s base, string padding (ljust/rjust/zfill/center),
     split()'s maxsplit;
  4. a subscript index or slice bound ONLY on text (`headline[:120]`, `str(ts)[:10]`, `text.split(",")[2]`) —
     `qualified[:3]` caps how many trade, `closes[-21]` / `closes[-20:]` are lookbacks, `ranked[2]` is a pick
     (indexes 0 / 1 / -1 are whitelisted anyway); never a literal inside a filter (`df[df.x > 0.03]`);
  5. a unit conversion OF DATA: `* 100` or `/ 100` when the immediate operand's own name carries a
     pct/percent/bps/frac word (`equity * (risk_pct / 100)`); 60 / 24 / 3600 / 86400 /
     1000 / 1e6 multiplying or dividing a data operand whose OWN name ends in a unit (exact list: `_UNIT_WORDS`, e.g. secs/mins/hours/days/ms/mm:
     `mins * 60`; `60 * 60`, `1000 / price` and `adv < 1e6 * price` are thresholds); a square or square root (`** 2`, `** 0.5`); a 0..100 clamp in either nesting (`min(100, max(0, x))`)
     of a non-quantity value (`min(raw_shares, 100)` and a clamp of `qty` are share caps; `equity / 100` IS a threshold);
  6. a `len(...)` data-sufficiency check on a DATA series (`_DATA_WORDS`: df/bars/closes/...): `len(x) < 2` /
     `<= 2` (only 0, 1 or 2 with < or <=), never on a
     capacity word anywhere inside the len() (lots/positions/slots/holdings/open/entries/trades/orders/held/
     active/book/holds/syms, including a string key such as `state["positions"]`: `len(open_lots) < 2`, `len(self.lots[sym]) < 2` are caps);
  7. a float tolerance: a comparison literal with |x| <= 1e-6;
  8. a comparison whose other operand is plumbing (HTTP status, weekday, a whole-word time name — ttl, timeout,
     cache, epoch, uptime, latency — or a plumbing name), or a
     keyword argument / assignment / default whose name is plumbing; a time/plumbing name never qualifies when
     it also carries a threshold word;
  9. a different-signal PAIRED comparison joined by `and` (`vix > 25 and spy_pct <= -1.0`): both comparisons must hold a real
     literal (|x| < 1e6; a 0 / 1 / -1 counts only against a threshold-named signal such as `spy_pct`) on
     different signals (hour / minute / second of a clock are one signal) — a guard such as `equity > 0`, `qty == 1`, `len(...)` or a plumbing comparison (item 8:
     `_cache_age <= 1800`, `status_code == 200`, `attempt < 3`) is not a signal.
A PLUMBING NAME is one whose underscore-separated words include a plumbing word (retries, attempts, paging,
workers/threads, poll/sleep, timeout, digits/precision, chunk/batch, version, requests/api,
status/code, indent; page_size/batch_size/chunk_size; time units — seconds/minutes/hours/ms/wait — ONLY with an
infrastructure word: request, http, quote, fetch, cache, api, poll, sleep, retry, rate, socket, connect,
read, write) AND none of the threshold
words (pct, mult, risk, cap, limit, thresh, score, weight, stop, target, budget, slot(s), lot(s), reserve,
exposure, leverage, frac/fraction, gross, atr, max, min, ratio, size, k, n, usd, cushion, floor, ceiling,
trigger, band, cutoff, window, lookback, cooldown, hold, held, exit, entry/entries, close, reentry, width, since,
open, trade(s), position(s), elapsed, confirm/confirmation, bar(s)).
Whole words only (snake_case and camelCase split).
A number written as a string and parsed by float() / int() / Decimal() / getenv() / .get() is a literal too.
NO TAG EXEMPTION: a `# PROV:` / `# STATIC-WAIVER:` comment exempts nothing (2026-10-14).

Pure stdlib (ast) so it runs identically in the local hook AND the CI mirror. The list it returns is the
single source of truth for both preship_gate._dynamic_ok (ship-time, on staged bytes, diff-scoped to added
lines) and the PreToolUse early-warning.
"""
import ast
import math
import re

# Statistical ESTIMATORS whose direct numeric parameter (a quantile level, a window length) is an input to a
# data computation. Only these exempt, and only a literal passed DIRECTLY (not nested in arithmetic or a
# comparison): np.clip / np.minimum / np.where / derive_x(atr, 2.5) launder nothing (cold review 2026-10-14).
_ESTIMATORS = {"quantile", "nanquantile", "percentile", "nanpercentile", "rolling", "ewm", "mean", "nanmean",
               "median", "nanmedian", "std", "nanstd", "var", "nanvar", "pct_change", "diff", "shift",
               "expanding"}
# `tail(n)` picks the bottom n rows (`ranked.tail(3)` = 3 shorts); only `x.tail(n).<estimator>()` is a window (round 15)
_MECH_CALLS = {"range", "sleep", "ljust", "rjust", "zfill", "center", "split", "rsplit"}
_WHITELIST = {0, 1, -1}
_UNIT_OPERANDS = {100, 60, 24, 3600, 86400, 1000, 1e6}
_TOLERANCE = 1e-6
_TIME_WORDS = frozenset({"ttl", "timeout", "cache", "epoch", "uptime", "latency"})
# not expiry / deadline: `days_to_expiry <= 2` is an options trade decision (cold review round 7)
_PLUMBING_ATTR = {"status_code", "status", "weekday", "isoweekday", "returncode"}
_PLUMBING_WORDS = frozenset({
    "retry", "retries", "attempt", "attempts", "tries", "page", "pages", "worker", "workers", "thread",
    "threads", "poll", "sleep", "timeout", "digits", "precision", "decimals", "chunk", "batch",
    "version", "req", "requests", "api", "status", "code", "indent",
})
# Time-unit words are plumbing ONLY next to an infrastructure word: `QUOTE_FETCH_TIMEOUT_SECONDS` is mechanics,
# `elapsed_minutes > 90` / `CONFIRM_WAIT_BARS = 3` are trade decisions (cold review round 6).
_TIME_UNIT_WORDS = frozenset({"seconds", "secs", "sec", "ms", "minute", "minutes", "hour", "hours", "wait"})
_INFRA_WORDS = frozenset({"request", "requests", "req", "http", "quote", "quotes", "fetch", "cache", "api",
                          "poll", "sleep", "retry", "retries", "rate", "socket", "connect", "read", "write"})
_THRESHOLD_WORDS = frozenset({
    "pct", "mult", "multiplier", "risk", "cap", "caps", "limit", "limits", "thresh", "threshold", "score",
    "weight", "stop", "target", "budget", "slot", "slots", "lot", "lots", "reserve", "exposure", "leverage",
    "frac", "fraction", "gross", "atr", "max", "min", "ratio", "size", "k", "n", "usd", "cushion", "floor",
    "ceiling", "ceil", "trigger", "band", "cutoff", "window", "lookback", "cooldown", "hold", "exit",
    "entry", "entries", "close", "reentry", "width", "held", "since", "open", "trade", "trades", "position",
    "positions", "elapsed", "confirm", "confirmation", "bars", "bar",
})


def _words(name: str) -> set:
    # split snake_case and camelCase into lower-case words
    return {w.lower() for w in re.split(r"_+|(?<=[a-z0-9])(?=[A-Z])", name or "") if w}


_PLUMBING_SIZES = re.compile(r"(?:^|_)(?:page|batch|chunk)_size(?:_|$)", re.I)  # paging sizes are mechanics


_PCT_WORDS = frozenset({"pct", "percent", "bps", "frac", "fraction"})


def _is_time_name(name: str) -> bool:
    ws = _words(name)
    return bool(ws & _TIME_WORDS) and not (ws & _THRESHOLD_WORDS)


def _is_plumbing_name(name: str) -> bool:
    if _PLUMBING_SIZES.search(name or ""):
        return not ((_words(name) - {"size"}) & _THRESHOLD_WORDS)
    ws = _words(name)
    plumbing = bool(ws & _PLUMBING_WORDS) or bool(ws & _TIME_UNIT_WORDS and ws & _INFRA_WORDS)
    return plumbing and not (ws & _THRESHOLD_WORDS)


class Violation:
    __slots__ = ("line", "col", "name", "literal", "snippet")

    def __init__(self, line, col, name, literal, snippet):
        self.line, self.col, self.name, self.literal, self.snippet = line, col, name, literal, snippet

    def __repr__(self):
        return f"L{self.line}: {self.name} = {self.literal!r}  |  {self.snippet}"


def _is_number(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)


def _num_value(node):
    """The numeric value of a literal or a negated literal (`-0.5`), else None."""
    if _is_number(node):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)) and _is_number(node.operand):
        return -node.operand.value if isinstance(node.op, ast.USub) else node.operand.value
    return None


def _call_name(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        base = _call_name(func.value)
        return f"{base}.{func.attr}" if base else func.attr
    return ""


def _short_call(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _operand_name(op):
    if isinstance(op, ast.Name):
        return op.id
    if isinstance(op, ast.Attribute):
        return op.attr
    if isinstance(op, ast.Call):
        return _short_call(op.func)
    return ""


def _exclude_tree(node, excluded):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            excluded.add(id(sub))     # a parsed numeric string under a plumbing name is plumbing too (round 19)
        if _num_value(sub) is not None:
            excluded.add(id(sub))
            if isinstance(sub, ast.UnaryOp):
                excluded.add(id(sub.operand))


_MODULES = {"np", "numpy", "pd", "pandas", "statistics", "stats", "math"}


def _is_module(node) -> bool:
    return isinstance(node, ast.Name) and node.id in _MODULES


def _has_data(node) -> bool:
    """True when the expression references data (a variable, attribute, call result or subscript), not
    only literals. Module names (np, pd, ...) are not data."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id not in _MODULES:
            return True
        if isinstance(sub, (ast.Subscript, ast.Call)) and sub is not node:
            return True
    return False



# `len(open_lots) < 2` is a 2-slot capacity cap, not a data-sufficiency check (cold review round 8)
_CAPACITY_WORDS = frozenset({"lot", "lots", "position", "positions", "slot", "slots", "holding", "holdings",
                             "open", "entries", "trades", "orders", "held", "active", "book",
                             "holds", "syms"})


# hour / minute / second of one clock are ONE signal: `now.minute >= 45 and now.hour == 15` is a single
# time-of-day cutoff, not a check-and-balance (cold review round 12)
_CLOCK = {"hour": "clock", "minute": "clock", "second": "clock", "time": "clock"}

# a unit conversion needs a UNIT on the data side: `mins * 60`, `hours * 3600`, `elapsed.total_seconds() / 60`,
# `latency_ms / 1000`, `volume_mm * 1e6` (round 16)
_UNIT_WORDS = frozenset({"sec", "secs", "second", "seconds", "ms", "millis", "us", "ns", "mins", "minute",
                         "minutes", "hour", "hours", "hr", "hrs", "days", "epoch", "timestamp", "kb", "mb",
                         "thousands", "millions", "mm"})

# `len(x) < 2` is data sufficiency ONLY on a data series (round 18: allowlist — `len(longs) < 2` is a 2-long cap)
_DATA_WORDS = frozenset({"df", "data", "bars", "bar", "closes", "close", "prices", "highs", "lows", "opens", "volumes",
                         "returns", "rets", "series", "ts", "daily", "intraday", "history", "hist", "values", "vals",
                         "samples", "rows", "frame", "candles", "quotes", "ohlc", "ohlcv", "arr", "points", "obs"})

_PARSE_CALLS = {"float", "int", "Decimal", "getenv", "get", "environ"}

_TEXT_WORDS = frozenset({"text", "msg", "message", "headline", "title", "detail", "details",
                         "date", "iso", "name", "label", "summary", "body", "desc", "description", "reason",
                         "err", "error", "snip", "url", "key", "sha", "hash", "note", "notes", "prompt"})


def _is_text(node: ast.AST) -> bool:
    """True for a string-ish receiver: `str(...)` / f-string / `.strip()` etc., or a name made of text words."""
    if isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return True
    if isinstance(node, ast.Subscript):
        return _is_text(node.value)       # `headline[:120][:5]`, `text.split(",")[2]`
    if isinstance(node, ast.Call):
        f = _short_call(node.func)
        if f in ("str", "repr", "format", "strip", "lstrip", "rstrip", "lower", "upper", "isoformat", "strftime",
                 "join", "decode", "hexdigest", "split", "rsplit", "splitlines", "partition", "rpartition"):
            return True
    ws = _words(_operand_name(node))
    return bool(ws) and ws <= (_TEXT_WORDS | {"self", "s", "str", "raw", "full", "short", "_"}) and bool(ws & _TEXT_WORDS)


def _names_in(node: ast.AST) -> set:
    """Every word of every Name / Attribute anywhere under node (`len(self.lots[sym])` -> self, lots, sym)."""
    out: set = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out |= _words(n.id)
        elif isinstance(n, ast.Attribute):
            out |= _words(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            out |= _words(n.value)       # `len(state["positions"])` (round 10)
    return out


def _plumbing_compare(node: ast.Compare) -> bool:
    """True when a comparison is a plumbing guard: an HTTP status / weekday attribute, or a time or plumbing name."""
    for op in [node.left] + list(node.comparators):
        nm = _operand_name(op)
        if isinstance(op, ast.Attribute) and op.attr in _PLUMBING_ATTR:
            return True
        if isinstance(op, ast.Call) and isinstance(op.func, ast.Attribute) and op.func.attr in _PLUMBING_ATTR:
            return True
        if nm and (_is_time_name(nm) or _is_plumbing_name(nm)):
            return True
    return False

def scan(path: str, source: str) -> "list[Violation]":
    """Return the static-threshold violations in `source` (GUILTY BY DEFAULT — see the module docstring)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []  # unparseable => not this gate's job (py_compile/ruff catch it)
    lines = source.splitlines()
    excluded: set = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            short = _short_call(node.func)
            is_deriv = short in _ESTIMATORS
            # A derivation exempts its numeric parameters only when it is fed DATA (a name, attribute, call or
            # subscript somewhere in its arguments or receiver): `np.quantile(vols, 0.9)` passes, while a
            # literal-only wrapper such as `np.float64(0.25)` or `pd.Series([2.5])` launders nothing.
            fed = isinstance(node.func, ast.Attribute) and _has_data(node.func.value) and not _is_module(node.func.value)
            fed = fed or any(_has_data(a) for a in list(node.args) + [kw.value for kw in node.keywords])
            if (is_deriv and fed) or short in _MECH_CALLS:
                for a in list(node.args) + [kw.value for kw in node.keywords]:
                    if _num_value(a) is not None:      # direct literal only, never a nested expression
                        _exclude_tree(a, excluded)
            if short == "int":   # int(s, 16): the base is mechanics
                for a in list(node.args[1:]) + [kw.value for kw in node.keywords if kw.arg == "base"]:
                    _exclude_tree(a, excluded)
            if short == "round":
                for a in list(node.args[1:]) + [kw.value for kw in node.keywords if kw.arg == "ndigits"]:
                    _exclude_tree(a, excluded)
            for kw in node.keywords:
                if kw.arg and _is_plumbing_name(kw.arg):
                    _exclude_tree(kw.value, excluded)
            # `x.tail(20).mean()` — tail feeding an estimator is a sample window
            if (is_deriv and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Call)
                    and _short_call(node.func.value.func) == "tail"):
                for a in node.func.value.args:
                    if _num_value(a) is not None:
                        _exclude_tree(a, excluded)
            # a 0..100 clamp ONLY: min(100, max(0, x)) / max(0, min(100, x)). `min(raw_shares, 100)` is a
            # 100-share cap (round 15).
            if short in ("min", "max"):
                opp = "max" if short == "min" else "min"
                bound = 100 if short == "min" else 0
                for inner in node.args:
                    if (isinstance(inner, ast.Call) and _short_call(inner.func) == opp
                            and not _names_in(inner) & (_CAPACITY_WORDS | {"shares", "qty", "size", "budget", "dollars", "usd",
                                                            "notional", "risk", "cash", "equity"})
                            and any(_num_value(a) == 100 - bound for a in inner.args)
                            and any(_num_value(a) == bound for a in node.args)):
                        for a in list(node.args) + list(inner.args):
                            if _num_value(a) in (0, 100):
                                _exclude_tree(a, excluded)
        if isinstance(node, ast.Subscript):
            # only a DIRECT index on TEXT (`text.split(",")[2]`) or a text slice (`headline[:120]`);
            # a literal inside a filter expression (`df[df.atr_pct > 0.03]`) is a threshold (cold review round 5)
            sl = node.slice
            if _num_value(sl) is not None and _is_text(node.value):
                _exclude_tree(sl, excluded)   # `closes[-21]` is a lookback, `ranked[2]` a pick (round 14)
            elif isinstance(sl, ast.Slice) and _is_text(node.value):
                # a slice bound is a COUNT — `qualified[:3]` caps entries (round 13). Only a text / timestamp
                # slice (`headline[:120]`, `str(ts)[:10]`) is mechanics.
                for b in (sl.lower, sl.upper, sl.step):
                    if b is not None and _num_value(b) is not None:
                        _exclude_tree(b, excluded)
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Pow) and _num_value(node.right) in (2, 0.5):
                _exclude_tree(node.right, excluded)          # square / square root only (`** 1.5` is a shape choice)
            if isinstance(node.op, (ast.Mult, ast.Div, ast.FloorDiv)):
                for operand, other in ((node.left, node.right), (node.right, node.left)):
                    val = _num_value(operand)
                    if val is None or val not in _UNIT_OPERANDS or not _has_data(other):
                        continue   # `60 * 60` / `24 * 3600` is a literal duration, not a conversion (round 15)
                    if isinstance(node.op, (ast.Div, ast.FloorDiv)) and operand is node.left:
                        continue   # a literal NUMERATOR is a budget: `1000 / price` is $1,000 of shares (round 16)
                    # the IMMEDIATE operand's own name decides, by its LAST word (`mins`, `delta.days`,
                    # `elapsed.total_seconds()`); a compound expression or builtin min/max is not a unit (round 17:
                    # `day_start_equity / 1000` is a dollar threshold, not a day->ms conversion)
                    nm = _operand_name(other)
                    if isinstance(other, ast.Call) and nm in ("min", "max", "abs", "sum", "round"):
                        nm = ""
                    last = nm.lower().replace("total_", "").split("_")[-1] if nm else ""
                    unit_words = _words(nm) if val == 100 else ({last} if last else set())
                    if val == 100:
                        if not unit_words & _PCT_WORDS:
                            continue   # `equity / 100` is a 1% budget, `qty * 100` a lot size
                    elif not unit_words & _UNIT_WORDS:
                        continue   # `adv < 1e6 * price`, `price / 1000` are thresholds, not conversions
                    _exclude_tree(operand, excluded)
        if isinstance(node, ast.Compare):
            operands = [node.left] + list(node.comparators)
            plumbing_peer = _plumbing_compare(node)
            for op in operands:
                v = _num_value(op)
                if v is None:
                    continue
                if plumbing_peer or abs(v) <= _TOLERANCE:
                    _exclude_tree(op, excluded)
            if (isinstance(node.left, ast.Call) and _short_call(node.left.func) == "len"
                    and all(isinstance(o, (ast.Lt, ast.LtE)) for o in node.ops)
                    and _names_in(node.left) & _DATA_WORDS and not _names_in(node.left) & _CAPACITY_WORDS):
                for c in node.comparators:
                    if _num_value(c) in (0, 1, 2):
                        _exclude_tree(c, excluded)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):   # `or` is not a check-and-balance
            # A check-and-balance pairs two REAL thresholds on two different signals. A guard such as
            # `equity > 0` (whitelisted literal) or a |x| >= 1e6 sentinel is not a second signal (cold review 2026-10-14).
            real = []
            for c in (v for v in node.values if isinstance(v, ast.Compare)):
                if _plumbing_compare(c):
                    continue   # a cache-age / status / retry / weekday check is a guard, not a signal (round 8)
                cmp_names = [_operand_name(o) for o in [c.left] + list(c.comparators) if _num_value(o) is None]
                thresholdish = any(_words(n) & _THRESHOLD_WORDS for n in cmp_names if n)
                # 0 / 1 / -1 count as a REAL threshold only against a threshold-named signal (`spy_pct <= -1.0`);
                # against `qty == 1` or `equity > 0` they are guards, not a second signal (cold review round 6)
                lits = [o for o in [c.left] + list(c.comparators)
                        if _num_value(o) is not None and abs(_num_value(o)) < 1e6
                        and (_num_value(o) not in _WHITELIST or thresholdish)]
                names = {_CLOCK.get(_operand_name(o), _operand_name(o)) for o in [c.left] + list(c.comparators)
                         if _num_value(o) is None and _operand_name(o) not in ("", "len")}
                if lits and names:
                    real.append((c, frozenset(names)))
            if len(real) >= 2 and len(set().union(*(n for _, n in real))) >= 2 and \
                    len({n for _, n in real}) >= 2:
                for c, _ in real:
                    _exclude_tree(c, excluded)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            tnames = [_operand_name(t) for t in targets]
            if tnames and all(n and _is_plumbing_name(n) for n in tnames) and node.value is not None:
                _exclude_tree(node.value, excluded)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fargs = node.args
            pos = list(fargs.posonlyargs) + list(fargs.args)
            for arg, default in zip(pos[len(pos) - len(fargs.defaults):], fargs.defaults, strict=True):
                if _is_plumbing_name(arg.arg):
                    _exclude_tree(default, excluded)
            for arg, kdefault in zip(fargs.kwonlyargs, fargs.kw_defaults, strict=True):
                default = kdefault  # type: ignore[assignment]
                if default is not None and _is_plumbing_name(arg.arg):
                    _exclude_tree(default, excluded)

    out = []
    seen = set()
    for node in ast.walk(tree):
        v = _num_value(node)
        if v is None or isinstance(v, bool):
            continue
        if isinstance(node, ast.Constant) and id(node) in seen:
            continue  # operand of a negation already reported via its UnaryOp
        if isinstance(node, ast.UnaryOp):
            seen.add(id(node.operand))
        if id(node) in excluded or v in _WHITELIST:
            continue
        ln, col = getattr(node, "lineno", 0), getattr(node, "col_offset", 0)
        snip = lines[ln - 1].strip() if 1 <= ln <= len(lines) else ""
        out.append(Violation(ln, col, "<literal>", v, snip[:100]))
    # a number written as a string and parsed (`float(os.environ.get("SWING_K", "1.9"))`, `Decimal("0.03")`) is a
    # literal too (round 18)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _short_call(node.func) in _PARSE_CALLS):
            continue
        parsed = list(node.args[:1]) if _short_call(node.func) == "int" else list(node.args)   # int(s, 16): base
        for a in parsed + [kw.value for kw in node.keywords if kw.arg != "base"]:
            if isinstance(a, ast.Constant) and isinstance(a.value, str) and id(a) not in excluded:
                try:
                    sv = float(a.value.strip())
                except ValueError:
                    continue
                if math.isfinite(sv) and sv not in _WHITELIST:   # float("inf") is a sentinel, not a threshold
                    ln = getattr(a, "lineno", 0)
                    snip = lines[ln - 1].strip() if 1 <= ln <= len(lines) else ""
                    out.append(Violation(ln, a.col_offset, "<literal>", sv, snip[:100]))
    uniq, keys = [], set()
    for v in sorted(out, key=lambda x: (x.line, x.col)):
        k = (v.line, v.col)
        if k not in keys:
            keys.add(k)
            uniq.append(v)
    return uniq


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        try:
            src = open(p, encoding="utf-8").read()
        except OSError as e:
            print(f"{p}: unreadable ({e})")
            continue
        vs = scan(p, src)
        print(f"=== {p}: {len(vs)} static-threshold violation(s) ===")
        for v in vs[:60]:
            print(" ", v)
