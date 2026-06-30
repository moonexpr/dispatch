#!/usr/bin/env bash
# test-jobreq.sh — live-wiring test for JOB REQUEST GENERATION (#99, epic #95).
#
# Two parts, both OFFLINE (no model, no gh, no network):
#   A. Emitted Job Request JSON validates against schemas/job-request.json across
#      all four scopes and their routes (xs/s -> gen-local, m -> gen-default,
#      l -> gen-frontier), with negative tests proving the schema has teeth.
#   B. The Engineer runs end-to-end through the pipeline replay bridge
#      (./pipeline --from engineer --until engineer) with
#      ENGINEER_OFFLINE=1 ENGINEER_BIN=scripts/claude-engineer.sh, producing an
#      Invoice that validates against schemas/invoice.json — no `claude -p`, no gh.
#
# The ONE paid live `claude -p` smoke is deliberately NOT run here (operator-gated).
#
# Complements scripts/smoke.sh. Run from anywhere:
#   bash scripts/demo/test-jobreq.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"
PY="${PYTHON_BIN:-python3}"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

pass=0 fail=0
ok()  { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad() { printf '  \033[31mFAIL\033[0m %s\n'  "$1"; fail=$((fail+1)); }
assert_eq() { [[ "$2" == "$3" ]] && ok "$1 (=$3)" || bad "$1 (expected '$2', got '$3')"; }

echo "== JOB REQUEST GENERATION test (#99) — offline =="
echo
echo "-- A. emitted Job Request validates against schemas/job-request.json --"

# Python: build a real job request per scope via pipeline._build_job_request,
# validate with jsonschema, assert route==scope_route mapping, then prove the
# schema rejects malformed requests. Prints one PASS/FAIL line per assertion.
A_OUT="$(ROOT="${ROOT}" "${PY}" - <<'PYEOF'
import os, sys, json, dataclasses
ROOT = os.environ["ROOT"]
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "intake"))
import jsonschema
import tuning
import intake as _intake
import pipeline

schema = json.load(open(os.path.join(ROOT, "schemas", "job-request.json")))
validator = jsonschema.Draft202012Validator(schema)

def make_item(num, title, body, repo):
    # Construct an IntakeItem covering every dataclass field (defaults for the
    # ones the builder doesn't read) so this stays faithful to the real emit path.
    vals = {}
    for f in dataclasses.fields(_intake.IntakeItem):
        if f.name == "number": vals[f.name] = num
        elif f.name == "title": vals[f.name] = title
        elif f.name == "body": vals[f.name] = body
        elif f.name == "repository": vals[f.name] = repo
        elif f.type in ("int",) or f.type is int: vals[f.name] = 0
        else: vals[f.name] = ""
    return _intake.IntakeItem(**vals)

results = []
def check(label, cond):
    results.append((label, bool(cond)))

# Per-scope emission + schema validation + route mapping.
expected_route = tuning.SCOPE_ROUTE  # {'xs':'gen-local','s':'gen-local','m':'gen-default','l':'gen-frontier'}
for scope in ("xs", "s", "m", "l"):
    route = expected_route[scope]
    triage = {"action": "implement", "scope": scope, "route": route, "confidence": 0.9}
    item = make_item(42, f"{scope} task", "Body with acceptance criteria.", "ReclaimByDesign/dispatch-testrepo-a")
    jr = pipeline._build_job_request(item, triage)
    errs = list(validator.iter_errors(jr))
    check(f"scope {scope} job-request schema-valid", not errs)
    check(f"scope {scope} route == {route}", jr.get("route") == route)

# A valid baseline to mutate for the negative (teeth) tests.
base = pipeline._build_job_request(
    make_item(7, "t", "b", "ReclaimByDesign/dispatch-testrepo-a"),
    {"action": "implement", "scope": "m", "route": "gen-default", "confidence": 0.8})
check("baseline valid", not list(validator.iter_errors(base)))

