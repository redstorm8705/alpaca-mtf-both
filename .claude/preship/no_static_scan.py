#!/usr/bin/env python3
# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
no_static_scan.py — the DYNAMIC-OPTIMAL scanner (remediation Gate 1+2, BGGN 2026-08-25).

WHY: the entire MRI redesign shipped on hardcoded static thresholds (TLT<-0.5->10, VIX>20/25/30,
level cutoffs 81/61/41/21) in violation of the PERMANENT no-static-regimes mandate — and the
operator, not the process, caught it. Per DOCUMENTATION-IS-NOT-ENFORCEMENT this scanner is the
mechanism: it finds static numeric thresholds in RISK/SCORING code so the ship gate can REJECT them.

A numeric literal in a scanned file is a VIOLATION when it appears in EITHER:
  - a comparison  (`vix > 25`, `tlt_pct < -0.5`, `score >= 41`), or
  - an assignment / dict-value bound to a threshold-ish name (see _THRESH_RE: *_pts, *_score,
    *thresh*, *cutoff*, *band*, *weight*, *mult*, *scalar*, *floor*, *ceil*, *limit*, *window*,
    *lookback*, *target*, *_pct, *_mad, *_z, *_sigma)
UNLESS it is:
  - WHITELISTED structural (0, 1, -1 and their float forms) — indices/identity/sign, never a
    calibrated decision threshold;
  - on a line carrying `# PROV:<id>` or `# STATIC-WAIVER:<id>` (bound at ship time to a
    provenance/waiver marker by preship_gate — a bare tag is not enough; the marker is the teeth);
  - an ARGUMENT to a derivation call (derive_*/fit_*/calibrate_*/quantile*/percentile*/rolling*/
    ewm*/std*/mean*/median*/np.*/pd.*/statistics.*) — a lookback/quantile passed INTO a data
    computation is a derivation input, not a frozen decision cutoff.

