"use strict";
/**
 * /update-docs — docs/CHANGELOG for a merge-candidate PR (HANDOFF §5.4)
 *
 * args (global):
 *   { pr: <number> }
 *
 * Phases:
 *   1. Read the merge-candidate diff.
 *   2. Update docs/CHANGELOG on the `gen-local` tier (bounded mechanical work).
 *   3. Verifier subagent on the `critic` tier (cross-family) checks each doc
 *      claim against the diff.
 *   4. Push to the SAME branch.
 *
 * Never merges. Operates only on the PR's existing branch.
 */

const input = (typeof args !== "undefined" && args) ? args : {};
const runtime = (typeof globalThis !== "undefined") ? globalThis : {};

function requireArg(obj, key) {
  if (obj == null || obj[key] === undefined || obj[key] === null) {
    throw new Error(`/update-docs: missing required arg '${key}'`);
  }
  return obj[key];
}

function subagent(opts) {
  if (typeof runtime.spawnSubagent === "function") return runtime.spawnSubagent(opts);
  if (typeof runtime.agent === "function") return runtime.agent(opts);
  console.log("[update-docs][plan]", JSON.stringify(opts));
  return { planned: true };
}

async function run() {
  const pr = requireArg(input, "pr");

  // Phase 1 — read the merge-candidate diff.
  const diff = await subagent({
    model: "triage",
    tools: ["gh", "git"],
    prompt:
      `Fetch the diff for PR #${pr} (the merge candidate). Summarize the ` +
      `user-facing and API-facing changes that documentation must reflect.`,
  });

  // Phase 2 — mechanical docs/CHANGELOG update on gen-local.
  const docs = await subagent({
    model: "gen-local",
    isolation: "worktree",
    tools: ["read", "edit", "git"],
    prompt:
      `Update docs and CHANGELOG on PR #${pr}'s branch to match these ` +
      `changes. Be precise; do not invent behavior not present in the diff.` +
      `\nDiff summary:\n${JSON.stringify(diff)}`,
  });

  // Phase 3 — cross-family verifier checks every doc claim against the diff.
  const verification = await subagent({
    model: "critic",
    tools: ["read", "gh"],
    prompt:
      `You are an adversarial doc verifier (cross-family vs the writer). For ` +
      `each claim in the updated docs/CHANGELOG, confirm it is supported by ` +
      `the PR #${pr} diff. List any unsupported or inaccurate claim to fix.` +
      `\nDocs:\n${JSON.stringify(docs)}`,
  });

  // Phase 4 — push to the same branch (no merge).
  const push = await subagent({
    model: "triage",
    tools: ["git", "gh"],
    prompt:
      `If the verifier flagged issues, correct them, then push the docs ` +
      `commit to PR #${pr}'s existing branch. Do NOT merge.\nVerification:\n` +
      `${JSON.stringify(verification)}`,
  });

  return { pr, docs, verification, push };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { run };
}

if (typeof runtime.__WORKFLOW_AUTORUN__ === "undefined" || runtime.__WORKFLOW_AUTORUN__) {
  Promise.resolve()
    .then(run)
    .catch((err) => {
      console.error(`/update-docs failed: ${err && err.message}`);
      if (typeof process !== "undefined" && process.exitCode === undefined) {
        process.exitCode = 1;
      }
    });
}
