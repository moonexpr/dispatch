"""foundation — the shared, domain-agnostic substrate the rest of the system builds on.

It holds mechanism, not policy: generic runtime/OS plumbing, subprocess invocation,
a file-access facade, model inference, agent-session backends, and generic data
structures. Each module is a reusable building block defined without reference to
any particular caller; higher layers compose these blocks, but foundation never
depends on them:

  * ``runtime``    — generic runtime/OS plumbing (UTC clock + epoch parsing,
                     subsystem logging, env-default/dotenv loading, the dry-run
                     command runner, tool-presence checks, single-host advisory
                     locks). Stdlib-only leaf.
  * ``proc``       — robust subprocess invocation (leaf module, stdlib-only).
  * ``filesys``    — the app-directory file-access facade (config YAML, data files).
  * ``models``     — LLM/HuggingFace inference + gen-* route resolution.
  * ``structures`` — generic data structures (linked list, stack, FSM, graph,
                     issue dependency DAG).
  * ``env``        — broad environment aggregator (process env, dotenv, Keychain).
  * ``agent_sdk``  — headless agent-session backends (CLI, SDK, single-shot chat).
  * ``worker``     — executor abstraction over agents and subprocesses.

Foundation modules hold *mechanism* only; the *policy* that composes them —
what to run, when to run it, and what the results mean — belongs in the higher
layers that import foundation, not here.
"""
