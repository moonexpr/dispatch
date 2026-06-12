#!/usr/bin/env python3
"""classify_local_stub.py — v1 local classifier stub (HANDOFF §5.3).

INTERFACE-ONLY SCAFFOLD. Do not implement training here; this file exists so
the v1 seam is concrete and review-able. It mirrors classify.py's signature
(same TriageResult) so the dispatch path can swap implementations behind one
import once a trained model exists.

PLAN (v1, deferred — HANDOFF §9 "local classifier training")
------------------------------------------------------------
Replace the v0 zero-shot HF Inference call with a fine-tuned sequence
classifier served locally:

  * Model:   a ModernBERT- / DeBERTa-v3-class encoder with one classification
             head per axis (action, scope), or a single multi-task head.
  * Framework: PyTorch, device = MPS on Apple silicon (fall back to CPU).
  * Training data: the issue -> label history THIS pipeline accrues over time
             (durable in GitHub: closed issues + the labels dispatch/closure
             applied). No synthetic data; the pipeline is its own training set.
  * Pipeline: tokenize(title + body) -> encoder -> per-axis softmax ->
             argmax -> map to enums -> confidence = max softmax prob (or a
             calibrated temperature-scaled prob).
  * Serving:  load once into a long-lived process / local endpoint; classify
             is a pure function of (title, body), like v0.

SECURITY (unchanged from v0): this remains a QUARANTINE READER (HANDOFF §8).
It reads untrusted issue text and MUST NOT gain tool/exec capability. A local
model does not change that: no subprocess, no eval, text is data only.
"""
from __future__ import annotations

# Reuse the canonical schema so the two implementations cannot drift.
from classify import TriageResult  # noqa: F401  (re-exported for callers)

# TODO(v1): import torch / transformers here once the dependency is approved.
#   import torch
#   from transformers import AutoModelForSequenceClassification, AutoTokenizer

# TODO(v1): module-level lazy singletons so the model loads once per process.
#   _MODEL = None
#   _TOKENIZER = None
#   _DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"

_MODEL_DIR_ENV = "CLASSIFIER_LOCAL_MODEL_DIR"  # where the fine-tune is saved


def _load_model():
    """TODO(v1): load tokenizer + fine-tuned encoder from CLASSIFIER_LOCAL_MODEL_DIR."""
    raise NotImplementedError(
        "v1 local classifier not implemented; use classify.py (HF v0). "
        f"Training/serving from ${_MODEL_DIR_ENV} is deferred (HANDOFF §9).")


def classify(title: str, body: str) -> "TriageResult":  # noqa: D401
    """Same signature/contract as classify.classify — v1 implementation TBD."""
    _load_model()
    raise NotImplementedError  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(
        "classify_local_stub.py is a v1 interface stub; run classify.py for v0.")
