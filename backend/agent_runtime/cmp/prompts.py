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

PATH_NAME_SYSTEM = """\
You name a task node for a session map, from the user's request that started it.
Respond with ONLY a JSON object, no prose:
{"title": "<= 40 chars: concise noun phrase naming the deliverable or subject (keep the user's language)",
 "action": "2-4 word English verb phrase, e.g. 'create report', 'send email', 'fix bug'"}

Examples:
- "buatkan laporan mingguan Perusahaan A" -> {"title": "Laporan Mingguan Perusahaan A", "action": "create report"}
- "please commit aja dulu perubahannya" -> {"title": "Commit perubahan", "action": "commit changes"}
- "cari bug di 3 plugin paling aktif" -> {"title": "Bug hunt 3 plugin teraktif", "action": "find bugs"}"""
