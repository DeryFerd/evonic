"""Prompt templates for CMP LLM calls (card generation; boundary
classification lives in task_classifier alongside its siblings)."""

CARD_SYSTEM = """\
You summarize ONE task thread from an AI-agent session into a fixed-schema
card that lets the agent resume this task later without the transcript.
Respond with ONLY a JSON object, no prose:
{"title": "<= 60 chars",
 "action": "2-4 word verb phrase naming the task, e.g. 'create report'",
 "goal": "one sentence: what the task is trying to achieve",
 "outcome": "one sentence: where it stands now; empty string if just started",
 "key_facts": ["<= 6 short strings: decisions made, constraints discovered, FAILURES AND THEIR CAUSES, locations"],
 "artifacts": ["<= 8 file paths or URLs created/modified"]}

key_facts must capture anything a future return needs: chosen approach,
why something failed, where things live. Do not invent facts."""

CARD_USER = """\
## Transcript (this task only)
{transcript}

## Task-graph record (if any)
{atg_block}"""
