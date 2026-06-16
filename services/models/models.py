#!/usr/bin/env python3
"""models.py — LLM provider abstraction for the dispatch pipeline.

Model aliases
-------------
  haiku   → claude-haiku-4-5-20251001   fast, cheap, classification/ranking
  sonnet  → claude-sonnet-4-6            balanced default
  opus    → claude-opus-4-8              frontier / hardest problems
  local   → gen-local (MLX Qwen3-Coder) via LiteLLM proxy
  hf:<id> → HuggingFace Inference API   e.g. hf:facebook/bart-large-mnli

Backend auto-selection (priority order)
---------------------------------------
  1. litellm    LITELLM_BASE_URL is set → OpenAI-compatible proxy handles routing
  2. anthropic  ANTHROPIC_API_KEY is set → direct Anthropic Messages API
  3. huggingface HF_TOKEN is set; only for hf: aliases
  4. cli        subprocess `claude -p`; no API key required

Override: MODELS_BACKEND=litellm|anthropic|huggingface|cli

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
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Model alias tables
# ---------------------------------------------------------------------------

# Alias → LiteLLM group name (as configured in litellm.pipeline.yaml)
_LITELLM_ALIASES: Dict[str, str] = {
    "haiku":  "haiku-tier",
    "sonnet": "gen-default",
    "opus":   "gen-frontier",
    "local":  "gen-local",
}

# Alias → (env-var-name, hardcoded-fallback-id)
_ANTHROPIC_MODEL_ENV: Dict[str, tuple] = {
    "haiku":  ("ANTHROPIC_HAIKU_MODEL",    "claude-haiku-4-5-20251001"),
    "sonnet": ("ANTHROPIC_DEFAULT_MODEL",  "claude-sonnet-4-6"),
    "opus":   ("ANTHROPIC_FRONTIER_MODEL", "claude-opus-4-8"),
}

# Alias → raw model id for the CLI --model flag
_CLI_MODEL_IDS: Dict[str, str] = {
    "haiku":  "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-8",
}


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
        result = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            check=True, timeout=300, env=env,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"claude CLI failed ({exc.returncode}): {exc.stderr.strip()}") from exc
    except FileNotFoundError:
        raise RuntimeError(f"claude CLI not found at {claude_bin!r}; set CLAUDE_BIN or use a different backend")


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_BACKENDS = {
    "litellm":     _backend_litellm,
    "anthropic":   _backend_anthropic,
    "huggingface": _backend_huggingface,
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
    p.add_argument("model", help="Model alias: haiku, sonnet, opus, local, hf:<id>")
    p.add_argument("prompt", help="User prompt")
    p.add_argument("--system", default="", help="System message")
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--backend", default="", help="Force backend (sets MODELS_BACKEND)")
    args = p.parse_args(argv)

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
