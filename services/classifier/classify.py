#!/usr/bin/env python3
"""classify.py — issue triage classifier (HANDOFF §5.3, v0).

Maps a GitHub issue (title, body) to a routing decision:

    TriageResult{ action, scope, route, confidence }

v0 implementation: zero-shot NLI via the Hugging Face Inference API
(serverless). One pure function of (title, body); no hidden state.

SECURITY — QUARANTINE READER (HANDOFF §8)
-----------------------------------------
This module reads UNTRUSTED issue text. It is the pipeline's quarantine
reader *by design* and MUST NEVER gain tool/exec capability. It therefore
imports nothing that can run commands (no ``subprocess``, ``os.system``,
``eval``, ``exec``, ``pickle``). It only:
  * reads env vars (config/secrets) via ``os.environ``,
  * performs an outbound HTTPS POST to the configured inference endpoint,
  * emits a JSON object on stdout.
Issue text is data passed as the request body — never interpreted as code or
instructions here or downstream.

Usage
-----
    python3 classify.py --title "..." --body "..."
    python3 classify.py --issue-json fixtures/issue_1.json
    # -> {"action": "...", "scope": "...", "route": "...", "confidence": 0.0}

Exit codes: 0 success; 2 bad args; 3 inference/API failure (so OpenClaw's
command-job semantics surface the failure to the operator — HANDOFF §5.1).

Modes
-----
  * Live (default): requires HF_TOKEN; calls the Inference API; retries with
    backoff; on final failure exits non-zero (NEVER silently degrades —
    HANDOFF §5.3).
  * Offline (CLASSIFIER_OFFLINE=1): deterministic, network-free rule-based
    classification. Used by smoke tests and as an explicitly-opted-in
    degraded mode. Same signature, schema-valid output, fully deterministic.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Dict, List, Tuple

# --- Schema (HANDOFF §5.3) -------------------------------------------------
ACTIONS = ("implement", "needs-human", "wont-do", "duplicate?")
SCOPES = ("xs", "s", "m", "l")
ROUTES = ("gen-local", "gen-default", "gen-frontier")

# scope -> route map (mirrors scripts/lib/common.sh route_for_scope).
_SCOPE_ROUTE = {"xs": "gen-local", "s": "gen-local",
                "m": "gen-default", "l": "gen-frontier"}

# Zero-shot candidate labels per axis, mapped back to our enums.
_ACTION_CANDIDATES = {
    "implement a software change": "implement",
    "requires a human decision": "needs-human",
    "should not be done": "wont-do",
    "duplicate of another issue": "duplicate?",
}
_SCOPE_CANDIDATES = {
    "a tiny one-line change": "xs",
    "a small change": "s",
    "a medium-sized change": "m",
    "a large or architectural change": "l",
}


@dataclass(frozen=True)
class TriageResult:
    action: str       # one of ACTIONS
    scope: str        # one of SCOPES  (drives route)
    route: str        # one of ROUTES
    confidence: float  # 0.0 - 1.0

    def validate(self) -> "TriageResult":
        assert self.action in ACTIONS, f"bad action: {self.action}"
        assert self.scope in SCOPES, f"bad scope: {self.scope}"
        assert self.route in ROUTES, f"bad route: {self.route}"
        assert 0.0 <= self.confidence <= 1.0, f"bad confidence: {self.confidence}"
        return self


# --- Offline deterministic classifier (test / degraded mode) ---------------
# Keyword signals are intentionally simple and fully deterministic: identical
# (title, body) always yields identical output (HANDOFF §7.2).
_XS_HINTS = ("typo", "rename", "comment", "one-line", "one line", "wording",
             "docstring", "lint", "format")
_S_HINTS = ("add a flag", "small", "minor", "tweak", "adjust", "bump version")
_L_HINTS = ("refactor", "redesign", "architecture", "migration", "epic",
            "rewrite", "overhaul", "multi-service", "breaking change")
_WONTDO_HINTS = ("wontfix", "won't do", "wont do", "by design", "not planned")
_DUP_HINTS = ("duplicate", "dupe", "already reported", "same as #")
_VAGUE_HINTS = ("not sure", "maybe", "somehow", "investigate", "unclear",
                "?", "thoughts", "discuss")


def _norm(title: str, body: str) -> str:
    return f"{title}\n{body}".lower()


def classify_offline(title: str, body: str) -> TriageResult:
    """Deterministic, network-free classification."""
    text = _norm(title, body)

    def has(hints: Tuple[str, ...]) -> int:
        return sum(1 for h in hints if h in text)

    # Scope: large hints dominate, then xs, then s, default m.
    if has(_L_HINTS):
        scope = "l"
    elif has(_XS_HINTS):
        scope = "xs"
    elif has(_S_HINTS):
        scope = "s"
    else:
        scope = "m"

    # Action.
    if has(_DUP_HINTS):
        action = "duplicate?"
    elif has(_WONTDO_HINTS):
        action = "wont-do"
    else:
        action = "implement"

    # Confidence: start from a base and add/subtract deterministic signals.
    vague = has(_VAGUE_HINTS)
    body_len = len((body or "").strip())
    conf = 0.60
    conf += 0.10 * min(has(_XS_HINTS) + has(_S_HINTS) + has(_L_HINTS), 3)
    conf += 0.10 if body_len >= 80 else 0.0      # detailed issues read clearer
    conf -= 0.12 * min(vague, 3)                 # vague language lowers it
    conf -= 0.15 if body_len < 25 else 0.0       # near-empty issues are murky
    conf = max(0.05, min(0.99, round(conf, 4)))

    # An uncertain duplicate signal collapses confidence (human should confirm).
    if action == "duplicate?":
        conf = min(conf, 0.50)

    return TriageResult(action, scope, _SCOPE_ROUTE[scope], conf).validate()


# --- Live HF Inference API classifier (v0) ---------------------------------
def _hf_zero_shot(text: str, candidate_labels: List[str],
                  token: str, model: str, base_url: str,
                  retries: int = 4) -> Tuple[str, float]:
    """Call the HF zero-shot pipeline. Returns (top_label, top_score).

    Retries with exponential backoff on transient errors (incl. 503 while the
    model warms up). Raises RuntimeError after exhausting retries.
    """
    url = f"{base_url.rstrip('/')}/models/{model}"
    payload = json.dumps({
        "inputs": text,
        "parameters": {"candidate_labels": candidate_labels,
                       "multi_label": False},
        "options": {"wait_for_model": True},
    }).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}

    last_err = "unknown error"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=payload, headers=headers,
                                         method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            labels = data.get("labels")
            scores = data.get("scores")
            if not labels or not scores:
                raise RuntimeError(f"unexpected response shape: {data!r}")
            return labels[0], float(scores[0])
        except urllib.error.HTTPError as e:  # noqa: PERF203
            last_err = f"HTTP {e.code}"
            if e.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HF inference error: {last_err}") from e
        except (urllib.error.URLError, TimeoutError, RuntimeError) as e:
            last_err = str(e)
        if attempt < retries - 1:
            time.sleep(2 ** attempt)  # 1s, 2s, 4s, 8s
    raise RuntimeError(f"HF inference failed after {retries} tries: {last_err}")


def classify_live(title: str, body: str) -> TriageResult:
    token = os.environ.get("HF_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "HF_TOKEN is required for live classification "
            "(set CLASSIFIER_OFFLINE=1 for the deterministic offline mode).")
    model = os.environ.get("HF_INFERENCE_MODEL", "facebook/bart-large-mnli")
    base_url = os.environ.get("HF_API_URL",
                              "https://api-inference.huggingface.co")
    text = _norm(title, body)

    action_label, action_score = _hf_zero_shot(
        text, list(_ACTION_CANDIDATES), token, model, base_url)
    scope_label, scope_score = _hf_zero_shot(
        text, list(_SCOPE_CANDIDATES), token, model, base_url)

    action = _ACTION_CANDIDATES[action_label]
    scope = _SCOPE_CANDIDATES[scope_label]
    # Confidence: geometric mean of the two axis top-scores (penalises an
    # uncertain decision on either axis).
    confidence = round((action_score * scope_score) ** 0.5, 4)
    return TriageResult(action, scope, _SCOPE_ROUTE[scope],
                        confidence).validate()


def classify(title: str, body: str) -> TriageResult:
    """Top-level entry: offline iff CLASSIFIER_OFFLINE is set, else live."""
    if os.environ.get("CLASSIFIER_OFFLINE", "").strip() in ("1", "true", "yes"):
        return classify_offline(title, body)
    return classify_live(title, body)


# --- CLI -------------------------------------------------------------------
def _load_issue_json(path: str) -> Tuple[str, str]:
    with open(path, "r", encoding="utf-8") as fh:
        obj = json.load(fh)
    return str(obj.get("title", "")), str(obj.get("body", ""))


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description="Triage classifier (HANDOFF §5.3)")
    p.add_argument("--title", default=None)
    p.add_argument("--body", default=None)
    p.add_argument("--issue-json", default=None,
                   help="path to a JSON file with {title, body}")
    args = p.parse_args(argv)

    if args.issue_json:
        title, body = _load_issue_json(args.issue_json)
    else:
        if args.title is None:
            p.error("provide --title/--body or --issue-json")
        title, body = args.title, (args.body or "")

    try:
        result = classify(title, body)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 3  # inference/config failure -> OpenClaw notifies operator

    # Stable key order so output is byte-identical across runs (determinism).
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
