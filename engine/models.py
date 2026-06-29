#!/usr/bin/env python3
"""models.py — LLM provider abstraction for the dispatch pipeline.

Model aliases
-------------
  haiku   → claude-haiku-4-5-20251001   fast, cheap, classification/ranking
  sonnet  → claude-sonnet-4-6            balanced default
  opus    → claude-opus-4-8              frontier / hardest problems
  local   → gen-local (MLX Qwen3-Coder) via LiteLLM proxy
  hf:<id> → HuggingFace Inference API   e.g. hf:facebook/bart-large-mnli
  or:<id> → OpenRouter chat completions e.g. or:openai/gpt-4o-mini (non-Anthropic adversary)

Backend auto-selection (priority order)
---------------------------------------
  1. litellm    LITELLM_BASE_URL is set → OpenAI-compatible proxy handles routing
  2. huggingface HF_TOKEN is set; only for hf: aliases
  3. openrouter  OPENROUTER_API_KEY is set; only for or: aliases (OpenAI-compatible)
  4. anthropic  ANTHROPIC_API_KEY is set → direct Anthropic Messages API
  5. cli        subprocess `claude -p`; no API key required

Override: MODELS_BACKEND=litellm|huggingface|openrouter|anthropic|cli

Env vars consumed
-----------------
  MODELS_BACKEND             force a backend (optional)
  LITELLM_BASE_URL           proxy base URL (default: http://127.0.0.1:4000)
  LITELLM_MASTER_KEY         proxy auth key
  ANTHROPIC_API_KEY          direct Anthropic auth
  ANTHROPIC_HAIKU_MODEL      override haiku model id
  ANTHROPIC_DEFAULT_MODEL    override sonnet model id
  ANTHROPIC_FRONTIER_MODEL   override opus model id
  HF_TOKEN                   HuggingFace auth token
  HF_API_URL                 HF Inference API base (default: https://api-inference.huggingface.co)
  OPENROUTER_API_KEY         OpenRouter auth token (enables the openrouter backend)
  OPENROUTER_MODEL           default OpenRouter model id when an or: alias omits one
                             (default: openai/gpt-4o-mini)
  OPENROUTER_BASE_URL        OpenRouter API base (default: https://openrouter.ai/api/v1)
  CLAUDE_BIN                 claude CLI binary (default: claude)

Public API
----------
  complete(model, prompt, *, system="", max_tokens=1024, temperature=0.0) -> str
  chat(model, messages, *, max_tokens=1024, temperature=0.0) -> str

  messages: list of {"role": "system"|"user"|"assistant", "content": str}

CLI (for manual testing)
------------------------
  python3 models.py haiku "What is 2+2?"
  python3 models.py sonnet "Explain this issue..." --system "You are a PM."
  MODELS_BACKEND=anthropic python3 models.py haiku "ping"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# Make `from engine import filesys` resolve whether this module is imported as
# `engine.models` or run as a script (`python3 engine/models.py --route ...`),
# where sys.path[0] is engine/ rather than the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import filesys, proc  # noqa: E402


# ---------------------------------------------------------------------------
# Model alias tables — DATA, loaded from app/config/models.yml (state out of
# code), via the engine.filesys facade. The literal tables moved to YAML; the
# resolution logic stays here. Env-var overrides (ANTHROPIC_*_MODEL,
# GEN_LOCAL_MODEL) are still applied at runtime by model_id_for_route / the
# backends, not baked into the table.
# ---------------------------------------------------------------------------
_MODELS_CFG: Dict[str, Any] = filesys.read_yaml("config/models.yml")

# Alias → LiteLLM group name (as configured in litellm.pipeline.yaml)
_LITELLM_ALIASES: Dict[str, str] = dict(_MODELS_CFG.get("litellm_aliases") or {})

# Alias → (env-var-name, hardcoded-fallback-id)
_ANTHROPIC_MODEL_ENV: Dict[str, tuple] = {
    alias: (spec["env"], spec["fallback"])
    for alias, spec in (_MODELS_CFG.get("anthropic") or {}).items()
}

# Alias → raw model id for the CLI --model flag
_CLI_MODEL_IDS: Dict[str, str] = dict(_MODELS_CFG.get("cli_model_ids") or {})

# Generation route (the gen-* tiers carried on Job Requests / Invoices) → model
# alias. gen-local is intentionally NOT in this table — it is a *free variable*
# resolved by model_id_for_route() (see below).
_ROUTE_TO_ALIAS: Dict[str, str] = dict(_MODELS_CFG.get("route_to_alias") or {})


def model_id_for_route(route: str) -> str:
    """Resolve a gen-* generation route to a concrete model id — the single
    source of truth for every gen-* reference in the pipeline.

      gen-default  → sonnet tier (ANTHROPIC_DEFAULT_MODEL  or claude-sonnet-4-6)
      gen-frontier → opus tier   (ANTHROPIC_FRONTIER_MODEL or claude-opus-4-8)
      gen-local    → a FREE VARIABLE: whatever local model the environment names
                     in GEN_LOCAL_MODEL; if that is unset, falls back to the
                     haiku tier (ANTHROPIC_HAIKU_MODEL or claude-haiku-4-5-20251001).

    Unknown routes resolve as gen-default. The returned id is bare (any leading
    'anthropic/' provider prefix is stripped) so it can be passed straight to
    `claude -p --model <id>`.
    """
    alias = _ROUTE_TO_ALIAS.get(route, "sonnet")
    if route == "gen-local":
        explicit = os.environ.get("GEN_LOCAL_MODEL", "").strip()
        if explicit:
            return explicit.removeprefix("anthropic/")
        alias = "haiku"  # documented fallback when no local model is configured
    env_var, fallback = _ANTHROPIC_MODEL_ENV[alias]
    return os.environ.get(env_var, fallback).removeprefix("anthropic/")


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

def _detect_backend(model: str) -> str:
    forced = os.environ.get("MODELS_BACKEND", "").strip()
    if forced:
        return forced
    if os.environ.get("LITELLM_BASE_URL"):
        return "litellm"
    if model.startswith("hf:") and os.environ.get("HF_TOKEN"):
        return "huggingface"
    if model.startswith("or:") and os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "cli"


# ---------------------------------------------------------------------------
# HTTP helper (stdlib only)
# ---------------------------------------------------------------------------

def _post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Any:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:500]
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------

def _backend_litellm(
    model: str,
    messages: List[Dict[str, str]],
    max_tokens: int,
    temperature: float,
) -> str:
    base = os.environ.get("LITELLM_BASE_URL", "http://127.0.0.1:4000").rstrip("/")
    key  = os.environ.get("LITELLM_MASTER_KEY", "")

    if model.startswith("hf:"):
        model_id = f"huggingface/{model[3:]}"  # litellm provider-prefixed form
    else:
        model_id = _LITELLM_ALIASES.get(model, model)

    resp = _post_json(
        f"{base}/v1/chat/completions",
        {"model": model_id, "messages": messages,
         "max_tokens": max_tokens, "temperature": temperature},
        {"Authorization": f"Bearer {key}"} if key else {},
    )
    return resp["choices"][0]["message"]["content"]


def _backend_anthropic(
    model: str,
    messages: List[Dict[str, str]],
    max_tokens: int,
    temperature: float,
) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    env_var, fallback = _ANTHROPIC_MODEL_ENV.get(model, ("", model))
    raw_id = os.environ.get(env_var, fallback) if env_var else model
    # pipeline.env stores litellm-prefixed ids like "anthropic/claude-sonnet-4-6"
    model_id = raw_id.removeprefix("anthropic/")

    # Anthropic API takes system as a top-level field, not in messages
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user_turns = [m for m in messages if m["role"] != "system"]

    payload: Dict[str, Any] = {
        "model": model_id,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": user_turns,
    }
    if system:
        payload["system"] = system

    resp = _post_json(
        "https://api.anthropic.com/v1/messages",
        payload,
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
    )
    return resp["content"][0]["text"]


def _backend_huggingface(
    model: str,
    messages: List[Dict[str, str]],
    max_tokens: int,
    temperature: float,
) -> str:
    if not model.startswith("hf:"):
        raise ValueError(f"huggingface backend requires hf:<model-id>, got: {model!r}")
    model_id = model[3:]
    hf_token = os.environ.get("HF_TOKEN", "")
    base = os.environ.get("HF_API_URL", "https://api-inference.huggingface.co").rstrip("/")

    # Flatten messages into a single prompt string
    prompt = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages)
    prompt += "\nASSISTANT:"

    resp = _post_json(
        f"{base}/models/{model_id}",
        {"inputs": prompt,
         "parameters": {"max_new_tokens": max_tokens,
                        "temperature": max(temperature, 0.01)}},
        {"Authorization": f"Bearer {hf_token}"} if hf_token else {},
    )
    if isinstance(resp, list):
        generated = resp[0].get("generated_text", "")
        return generated.split("ASSISTANT:")[-1].strip()
    return resp.get("generated_text", str(resp))


def _backend_openrouter(
    model: str,
    messages: List[Dict[str, str]],
    max_tokens: int,
    temperature: float,
) -> str:
    """OpenRouter (OpenAI-compatible chat completions). Lets the pipeline reach a
    genuinely non-Anthropic adversary family (gpt-*, gemini-*, llama-*, …) for the
    #111 cross-model weigh-in. The model id is the bare OpenRouter id; accept an
    explicit ``or:<id>`` alias, else fall back to OPENROUTER_MODEL."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    model_id = model[3:] if model.startswith("or:") else \
        os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    if not model_id:
        model_id = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    base = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    resp = _post_json(
        f"{base}/chat/completions",
        {"model": model_id, "messages": messages,
         "max_tokens": max_tokens, "temperature": temperature},
        {"Authorization": f"Bearer {api_key}"},
    )
    return resp["choices"][0]["message"]["content"]