def rejects(label, mutate):
    j = dict(base); mutate(j)
    check(label, bool(list(validator.iter_errors(j))))

rejects("rejects bad route enum",        lambda j: j.__setitem__("route", "gen-bogus"))
rejects("rejects bad scope enum",        lambda j: j.__setitem__("scope", "xl"))
rejects("rejects confidence > 1",        lambda j: j.__setitem__("confidence", 1.5))
rejects("rejects missing required field",lambda j: j.pop("issue"))
rejects("rejects additionalProperties",  lambda j: j.__setitem__("evil", "x"))
rejects("rejects non-slug repo",         lambda j: j.__setitem__("repo", "not-a-slug"))

for label, good in results:
    print(("OK" if good else "NO") + "\t" + label)
PYEOF
)"
# Render each Python assertion as a harness line.
while IFS=$'\t' read -r tag label; do
  [[ -z "${label}" ]] && continue
  if [[ "${tag}" == "OK" ]]; then ok "${label}"; else bad "${label}"; fi
done <<< "${A_OUT}"

echo
echo "-- A2. gen-* route -> model resolution (single source of truth: models.py) --"
MR() { "${PY}" "${ROOT}/foundation/models.py" --route "$1" 2>/dev/null; }
assert_eq "gen-default -> sonnet"        "claude-sonnet-4-6"          "$(MR gen-default)"
assert_eq "gen-frontier -> opus"         "claude-opus-4-8"            "$(MR gen-frontier)"
assert_eq "gen-local -> haiku (no local model defined)" "claude-haiku-4-5-20251001" "$(MR gen-local)"
assert_eq "gen-local is a free variable (GEN_LOCAL_MODEL wins)" "mlx/qwen3-coder" "$(GEN_LOCAL_MODEL='mlx/qwen3-coder' MR gen-local)"
assert_eq "tier id is env-overridable"   "claude-sonnet-X"           "$(ANTHROPIC_DEFAULT_MODEL='anthropic/claude-sonnet-X' MR gen-default)"
assert_eq "unknown route -> gen-default" "claude-sonnet-4-6"         "$(MR gen-bogus)"

echo
echo "-- B. engineer end-to-end OFFLINE through the pipeline bridge --"

# Emit one real Job Request to a file (m scope -> gen-default) for the replay.
JR="${TMP}/job-request.json"
ROOT="${ROOT}" "${PY}" - > "${JR}" <<'PYEOF'
import os, sys, json, dataclasses
ROOT = os.environ["ROOT"]
sys.path.insert(0, os.path.join(ROOT, "src")); sys.path.insert(0, os.path.join(ROOT, "src", "intake"))
import intake as _intake, pipeline
vals = {f.name: (104 if f.name=="number" else
                 "Add a hello endpoint" if f.name=="title" else
                 "Implement GET /hello returning 200." if f.name=="body" else
                 "ReclaimByDesign/dispatch-testrepo-a" if f.name=="repository" else
                 (0 if (f.type is int or f.type=="int") else ""))
        for f in dataclasses.fields(_intake.IntakeItem)}
item = _intake.IntakeItem(**vals)
print(json.dumps(pipeline._build_job_request(item, {"action":"implement","scope":"m","route":"gen-default","confidence":0.82})))
PYEOF
assert_eq "emitted JR is valid JSON" "104" "$(jq -r '.issue' "${JR}")"

# Run the engineer stage offline. PIPELINE_DRY_RUN=0 + ENGINEER_OFFLINE=1 means
# the stub Invoice path: a schema-valid Invoice with NO model/gh calls. --until
# engineer halts before architect-intake (the gh-touching stage) ever runs.
ART="${TMP}/artifacts"; mkdir -p "${ART}"
env PIPELINE_DRY_RUN=0 ENGINEER_OFFLINE=1 \
    ENGINEER_BIN="${ROOT}/scripts/claude-engineer.sh" \
    DISPATCH_ARTIFACTS_DIR="${ART}" DISPATCH_TICK_ID="tick-JOBREQ" \
    "${ROOT}/pipeline" --from engineer --artifact "${JR}" --until engineer \
    > "${TMP}/pipeline.log" 2>&1
