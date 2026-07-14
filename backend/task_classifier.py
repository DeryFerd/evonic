"""
Task Complexity Classifier — determines if a task is trivial or complex.

Trivial tasks start in "execute" mode (writes allowed immediately).
Complex tasks start in "plan" mode (must plan before writing).

Uses a lightweight LLM call (no tools, no thinking) with an optional
heuristic fast-path to skip the LLM entirely for obvious cases.
"""

import logging
import re
from typing import Optional

import config
from backend.llm_client import LLMClient

_logger = logging.getLogger(__name__)

# Heuristic thresholds
_TRIVIAL_MAX_WORDS = 15
_COMPLEX_MIN_WORDS = 80

# Keywords that strongly suggest complexity (EN + ID equivalents — sessions
# are frequently Indonesian/code-switched and would otherwise never hit
# this deterministic fast-path)
_COMPLEX_KEYWORDS = {
    "refactor", "redesign", "migrate", "architect", "implement",
    "integrate", "optimize", "review", "analyze", "investigate",
    "debug", "troubleshoot", "upgrade", "overhaul", "restructure",
    "design", "plan", "strategy", "multiple", "several", "across",
    "gabungkan", "menggabungkan", "integrasikan", "implementasikan",
    "analisis", "selidiki", "investigasi", "migrasi", "optimalkan",
    "rancang", "desain", "beberapa",
}

# Patterns that suggest trivial single-action tasks
_TRIVIAL_PATTERNS = [
    re.compile(r"^(create|write|make|add|generate)\s+(a\s+)?(\w+\s+){0,3}file", re.I),
    re.compile(r"^(say|print|echo|output)\s+", re.I),
    re.compile(r"^(create|write)\s+hello\s+world", re.I),
]

_CLASSIFIER_SYSTEM = """You classify tasks as TRIVIAL or COMPLEX for an AI coding agent.

TRIVIAL: Can be completed in 1-2 file operations with no ambiguity. Examples:
- "Create a hello world Python file"
- "Add a .gitignore file"
- "Write a simple README"
- "Create an empty index.html"

COMPLEX: Requires research, reading existing code, multi-step changes, or design decisions. Examples:
- "Add authentication to the API"
- "Fix the bug in the payment module"
- "Refactor the database layer"
- "Create a REST API with CRUD operations"
- Any task mentioning existing code/files that need to be understood first

When in doubt, classify as COMPLEX.
Respond with exactly one word: TRIVIAL or COMPLEX"""


_BOUNDARY_SYSTEM = """You route a user's new message in a multi-task session for an AI agent.
The session contains several task paths (below). Decide exactly one:
  CONTINUE        - the message continues the ACTIVE path: feedback, refinement,
                    bug report, correction, or question about the same work.
  RETURN:<id>     - the message resumes a specific NON-ACTIVE path.
  DEP_BRANCH:<id> - a NEW task that USES the results of path <id>
                    (e.g. "now make an invoice for the client A website" after
                    a path that built that website).
  INDEP_BRANCH    - an unrelated new task.
When in doubt, answer CONTINUE.
Respond with exactly one token, e.g.: CONTINUE or RETURN:P2 or DEP_BRANCH:P1 or INDEP_BRANCH"""

_BOUNDARY_RE = re.compile(
    r'\b(CONTINUE|RETURN:(P\d+)|DEP_BRANCH:(P\d+)|INDEP_BRANCH)\b')


def classify_boundary(map_text: str, active_card: str, other_cards: str,
                      user_text: str) -> dict:
    """4-class session boundary decision for CMP.

    Returns {'decision': 'continue'|'return'|'dep_branch'|'indep_branch',
             'target': 'P<n>'|None}. Defaults to continue on ANY doubt,
    parse failure, or LLM error (precision-first: a false branch severs
    context; a missed branch only costs tokens).
    """
    fallback = {"decision": "continue", "target": None}
    text = (user_text or "").strip()
    if not text:
        return fallback
    try:
        client = _get_classifier_client()
        user_prompt = (f"## Path map\n{map_text}\n\n"
                       f"## Active path\n{active_card}\n\n"
                       f"## Other paths\n{other_cards}\n\n"
                       f"## New message\n{text[:4000]}")
        response = client.chat_completion(
            [{"role": "system", "content": _BOUNDARY_SYSTEM},
             {"role": "user", "content": user_prompt}],
            tools=None, temperature=0.0, enable_thinking=False,
            max_tokens=150,
        )
        if not response.get("success"):
            _logger.warning("Boundary classifier LLM call failed: %s",
                            response.get("error_type"))
            return fallback
        msg = (response.get("response", {}).get("choices") or [{}])[0].get("message", {})
        content = (msg.get("content") or "").strip().upper()
        if not content:
            content = (msg.get("reasoning_content") or "").strip().upper()
        m = _BOUNDARY_RE.search(content)
        if not m:
            return fallback
        token = m.group(1)
        if token == "CONTINUE":
            return fallback
        if token == "INDEP_BRANCH":
            return {"decision": "indep_branch", "target": None}
        if token.startswith("RETURN:"):
            return {"decision": "return", "target": m.group(2)}
        return {"decision": "dep_branch", "target": m.group(3)}
    except Exception as e:
        _logger.warning("Boundary classifier failed, defaulting to continue: %s", e)
        return fallback


