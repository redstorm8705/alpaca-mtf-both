GROUND-TRUTH FACTS for this diff: reporting/broker_ground_truth.py is a read-only module called only by nightly_audit.py
(a 16:05 ET cron audit; not the trading loop). The diff adds PT-formatted copies of existing ET time fields
(uncovered_windows_pt, cycle_gaps_pt, window_pt) and switches the text produced by render() and alarm_findings() from
ET to PT, per the project rule that every user-facing time is shown in America/Los_Angeles. The alarm trigger still reads
cycle_gaps_et; classify() logic and severities are unchanged. PT = ZoneInfo("America/Los_Angeles"). A cold reviewer
compared alarm_findings()/card_alarms() old vs new on 36 recorded classify() inputs: identical apart from the zone.
55 unit tests pass.
