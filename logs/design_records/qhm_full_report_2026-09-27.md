# QHM thesis report — full report in Slack (2026-09-27)

**Problem (Rafael, repeated ask):** the Sunday QHM thesis Slack post showed only a summary. It also:
- trimmed the board text (`p[:2800]`, at most 6 blocks, "…trimmed for Slack — full thesis in the memo");
- ended with a server file path (`logs/quarterly_holds_research_<date>.md`) he cannot open from Slack.

**Design:**
- `build_slack` posts a header and headline, then `memo_to_blocks(memo)`: the ENTIRE memo in Slack mrkdwn, per `rules/slack_format.md`.
  - Headings become bold lines.
  - Markdown tables become one record per row: the bold ticker first (no "Sym:" label, at Rafael's request), then "column: value" lines, with "—" for an empty cell.
  - A table row cut off before its separator row becomes "a · b" values, never raw pipes.
- Blocks are packed on line boundaries into sections of at most 2800 characters.
- `alerts.send_slack_blocks` posts up to 45 blocks per message, in order.
- The board-trimming path and the file-path footer are removed. The pending-approvals note gives a count instead of file names.

**Gate (enforcement, since this was a repeated ask):** `tests/test_qhm_thesis_slack_full.py` checks that:
- every memo word reaches Slack at least as often as in the memo (only each table's first-column label is exempt);
- sections arrive in memo order;
- no block exceeds 3000 characters;
- no `logs/`, `.md`, "full memo", "see memo" or "in the memo" appears;
- no raw `#`, `|table|` or `**` appears;
- all of this holds across 2000 randomly generated messy memos.

**Verification:**
- The real 2026-09-27 memo (13.5k characters) renders as 6 sections in one Slack message, with nothing lost.
- 13 tests pass locally and on the production box (py3.10).

**Reviews:** cold second-agent, 4 rounds.
- Round 1 FAIL: a cut-off table header leaked raw pipes.
- Round 2 FAIL: a header followed by a bare "---" dropped words.
- Round 3 PASS.
- Round 4 PASS, after the "Sym:" label removal.

**Approval:** Rafael approved ("Otherwise approved", with the "Sym" change).

**Follow-up (separate, pending Rafael's SEC contact email):** comprehensive content, meaning real reported fundamentals from SEC EDGAR (board data seats and GAI approve it as T2), Alpaca news, position P&L and the capital picture.
