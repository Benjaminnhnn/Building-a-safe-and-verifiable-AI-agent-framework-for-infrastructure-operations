# Helpers to choose an immutable GHCR image that CI has validated.
# Source from the repository root (bash or zsh):
#   source automation/lib/image-select.sh
#
# Image workflows tag with the push head SHA and run independently of ci.yml,
# so "the last commit that touched <dir>" may have no image, and an existing
# image may not have passed CI. Ask the registry instead of guessing from git.

# pick_image <package> <source dir>
#   Print the newest tag that exists on GHCR, is an ancestor of origin/develop,
#   and has the same <source dir> tree as origin/develop. Prints nothing if none.
#   ${t} is braced because zsh treats "$t:a..." as a path modifier.
pick_image() {
  local pkg="$1" dir="$2" token want t best=""
  git fetch -q origin
  token="$(curl -s "https://ghcr.io/token?scope=repository:benjaminnhnn/${pkg}:pull" | jq -r .token)"
  want="$(git rev-parse "origin/develop:${dir}")"
  for t in $(curl -s -H "Authorization: Bearer $token" "https://ghcr.io/v2/benjaminnhnn/${pkg}/tags/list" | jq -r '.tags[]'); do
    git cat-file -e "${t}^{commit}" 2>/dev/null || continue
    git merge-base --is-ancestor "${t}" origin/develop || continue
    [ "$(git rev-parse "${t}:${dir}")" = "$want" ] || continue
    if [ -z "$best" ] || git merge-base --is-ancestor "$best" "${t}"; then best="${t}"; fi
  done
  echo "$best"
}

# ci_ok <sha>
#   Succeed only when every ci.yml run for that exact commit concluded
#   "success". Uses gh when logged in, otherwise the public GitHub REST API
#   (works while the repository is public; 60 requests/hour unauthenticated).
#   Returns 2 (not 1) when neither source answers, so a tooling problem is not
#   mistaken for a red CI run.
ci_ok() {
  local sha="$1" repo="Benjaminnhnn/Building-a-safe-and-verifiable-AI-agent-framework-for-infrastructure-operations" result
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    result="$(gh run list --repo "$repo" --workflow ci.yml --commit "$sha" --json conclusion -q '.[].conclusion')" || return 2
  else
    result="$(curl -fsS "https://api.github.com/repos/${repo}/actions/workflows/ci.yml/runs?head_sha=${sha}" \
      | jq -r '.workflow_runs[].conclusion')" || { echo "Cannot query CI: log in with gh auth login" >&2; return 2; }
  fi
  echo "CI for ${sha}: $(printf '%s' "${result:-no runs}" | tr '\n' ' ')"
  [ -n "$result" ] && ! printf '%s\n' "$result" | grep -qvx success
}
