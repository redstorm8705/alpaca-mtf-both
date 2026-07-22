#!/usr/bin/env python3
"""
UserPromptSubmit hook — injects a standing reminder each turn. Forks are
conversational (not tool calls) so they cannot be hard-blocked; this keeps the
Open Question Protocol unavoidable. Soft control by design.
"""
print(
    "[protocol reminder] Before presenting ANY decision/fork to Rafael, convene "
    "board + Gro + GAI FIRST and bring their consolidated POV WITH the fork "
    "(Open Question Protocol). Before shipping ANY gated code, run the final "
    "Gro+GAI pre-ship audit (preship_gate enforces this on commit/push)."
)
