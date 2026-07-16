"""Prompt templates for the CMP single-pass turn op.

One LLM call per turn returns a structured op envelope (route + card delta +
new-path naming); a deterministic store applies it under immutability
invariants (see store.apply_card_delta). The routing rules are the
battle-tested 4-class rubric formerly in task_classifier._BOUNDARY_SYSTEM.
"""

TURN_SYSTEM = """\
You maintain the task-path map of a multi-task session for an AI agent. The
session contains several task paths (below). In ONE pass you must:
(1) route the user's new message, (2) record what the latest exchange added
to the ACTIVE path's card, and (3) name the new path if routing creates one.

## 1. Routing — the "route" field. Decide exactly one:
  continue      - the message is about the ACTIVE path's SAME deliverable:
                  feedback, refinement, bug report, correction, approval,
                  or a question about that work.
  return        - the message resumes a specific NON-ACTIVE path; put its id
                  in "target" (ids look like A1, A2, B1 - the letter is the
                  level).
  dep_branch    - a NEW goal with a DIFFERENT deliverable that uses the
                  results, tools, or context of an existing path; put that
                  path's id in "target". Examples: "now make an invoice for
                  the client A website" (after the path that built it);
                  "update the CLI tool we just used"; "rebuild the server
                  that hosts it".
  indep_branch  - a new goal unrelated to any existing path.

The test is the SUBJECT/DELIVERABLE, not the size or the phrasing. A message
about a DIFFERENT subject than the active path (different entity, document,
project, or kind of work) is a BRANCH or RETURN — never CONTINUE — even if it
is short or sounds casual. Example: active path is about the user's SCHEDULE
and the message is "gak usah, tolong checkkan invoice atas nama Intan" — that
is about invoices, a different subject, so it BRANCHES (or RETURNS to an
existing invoice path), NOT continue. A dismissal like "gak usah"/"no need"/
"cancel that" followed by a new request means: classify the NEW request.

CONTINUE only when the message is about the SAME subject/deliverable the
active path is producing: feedback, refinement, correction, follow-up, or a
question about that same work. "Small/quick" alone does not make it a
continue — a small request on a different subject still branches.

Also CONTINUE for approvals or acknowledgements of the active path's plan
("ok", "lanjutkan", "ya, setuju"). Explicitly mentioning a path id (e.g.
"lanjutkan A2") is a RETURN to that path. When you cannot tell whether the
subject is the same, answer continue.

## 2. Card delta — the "card" field (for the ACTIVE path):
  "outcome":       one sentence: where the active path stands NOW, after the
                   last agent reply. Omit or empty if nothing was delivered.
  "new_facts":     0-3 short strings: ONLY NEW information from the latest
                   exchange — decisions made, constraints discovered,
                   FAILURES AND THEIR CAUSES, locations. NEVER repeat facts
                   already on the active path's card. Do not invent facts.
  "new_artifacts": file paths or URLs newly created/modified, if any.

## 3. New path naming — the "new_path" field, ONLY when route is
dep_branch or indep_branch (omit otherwise):
  "title":  <= 40 chars, concise noun phrase naming the deliverable or
            subject (keep the user's language).
  "action": 2-4 word English verb phrase, e.g. "create report",
            "send email", "fix bug".

Respond with ONLY a JSON object, no prose:
{"route": "continue" | "return" | "dep_branch" | "indep_branch",
 "target": "<path id — required for return and dep_branch>",
 "new_path": {"title": "...", "action": "..."},
 "card": {"outcome": "...", "new_facts": ["..."], "new_artifacts": ["..."]}}"""

TURN_USER = """\
## Path map
{map_text}

## Active path
{active_card}

## Other paths
{other_cards}

{dialogue_block}## New message
{user_text}{init_note}"""

# Appended to TURN_USER on the session's very first message: the map holds
# only the just-created first path (mechanically titled with the raw
# message), so the only job is to name it properly.
TURN_INIT_NOTE = """

## Note
This is the session's FIRST message; the single path on the map was just
created from it. Set route to "continue" and fill "new_path" with a proper
title/action for that first path."""