def _backend_cli(
    model: str,
    messages: List[Dict[str, str]],
    max_tokens: int,
    temperature: float,
) -> str:
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    prompt = "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages)
    model_id = _CLI_MODEL_IDS.get(model)
    # Pass prompt via stdin to avoid hitting shell argument length limits on large payloads.
    cmd = [claude_bin, "-p", "-"]
    if model_id:
        cmd += ["--model", model_id]
    # Strip ANTHROPIC_API_KEY so claude uses its OAuth subscription, not API credits.
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        result = proc.run(cmd, input=prompt, check=True, timeout=300, env=env)
        return result.stdout.strip()
    except proc.ProcTimeout:
        raise  # timeout propagates, as it did before (was subprocess.TimeoutExpired)
    except proc.ProcError as exc:
        if exc.returncode is None:  # launch failure — e.g. the binary is missing
            raise RuntimeError(
                f"claude CLI not found at {claude_bin!r}; set CLAUDE_BIN or use a different backend"
            ) from exc
        raise RuntimeError(
            f"claude CLI failed ({exc.returncode}): {(exc.stderr or '').strip()}"
        ) from exc


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_BACKENDS = {
    "litellm":     _backend_litellm,
    "anthropic":   _backend_anthropic,
    "huggingface": _backend_huggingface,
    "openrouter":  _backend_openrouter,
    "cli":         _backend_cli,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def chat(
    model: str,
    messages: List[Dict[str, str]],
    *,
    max_tokens: int = 1024,
    temperature: float = 0.0,
) -> str:
    """Send a messages list to the specified model alias; return the reply text."""
    backend = _detect_backend(model)
    fn = _BACKENDS.get(backend)
    if fn is None:
        raise ValueError(f"unknown backend {backend!r}; valid: {sorted(_BACKENDS)}")
    return fn(model, messages, max_tokens, temperature)


def complete(
    model: str,
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 1024,
    temperature: float = 0.0,
) -> str:
    """Single-turn completion. Wraps prompt in a user message."""
    messages: List[Dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return chat(model, messages, max_tokens=max_tokens, temperature=temperature)


# ---------------------------------------------------------------------------
# CLI (manual testing)
# ---------------------------------------------------------------------------

def _cli_main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(
        description="Test the models API. Env vars control the backend.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python3 models.py haiku 'What is 2+2?'\n"
               "  MODELS_BACKEND=anthropic python3 models.py sonnet 'ping'\n"
               "  python3 models.py hf:facebook/bart-large-mnli 'classify this'",
    )
    p.add_argument("model", nargs="?", help="Model alias: haiku, sonnet, opus, local, hf:<id>")
    p.add_argument("prompt", nargs="?", help="User prompt")
    p.add_argument("--system", default="", help="System message")
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--backend", default="", help="Force backend (sets MODELS_BACKEND)")
    p.add_argument("--route", default="",
                   help="Resolve a gen-* route to a concrete model id and exit "
                        "(no model call). e.g. --route gen-local")
    args = p.parse_args(argv)

    # Route resolution mode: print the model id for a gen-* route and exit.
    if args.route:
        print(model_id_for_route(args.route))
        return 0

    if not args.model or not args.prompt:
        p.error("model and prompt are required unless --route is given")

    if args.backend:
        os.environ["MODELS_BACKEND"] = args.backend

    backend = _detect_backend(args.model)
    print(f"[models] backend={backend} model={args.model}", file=sys.stderr)

    try:
        reply = complete(
            args.model, args.prompt,
            system=args.system,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
        )
        print(reply)
        return 0
    except RuntimeError as exc:
        print(f"[models] error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_cli_main(sys.argv[1:]))
