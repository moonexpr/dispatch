#!/usr/bin/env python3
"""gen-logo.py — generate the dispatch logo via OpenRouter's image models.

Calls an OpenRouter image-capable model (default: Google Gemini flash-image) with
a prompt and writes every returned image to disk as PNG. Used to produce the
project logo; kept in-tree so the artwork can be regenerated or re-themed.

Auth: reads OPENROUTER_API_KEY from the environment.

    python scripts/gen-logo.py --out docs/assets/dispatch-logo.png
    python scripts/gen-logo.py --prompt-file scripts/logo-prompt.txt --out X.png
    python scripts/gen-logo.py --model google/gemini-2.5-flash-image --n 1

Pure standard library + an HTTPS POST. Exit 0 on at least one image written.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemini-2.5-flash-image"

DEFAULT_PROMPT = (
    "A clean, modern, flat vector logo for a developer tool named \"dispatch\". "
    "Central metaphor: an air-traffic-control tower / mission-control HQ that "
    "coordinates remote units by broadcasting signal. Show a stylized control "
    "tower or beacon emitting concentric broadcast/radio waves (the communication "
    "signal — like a bell, siren, or transmitter radiating outward), suggesting a "
    "central authority directing distributed work. Minimal geometric shapes, bold "
    "and legible at small sizes, strong silhouette, suitable as an app icon. "
    "Deep navy and teal with a single warm signal-amber accent on the emitted "
    "waves. Subtle, professional, no text, no lettering, centered composition on "
    "a transparent or solid background. High contrast, crisp edges."
)


def request_images(model: str, prompt: str, key: str) -> list[bytes]:
    payload = {
        "model": model,
        "modalities": ["image", "text"],
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/ReclaimByDesign/dispatch",
            "X-Title": "dispatch logo",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = json.loads(resp.read().decode())

    images: list[bytes] = []
    for choice in body.get("choices", []):
        for img in (choice.get("message") or {}).get("images", []) or []:
            url = (img.get("image_url") or {}).get("url", "")
            if url.startswith("data:") and "base64," in url:
                images.append(base64.b64decode(url.split("base64,", 1)[1]))
    if not images:
        # Surface the model's text so a refusal / error is visible.
        text = ""
        for choice in body.get("choices", []):
            text += (choice.get("message") or {}).get("content", "") or ""
        raise RuntimeError(
            "no images in response"
            + (f"; model said: {text.strip()[:400]}" if text.strip() else f"; raw: {json.dumps(body)[:400]}")
        )
    return images


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the dispatch logo via OpenRouter.")
    ap.add_argument("--out", type=Path, default=Path("docs/assets/dispatch-logo.png"))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--prompt", default=None)
    ap.add_argument("--prompt-file", type=Path, default=None)
    ap.add_argument("--n", type=int, default=1, help="number of generations (variants)")
    args = ap.parse_args(argv)

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("error: OPENROUTER_API_KEY not set", file=sys.stderr)
        return 1

    if args.prompt_file:
        prompt = args.prompt_file.read_text()
    elif args.prompt:
        prompt = args.prompt
    else:
        prompt = DEFAULT_PROMPT

    args.out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    for i in range(args.n):
        try:
            images = request_images(args.model, prompt, key)
        except Exception as exc:  # noqa: BLE001 — report and continue to next variant
            print(f"  variant {i + 1}: FAILED — {exc}", file=sys.stderr)
            continue
        for j, data in enumerate(images):
            if args.n == 1 and j == 0:
                dest = args.out
            else:
                dest = args.out.with_name(f"{args.out.stem}-{i + 1}{j + 1}{args.out.suffix}")
            dest.write_bytes(data)
            print(f"  wrote {dest}  ({len(data)} bytes)")
            written += 1

    print(f"done: {written} image(s) written.")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