rc=$?
assert_eq "pipeline replay exits 0" "0" "${rc}"

INV="${ART}/tick-JOBREQ/invoice.json"
if [[ -f "${INV}" ]]; then
  ok "Invoice written to tick dir"
else
  bad "Invoice written to tick dir (missing ${INV})"; INV="/dev/null"
fi

# Validate the produced Invoice against schemas/invoice.json (with date-time format).
inv_valid="$(ROOT="${ROOT}" INV="${INV}" "${PY}" - <<'PYEOF'
import os, json, jsonschema
schema = json.load(open(os.path.join(os.environ["ROOT"], "schemas", "invoice.json")))
try:
    inv = json.load(open(os.environ["INV"]))
except Exception as e:
    print("LOAD_ERR"); raise SystemExit(0)
v = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
errs = list(v.iter_errors(inv))
print("VALID" if not errs else "INVALID: " + "; ".join(e.message for e in errs[:3]))
PYEOF
)"
assert_eq "Invoice schema-valid" "VALID" "${inv_valid}"
assert_eq "Invoice status completed"   "completed"   "$(jq -r '.status'      "${INV}" 2>/dev/null)"
assert_eq "Invoice route_used default" "gen-default" "$(jq -r '.route_used'  "${INV}" 2>/dev/null)"
assert_eq "Invoice issue carried"      "104"         "$(jq -r '.issue'       "${INV}" 2>/dev/null)"
assert_eq "Invoice pr_number null (no PR offline)" "null" "$(jq -r '.pr_number' "${INV}" 2>/dev/null)"
# Evidence of the no-model/no-gh offline path in the summary.
grep -q "OFFLINE" "${INV}" 2>/dev/null && ok "Invoice summary marks OFFLINE (no model/gh)" \
                                       || bad "Invoice summary marks OFFLINE"

# Regression guard (finding from #95 test-run): a non-completed Invoice carries
# branch:null (claude-engineer.sh emit_invoice). It MUST still be schema-valid —
# branch is type ["string","null"], not the OpenAPI-only `nullable: true`.
nullbr_valid="$(ROOT="${ROOT}" "${PY}" - <<'PYEOF'
import os, json, jsonschema
schema = json.load(open(os.path.join(os.environ["ROOT"], "schemas", "invoice.json")))
inv = {"invoice_id":"issue-9-x","issue":9,"repo":"a/b","status":"failed","branch":None,
       "pr_number":None,"scope_actual":"m","route_used":"gen-default",
       "cost":{"tokens_in":0,"tokens_out":0,"duration_seconds":0,"model":"m"},
       "artifacts":[],"errors":["boom"],"summary":"engineer failed","timestamp":"2026-01-01T00:00:00Z"}
v = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
errs = list(v.iter_errors(inv))
print("VALID" if not errs else "INVALID: " + "; ".join(e.message for e in errs[:3]))
PYEOF
)"
assert_eq "failed Invoice with branch:null is schema-valid" "VALID" "${nullbr_valid}"

# Artifact guard has teeth: a non-schema-valid Job Request is rejected (exit 2).
printf '{"issue":1}\n' > "${TMP}/bad-jr.json"
env PIPELINE_DRY_RUN=0 ENGINEER_OFFLINE=1 ENGINEER_BIN="${ROOT}/scripts/claude-engineer.sh" \
    "${ROOT}/pipeline" --from engineer --artifact "${TMP}/bad-jr.json" --until engineer \
    >/dev/null 2>&1
assert_eq "invalid JR rejected by artifact guard (exit 2)" "2" "$?"

echo
echo "== JOB REQUEST: ${pass} passed, ${fail} failed =="
[[ "${fail}" -eq 0 ]]
