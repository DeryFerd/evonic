"""
Robust JSON extraction from LLM responses.

Models routinely wrap JSON in fences, prepend reasoning, or append prose —
naive `text[find('{'):rfind('}')+1]` slices die with "Extra data" whenever
the trailing prose contains a brace. This helper returns the FIRST complete
JSON object via json.JSONDecoder.raw_decode, which tolerates anything after
the object.
"""
from __future__ import annotations

import json
import re

_FENCE_RE = re.compile(r'```(?:json)?\s*(.*?)```', re.DOTALL)
_decoder = json.JSONDecoder()


def extract_first_json(text: str):
    """Return the first complete JSON object (dict) in an LLM response,
    or None. Fenced ```json blocks are preferred; otherwise scans for the
    first parseable object, ignoring surrounding prose."""
    if not text:
        return None
    candidates = []
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1))
    candidates.append(text)
    for candidate in candidates:
        idx = candidate.find('{')
        while idx != -1:
            try:
                obj, _end = _decoder.raw_decode(candidate[idx:])
                if isinstance(obj, dict):
                    return obj
            except ValueError:
                pass
            idx = candidate.find('{', idx + 1)
    return None