_CONTINUATION_SYSTEM = """You decide whether a user's new message starts a NEW task or CONTINUES the previous one, for an AI coding agent.

The agent just finished this task:
{goal}

CONTINUATION: feedback, bug reports, follow-ups, refinements, corrections or questions about the SAME work. Examples: "it doesn't work", "change the port to 8080", "add a button to that page", "why is it slow?", "belum bisa", "masih error"
NEW_TASK: an unrelated or clearly separate piece of work — a different project, feature, or goal than the finished task.

When in doubt, answer CONTINUATION.
Respond with exactly one word: NEW_TASK or CONTINUATION"""

# Short follow-ups ("belum bisa", "masih error", "coba lagi") are continuations.
_CONTINUATION_MAX_WORDS = 6


def classify_continuation(previous_goal: str, user_message: str) -> str:
    """Classify a message as 'new_task' or 'continuation' of previous_goal.

    Defaults to 'continuation' on any error — re-planning by surprise is
    worse than staying in the current flow.
    """
    text = (user_message or "").strip()
    if not text or not (previous_goal or "").strip():
        return "continuation"
    if len(text.split()) <= _CONTINUATION_MAX_WORDS:
        return "continuation"

    try:
        client = _get_classifier_client()
        response = client.chat_completion(
            [{"role": "system",
              "content": _CONTINUATION_SYSTEM.format(goal=previous_goal.strip()[:1000])},
             {"role": "user", "content": text[:4000]}],
            tools=None,
            temperature=0.0,
            enable_thinking=False,
            max_tokens=100,
        )
        if not response.get("success"):
            _logger.warning("Continuation classifier LLM call failed: %s",
                            response.get("error_type"))
            return "continuation"
        msg = (response.get("response", {}).get("choices") or [{}])[0].get("message", {})
        content = (msg.get("content") or "").strip().upper()
        if not content:
            content = (msg.get("reasoning_content") or "").strip().upper()
        result = "new_task" if "NEW_TASK" in content else "continuation"
        _logger.info("Message classified as %s (LLM)", result)
        return result
    except Exception as e:
        _logger.warning("Continuation classifier failed, defaulting to continuation: %s", e)
        return "continuation"


def _get_classifier_client() -> LLMClient:
    """Build an LLMClient for classification, using the configured model or default."""
    try:
        from models.db import db
        model_id = db.get_setting('task_classifier_model_id', '')
        if model_id:
            model = db.get_model_by_id(model_id)
            if model:
                return LLMClient(model_config=model)
            _logger.warning("Classifier model_id '%s' not found, falling back to default", model_id)
    except Exception as e:
        _logger.warning("Could not load classifier model config: %s", e)
    return LLMClient()


def _is_enabled() -> bool:
    """Check if the task classifier is enabled (DB setting overrides config default)."""
    try:
        from models.db import db
        default = '1' if config.TASK_CLASSIFIER_ENABLED else '0'
        return db.get_setting('task_classifier_enabled', default) == '1'
    except Exception:
        return config.TASK_CLASSIFIER_ENABLED


def _heuristic_classify(text: str) -> Optional[str]:
    """Fast-path heuristic. Returns 'trivial', 'complex', or None (needs LLM)."""
    words = text.split()
    word_count = len(words)

    # Very short + matches a trivial pattern -> trivial
    if word_count <= _TRIVIAL_MAX_WORDS:
        for pat in _TRIVIAL_PATTERNS:
            if pat.search(text):
                return "trivial"

    # Long message or contains complexity keywords -> complex
    if word_count >= _COMPLEX_MIN_WORDS:
        return "complex"
    lower_words = set(text.lower().split())
    if lower_words & _COMPLEX_KEYWORDS:
        return "complex"

    return None  # uncertain, need LLM


def classify_task(user_message: str) -> str:
    """Classify a task as 'trivial' or 'complex'.

    Returns 'trivial' or 'complex'. Defaults to 'complex' on any error.
    """
    if not _is_enabled():
        return "complex"

    text = user_message.strip()
    if not text:
        return "complex"

    # Try heuristic first
    result = _heuristic_classify(text)
    if result:
        _logger.info("Task classified as %s (heuristic)", result)
        return result

    # LLM classification
    try:
        client = _get_classifier_client()
        messages = [
            {"role": "system", "content": _CLASSIFIER_SYSTEM},
            {"role": "user", "content": text},
        ]
        response = client.chat_completion(
            messages,
            tools=None,
            temperature=0.0,
            enable_thinking=False,
            max_tokens=100,  # Increased from 10 to ensure model can produce full response
        )
        if not response.get("success"):
            _logger.warning("Task classifier LLM call failed: %s", response.get("error_type"))
            return "complex"
        choices = response.get("response", {}).get("choices", [])
        if not choices:
            return "complex"
        # Extract content from LLM response - model may put response in 'content' or 'reasoning_content'
        # (some models like deepseek-v4-flash put the response in reasoning_content field)
        msg = choices[0].get("message", {})
        content = msg.get("content", "").strip().upper()
        reasoning = msg.get("reasoning_content", "").strip().upper()
        # If content is empty, try reasoning_content as fallback
        if not content and reasoning:
            content = reasoning
        if "TRIVIAL" in content:
            _logger.info("Task classified as trivial (LLM)")
            return "trivial"
        _logger.info("Task classified as complex (LLM: %s)", content[:20] if content else "empty/missing")
        return "complex"
    except Exception as e:
        _logger.warning("Task classifier failed, defaulting to complex: %s", e)
        return "complex"