Pure stdlib (ast) so it runs identically in the local hook AND the CI mirror. The list it returns
is the single source of truth for both preship_gate._dynamic_ok (ship-time teeth, on staged bytes)
and the PreToolUse early-warning.
"""
import ast
import re

# Names whose bound literals are decision thresholds (must be derived), not plumbing.
_THRESH_RE = re.compile(
    r"(_pts|_score|thresh|cutoff|band|weight|mult|scalar|floor|ceil|limit|"
    r"window|lookback|target|_pct|_mad|_sigma|_z|zscore|_std|_var|quantile|percentile)",
    re.I,
)
# Calls whose numeric ARGS are derivation inputs (a lookback/quantile), not frozen cutoffs.
_DERIV_CALL_RE = re.compile(
    r"^(derive|fit|calibrate|recompute|quantile|percentile|rolling|ewm|std|var|mean|median|"
    r"nanmean|nanstd|np|numpy|pd|pandas|statistics|stats)([._]|$)",
    re.I,
)
_WHITELIST = {0, 1, -1, 2}  # sign/identity/index + 2 (halving/doubling plumbing); 0.0==0 etc in a set
_TAG_RE = re.compile(r"#\s*(PROV|STATIC-WAIVER):\s*\S+")
# Plumbing operands whose adjacent literal is structural, NOT a risk-decision threshold:
# HTTP status, weekday index, and time/cache/latency plumbing (a TTL/age bound, not a signal cutoff).
_TIME_RE = re.compile(r"(age|elapsed|ttl|_sec|_ms|timeout|cache|_ts|epoch|uptime|latency|expiry|deadline)", re.I)
_PLUMBING_ATTR = {"status_code", "status", "weekday", "isoweekday", "returncode"}


class Violation:
    __slots__ = ("line", "col", "name", "literal", "snippet")

    def __init__(self, line, col, name, literal, snippet):
        self.line, self.col, self.name, self.literal, self.snippet = line, col, name, literal, snippet

    def __repr__(self):
        return f"L{self.line}: {self.name} = {self.literal!r}  |  {self.snippet}"


def _is_number(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)


def _call_name(func):
    # dotted or bare call name for the derivation-call whitelist (np.quantile, rolling(...), derive_x())
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        base = _call_name(func.value)
        return f"{base}.{func.attr}" if base else func.attr
    return ""


def scan(path: str, source: str) -> "list[Violation]":
    """Return the list of static-threshold violations in `source` (already the file's text)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []  # unparseable => not this gate's job (py_compile/ruff catch it)
    lines = source.splitlines()

    # Pre-compute numeric-literal node ids that are STRUCTURAL/derivation, not decision thresholds:
    #  - args to a derivation call (a lookback/quantile passed INTO a computation);
    #  - the 100 in a `* 100` / `/ 100` percentage conversion, or a min()/max() 0..100 score clamp;
    #  - a comparator whose PEER operand is plumbing (HTTP status_code, .weekday(), time/cache/ttl).
    excluded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _DERIV_CALL_RE.match(_call_name(node.func) or ""):
            for a in list(node.args) + [kw.value for kw in node.keywords]:
                for sub in ast.walk(a):
                    if _is_number(sub):
                        excluded.add(id(sub))
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Div)):
            for operand in (node.left, node.right):
                if isinstance(operand, ast.Constant) and operand.value == 100:
                    excluded.add(id(operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("min", "max"):
            for a in node.args:
                if isinstance(a, ast.Constant) and a.value == 100:
                    excluded.add(id(a))  # 0..100 score-domain clamp, not a signal cutoff
        if isinstance(node, ast.Compare):
            peer_plumbing = False
            for op in [node.left] + list(node.comparators):
                if isinstance(op, ast.Attribute) and op.attr in _PLUMBING_ATTR:
                    peer_plumbing = True
                elif isinstance(op, ast.Call) and isinstance(op.func, ast.Attribute) and op.func.attr in _PLUMBING_ATTR:
                    peer_plumbing = True
                else:
                    nm = op.id if isinstance(op, ast.Name) else (op.attr if isinstance(op, ast.Attribute) else "")
                    if nm and _TIME_RE.search(nm):
                        peer_plumbing = True
            if peer_plumbing:
                for c in node.comparators:
                    if _is_number(c):
                        excluded.add(id(c))
        # PAIRING RULE (Rafael 2026-08-28): a static threshold is ALLOWED when its
        # comparison sits in a COMPOUND boolean (and/or) paired with >=1 OTHER comparison
        # on a DIFFERENT signal — a check-and-balance ("vix > 25 and spy_pct <= -1.0"),
        # NOT an isolated single-threshold decision and NOT a same-signal range
        # ("vix > 25 and vix < 30", one signal -> still flagged). Requires >=2 comparison
        # operands referencing >=2 DISTINCT base names, so pairing with a constant/flag
        # (`and True`) or the same signal cannot launder an isolated threshold.
        if isinstance(node, ast.BoolOp):
            _cmps = [v for v in node.values if isinstance(v, ast.Compare)]
            _signals = set()
            for _c in _cmps:
                for _op in [_c.left] + list(_c.comparators):
                    if isinstance(_op, ast.Name):
                        _signals.add(_op.id)
                    elif isinstance(_op, ast.Attribute):
                        _signals.add(_op.attr)
                    elif isinstance(_op, ast.Call):
                        _f = _op.func
                        if isinstance(_f, ast.Attribute):
                            _signals.add(_f.attr)
                        elif isinstance(_f, ast.Name):
                            _signals.add(_f.id)
            if len(_cmps) >= 2 and len(_signals) >= 2:
                for _c in _cmps:
                    for _sub in ast.walk(_c):
                        if _is_number(_sub):
                            excluded.add(id(_sub))

    def tagged(lineno):
        # PROV/WAIVER tag on the literal's own line OR the line immediately above it.
        for ln in (lineno, lineno - 1):
            if 1 <= ln <= len(lines) and _TAG_RE.search(lines[ln - 1]):
                return True
        return False

    def flag(numnode, name):
        if numnode.value in _WHITELIST:
            return None
        if id(numnode) in excluded:
            return None
        if tagged(numnode.lineno):
            return None
        snip = lines[numnode.lineno - 1].strip() if 1 <= numnode.lineno <= len(lines) else ""
        return Violation(numnode.lineno, numnode.col_offset, name, numnode.value, snip[:100])

    out = []
    # 1) comparisons: any numeric comparator is a decision threshold.
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for cmp in node.comparators:
                if _is_number(cmp):
                    v = flag(cmp, "<comparison>")
                    if v:
                        out.append(v)
    # 2) assignments / annotated assignments / dict values bound to a threshold-ish name.
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        names = []
        for t in targets:
            if isinstance(t, ast.Name):
                names.append(t.id)
            elif isinstance(t, ast.Attribute):
                names.append(t.attr)
        if not any(_THRESH_RE.search(n) for n in names):
            continue
        for sub in ast.walk(value):  # covers scalars, tuples, dict values, lists
            if _is_number(sub):
                v = flag(sub, names[0] if names else "<assign>")
                if v:
                    out.append(v)
    # de-dup by (line, col)
    seen, uniq = set(), []
    for v in out:
        k = (v.line, v.col)
        if k not in seen:
            seen.add(k)
            uniq.append(v)
    return sorted(uniq, key=lambda v: (v.line, v.col))


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
