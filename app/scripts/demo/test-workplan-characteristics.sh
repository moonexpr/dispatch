#!/usr/bin/env bash
# test-workplan-characteristics.sh — the three work-plan characteristics
# (strategy / purpose / issues_affected) + data-driven harness selection.
#
# Verifies, fully OFFLINE:
#   * purpose.py     — label-first, keyword fallback, default; new development
#                      (new-feature + prototype) -> the ponytail harness.
#   * strategy.py    — sequential / swarm / dynamic-workflow derivation + the
#                      swarm parallelization rationale.
#   * multi-issue    — aggregate `resolves` -> a combined `Closes #a Closes #b`.
#   * workorder.py   — the rendered CHARACTERISTICS + ENGINEERING HARNESS blocks.
#   * data-driven    — rules live in workplan-rules.yml: a DISPATCH_WORKPLAN_RULES
#                      override changes behaviour; a missing file degrades safely;
#                      and NO harness content (the ponytail URL/install) is
#                      hardcoded in any .py.
#
# Run from anywhere:  bash scripts/demo/test-workplan-characteristics.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../../.." && pwd)"
PY="${PYTHON_BIN:-python3}"
ACP="${ROOT}/src/architect"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

pass=0 fail=0
ok()  { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad() { printf '  \033[31mFAIL\033[0m %s\n'  "$1"; fail=$((fail+1)); }
assert_eq() { [[ "$2" == "$3" ]] && ok "$1 (=$3)" || bad "$1 (expected '$2', got '$3')"; }
assert_has() { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (missing: $3)" ;; esac; }
assert_not() { case "$2" in *"$3"*) bad "$1 (unexpected: $3)" ;; *) ok "$1" ;; esac; }

pyc() { PYTHONPATH="${ACP}:${ROOT}/src" "${PY}" -c "$1"; }

echo "== WORK-PLAN CHARACTERISTICS test — offline =="
echo
echo "-- purpose.py: label-first / keyword / default + ponytail for new dev --"
assert_eq "label wins (refactor)" "refactor" \
  "$("${PY}" "${ACP}/purpose.py" --title t --body "implement" --labels "purpose:refactor,queued" | jq -r .purpose)"
assert_eq "keyword -> research" "research" \
  "$("${PY}" "${ACP}/purpose.py" --title "Investigate X" --body "spike the feasibility" | jq -r .purpose)"
assert_eq "default -> new-feature" "new-feature" \
  "$("${PY}" "${ACP}/purpose.py" --title zzz --body qqq | jq -r .purpose)"
assert_eq "new-feature harness is ponytail" "ponytail" \
  "$("${PY}" "${ACP}/purpose.py" --title "Add a thing" --body "implement it" | jq -r .harness)"
assert_eq "prototype harness is ponytail" "ponytail" \
  "$("${PY}" "${ACP}/purpose.py" --title t --body "build a proof of concept" | jq -r .harness)"
assert_eq "refactor harness is NOT ponytail" "standard-guardrailed" \
  "$("${PY}" "${ACP}/purpose.py" --title t --body "refactor the module" | jq -r .harness)"

echo
echo "-- strategy.py: sequential / swarm / dynamic-workflow --"
# sequential: single impl unit (no discovered files) -> only the verify unit is parallel.
assert_eq "1 unit -> sequential" "sequential" \
  "$(pyc "import json,decompose,strategy as s
p=decompose.plan({'issue':1,'body':'x','route':'gen-local','scope':'s','confidence':0.9},[])
print(s.resolve_strategy(p,'new-feature')['strategy'])")"
# swarm: 3 independent impl units.
assert_eq "3 parallel units -> swarm" "swarm" \
  "$(pyc "import decompose,strategy as s
p=decompose.plan({'issue':1,'body':'x','route':'gen-default','scope':'m','confidence':0.9},['a.py','b.py','c.py'])
print(s.resolve_strategy(p,'new-feature')['strategy'])")"
# dynamic-workflow: exploratory purpose overrides DAG shape.
assert_eq "research purpose -> dynamic-workflow" "dynamic-workflow" \
  "$(pyc "import decompose,strategy as s
p=decompose.plan({'issue':1,'body':'x','route':'gen-default','scope':'m','confidence':0.9},['a.py','b.py','c.py'])
print(s.resolve_strategy(p,'research')['strategy'])")"
# swarm carries a non-empty parallelization rationale.
assert_eq "swarm has parallelization rationale" "yes" \
  "$(pyc "import decompose,strategy as s
p=decompose.plan({'issue':1,'body':'x','route':'gen-default','scope':'m','confidence':0.9},['a.py','b.py','c.py'])
r=s.resolve_strategy(p,'new-feature')
print('yes' if r['requires_parallelization_rationale'] and r['parallelization_rationale'] else 'no')")"

