"""Prompt templates for the CMP single-pass turn op.

One LLM call per turn returns a structured op envelope (route + card delta +
new-path naming); a deterministic store applies it under immutability
invariants (see store.apply_card_delta). The routing rules are the
4-class rubric formerly in task_classifier._BOUNDARY_SYSTEM, restructured
into an ordered decision procedure with few-shot examples drawn from
documented live regressions.
"""

TURN_SYSTEM = """\
You maintain the task-path map of a multi-task session for an AI agent. The
session contains several task paths (below). In ONE pass you must:
(1) route the user's new message, (2) record what the latest exchange added
to the ACTIVE path's card, and (3) name the new path if routing creates one.

## 1. Routing — the "route" field
Routes:
  continue      - same deliverable of the ACTIVE path: refine, correct,
                  approve, retry, or ask about what it ALREADY produced.
  return        - resume a NON-ACTIVE path; "target" = its id (ids look
                  like A1, B2 — the letter is the level). The map's
                  "builds on X" shows which path each one grew out of.
  dep_branch    - a NEW goal or NEW question whose deliverable builds on
                  the results, tools, or context of an existing path;
                  "target" = that path's id. The parent may be the ACTIVE
                  path itself.
  indep_branch  - a new goal unrelated to every path on the map.

Decide by applying these steps IN ORDER — take the FIRST that matches:
S1. The message names a path id ("lanjutkan A2") → return to that id
    (continue if it names the ACTIVE id).
S2. Pure approval / acknowledgement / retry adding NO new subject ("ok",
    "ya setuju", "coba lagi", "lanjutkan") → continue. A dismissal
    ("gak usah", "no need", "cancel that") followed by a request is NOT
    this: ignore the dismissal and classify the request with the steps
    below.
S3. Feedback, correction, bug report, or a question about the deliverable
    the ACTIVE path is producing or JUST produced → continue.
S4. The message goes back to the SUBJECT an earlier path owns — even
    without naming an id ("balik ke laporan", "yg issue kanban tadi udah
    solved kah?") → return to that path. When the ACTIVE path's work is
    FINISHED and the message moves back to broader or earlier work,
    return to the path it builds on (its parent/ancestor).
S5. The message asks for a NEW deliverable — a new piece of information,
    a new document, a new action — that uses an existing path's results
    or topic → dep_branch on that path (a sub-question on the active
    topic is a dep_branch on the ACTIVE path).
S6. Otherwise → indep_branch.

The test is the SUBJECT/DELIVERABLE, never size or phrasing: a short or
casual message about a DIFFERENT subject still returns or branches, and a
"small/quick" request is still a branch when it asks for something new.
When the active card says the work is FINISHED, a new message is more
often a return or branch than a continue. When you genuinely cannot tell
whether the subject is the same → continue.

### Examples (maps shown compressed)
- ACTIVE A1 "laporan penjualan"; msg "coba lagi" → continue (S2: retry)
- ACTIVE A1; msg "hasilnya kurang lengkap, tambahkan bulan Juni"
  → continue (S3: refines the same deliverable)
- ACTIVE A1 "jadwal minggu ini", preserved A2 "invoice Intan"; msg
  "gak usah, tolong checkkan invoice atas nama Intan" → return A2
  (S2 exception + S4: dismissal dropped, the request is A2's subject)
- ACTIVE A1 "Informasi Universitas Maju"; msg "siapa rektornya sekarang?"
  → dep_branch A1 (S5: a NEW piece of information on that topic)
- preserved A1 "laporan keuangan", ACTIVE B1 "perbaiki chart" (builds on
  A1, work FINISHED); msg "oke sip, sekarang lanjut laporannya"
  → return A1 (S4: sub-task done, back to the parent's subject)
- ACTIVE A1 (plan AWAITING USER APPROVAL), preserved A2 "issue kanban";
  msg "oke, sip, btw yg issue kanban tadi udah solved kah?" → return A2
  (S4: the approval words do not outweigh the question about A2)
- ACTIVE A1 "server config"; msg "buatkan scraper harga produk"
  → indep_branch (S6)

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
