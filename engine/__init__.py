"""engine — shared infrastructure for the Unattended Engineering Pipeline.

Hand-rolled plumbing that is **not** dispatch business logic lives here so the
orchestration / architect / intake src depend on one shared home rather
than re-implementing it:

  * ``runtime`` — generic runtime/OS plumbing (UTC clock + epoch parsing,
    subsystem logging, env-default/dotenv loading, the dry-run command runner,
    tool-presence checks, single-host advisory locks). Stdlib-only leaf.
  * ``proc``    — robust subprocess invocation (leaf module, stdlib-only).
  * ``filesys`` — the app-directory file-access facade (config YAML, personas).
  * ``models``  — LLM/HuggingFace inference + gen-* route resolution.
  * ``structures`` — generic data structures (linked list, stack, FSM, graph).

Engine modules hold *mechanism* only; the dispatch *policy* that composes them —
the pipeline contract ported from lib/common.sh (env-var defaults, gh wrappers,
run-ledger, the scope->route map) — lives in ``src/orchestration/common.py``,
not here. Generic, reusable building blocks are added to this package as they are
factored out of the individual src.
"""