echo
echo "-- multi-issue aggregation: resolves -> combined Closes --"
multi="$(pyc "import json,decompose,approval,workorder,characteristics as c
job={'issue':2,'repo':'a/x','title':'t','body':'implement','route':'gen-default','scope':'m','confidence':0.9}
tri={'action':'implement','scope':'m','route':'gen-default','confidence':0.9}
plan=decompose.plan(job,[],verify_cmd='bash scripts/smoke.sh')
ch=c.build(job,tri,plan,labels=[],resolves=[3,4])
auth=approval.approve(job,tri)
wo=workorder.render(job,tri,auth,plan=plan,characteristics=ch,verify_cmd='bash scripts/smoke.sh')
print('MULTI' if ch['issues_affected']['multi_issue'] else 'SINGLE')
print('C2' if 'Closes #2' in wo else '-')
print('C3' if 'Closes #3' in wo else '-')
print('C4' if 'Closes #4' in wo else '-')")"
assert_has "issues_affected marks multi-issue" "${multi}" "MULTI"
assert_has "Closes primary #2"  "${multi}" "C2"
assert_has "Closes sibling #3"  "${multi}" "C3"
assert_has "Closes sibling #4"  "${multi}" "C4"

echo
echo "-- rendered work order carries the new sections (via ./dispatch) --"
cd "${ROOT}" || exit 1
wo="$(CLASSIFIER_OFFLINE=1 PIPELINE_DRY_RUN=1 ./dispatch --fixture src/architect/fixtures/play-queue.json 2>/dev/null)"
assert_has "WORK PLAN CHARACTERISTICS block" "$wo" "# WORK PLAN CHARACTERISTICS"
assert_has "Strategy line"        "$wo" "Strategy:"
assert_has "Purpose line"         "$wo" "Purpose:"
assert_has "Issues affected line" "$wo" "Issues affected:"
assert_has "ENGINEERING HARNESS section" "$wo" "# ENGINEERING HARNESS"
assert_has "ponytail harness named"      "$wo" "Harness: ponytail"
assert_has "ponytail engage command"     "$wo" "/plugin install ponytail@ponytail"
# --json envelope carries the characteristics bundle.
if CLASSIFIER_OFFLINE=1 PIPELINE_DRY_RUN=1 ./dispatch --fixture src/architect/fixtures/play-queue.json --json 2>/dev/null \
   | jq -e '.characteristics.strategy.strategy and .characteristics.purpose.purpose and .characteristics.issues_affected.resolves' >/dev/null; then
  ok "--json envelope carries .characteristics"
else
  bad "--json envelope missing .characteristics"
fi

echo
echo "-- data-driven: YAML override + safe degradation + no hardcoded content --"
# Override the rules file: remap new-feature's harness, prove the code follows data.
cat > "${TMP}/rules.yml" <<'YML'
purpose:
  labels: {}
  keyword_rules: []
  default: new-feature
  harness:
    new-feature: { harness: my-custom-harness, rationale: "overridden via env" }
strategy:
  dynamic_purposes: []
  swarm_min_parallel_units: 2
  layouts:
    sequential: { summary: "seq", requires_parallelization_rationale: false }
issues_affected: { include_dag_neighbours: true }
YML
assert_eq "DISPATCH_WORKPLAN_RULES override is honoured" "my-custom-harness" \
  "$(DISPATCH_WORKPLAN_RULES="${TMP}/rules.yml" "${PY}" "${ACP}/purpose.py" --title t --body "add a feature" | jq -r .harness)"
# Missing rules file -> degrades to the content-free skeleton, no crash, default purpose.
assert_eq "missing rules file degrades safely" "new-feature" \
  "$(DISPATCH_WORKPLAN_RULES="${TMP}/does-not-exist.yml" "${PY}" "${ACP}/purpose.py" --title t --body qqq | jq -r .purpose)"
# Anti-hardcode guard: the ponytail harness CONTENT (repo + install) lives ONLY in
# YAML, never baked into a .py module (the operator's data-driven directive).
hits="$(grep -rIl -e "DietrichGebert/ponytail" -e "plugin install ponytail" "${ROOT}/src" --include='*.py' 2>/dev/null | wc -l | tr -d ' ')"
assert_eq "no ponytail content hardcoded in any .py" "0" "${hits}"
yaml_hits="$(grep -c "DietrichGebert/ponytail" "${ROOT}/app/config/workplan-rules.yml" 2>/dev/null | tr -d ' ')"
[[ "${yaml_hits}" -ge 1 ]] && ok "ponytail content lives in app/config/workplan-rules.yml" || bad "ponytail content not in YAML"

echo
echo "== WORK-PLAN CHARACTERISTICS: ${pass} passed, ${fail} failed =="
[[ "${fail}" -eq 0 ]]
