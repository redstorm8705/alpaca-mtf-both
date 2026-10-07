"""Regression coverage for phone-readable nightly-audit findings."""

from scripts.audit_slack import _finding_line, findings_from_report


REPORT = (
    "### NEW BUGS FOUND\n"
    "| CATEGORY | SEVERITY | FILE | DESCRIPTION | EXACT FAILURE CONDITION |\n"
    "| :--- | :--- | :--- | :--- | :--- |\n"
    "| EXECUTION BUG | HIGH | `day_tier_track_m.py` | Friday Close Fetch | "
    "`fetch_bars_window` includes Monday. |\n"
    "| INFRASTRUCTURE | LOW | `day_tier_logger.py` | Empty file append | "
    "Empty-file handling fails. |\n\n"
    "### FIX VALIDATION: N/A\n"
)


def test_markdown_table_rows_become_distinct_findings():
    findings = findings_from_report(REPORT)

    assert len(findings) == 2
    assert findings[0] == {
        "severity": "high",
        "title": "Friday Close Fetch",
        "detail": "`fetch_bars_window` includes Monday.",
        "file": "day_tier_track_m.py",
    }
    assert findings[1]["severity"] == "low"


def test_rendered_finding_omits_markdown_table_scaffolding():
    rendered = _finding_line(findings_from_report(REPORT)[0])

    assert rendered == (
        "*Friday Close Fetch* · `day_tier_track_m.py`\n"
        "`fetch_bars_window` includes Monday."
    )
    assert "CATEGORY" not in rendered
    assert ":---" not in rendered
    assert "|" not in rendered


def test_table_without_outer_pipes_keeps_rows_separate():
    report = (
        "### NEW BUGS FOUND\n"
        "CATEGORY | SEVERITY | FILE | DESCRIPTION | EXACT FAILURE CONDITION\n"
        ":--- | :--- | :--- | :--- | :---\n"
        "EXECUTION BUG | HIGH | `day_tier_track_m.py` | Friday Close Fetch | "
        "`fetch_bars_window` includes Monday.\n"
        "INFRASTRUCTURE | LOW | `day_tier_logger.py` | Empty file append | "
        "Empty-file handling fails.\n\n"
        "### FIX VALIDATION: N/A\n"
    )

    findings = findings_from_report(report)

    assert [finding["title"] for finding in findings] == [
        "Friday Close Fetch",
        "Empty file append",
    ]


def test_pipe_inside_inline_code_does_not_split_a_cell():
    report = REPORT.replace(
        "`fetch_bars_window` includes Monday.",
        "`left | right` remains one detail cell.",
    )

    findings = findings_from_report(report)

    assert len(findings) == 2
    assert findings[0]["detail"] == "`left | right` remains one detail cell."


def test_pipe_in_prose_finding_is_not_mistaken_for_a_table():
    report = """### NEW BUGS FOUND
- Stop protection is invalid when owner quantity | broker quantity diverges

### FIX VALIDATION: N/A
"""

    findings = findings_from_report(report)

    assert len(findings) == 1
    assert findings[0]["title"].startswith("Stop protection is invalid")


def test_three_cell_catastrophic_contract_remains_visible():
    report = """### CATASTROPHIC ALERT: 1
execution/day_trade_manager.py | protective-stop cancel failed | NVDA
### NEW BUGS FOUND
None — no new bugs.
"""

    findings = findings_from_report(report)

    assert len(findings) == 1
    assert findings[0]["severity"] == "critical"
    assert findings[0]["title"] == "execution/day_trade_manager.py"
    assert "protective-stop cancel failed" in findings[0]["detail"]


def test_many_pipes_in_prose_do_not_activate_table_mode():
    report = """### NEW BUGS FOUND
- Recovery failed: order_id | client_order_id | symbol | qty | stop_id | broker status

### FIX VALIDATION: N/A
"""

    findings = findings_from_report(report)

    assert len(findings) == 1
    assert findings[0]["title"].startswith("Recovery failed")


def test_prose_after_table_exits_table_mode():
    report = """### NEW BUGS FOUND
CATEGORY | SEVERITY | FILE | DESCRIPTION | CONDITION
:--- | :--- | :--- | :--- | :---
EXECUTION BUG | HIGH | x.py | Stop issue | bad state
Additional context without a blank separator
- Real failure: id | coid | symbol | qty | stop | broker state

### FIX VALIDATION: N/A
"""

    findings = findings_from_report(report)

    assert [finding["title"] for finding in findings[:2]] == [
        "Stop issue",
        "Additional context without a blank separator",
    ]
    assert findings[2]["title"] == "Real failure: id"
    assert "broker state" in findings[2]["detail"]


def test_short_pipe_prose_after_table_exits_table_mode():
    report = """### NEW BUGS FOUND
CATEGORY | SEVERITY | FILE | DESCRIPTION | CONDITION
:--- | :--- | :--- | :--- | :---
EXECUTION | HIGH | x.py | Stop issue | bad state
context | still prose
Immediate failure: id | coid | symbol | qty | stop | broker state

### FIX VALIDATION: N/A
"""

    findings = findings_from_report(report)

    assert findings[-1]["title"] == "Immediate failure: id"
    assert "broker state" in findings[-1]["detail"]


def test_failure_prose_directly_after_table_is_not_a_data_row():
    report = """### NEW BUGS FOUND
CATEGORY | SEVERITY | FILE | DESCRIPTION | CONDITION
--- | --- | --- | --- | ---
EXECUTION BUG | HIGH | x.py | Stop issue | bad state
Recovery failed: id | coid | symbol | qty | stop | broker state
### FIX VALIDATION: N/A
"""

    findings = findings_from_report(report)

    assert findings[-1]["title"] == "Recovery failed: id"
    assert "coid" in findings[-1]["detail"]
    assert findings[-1].get("file", "") != "symbol"
