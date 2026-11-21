#!/usr/bin/env bash
# Print the manifest digest that ghcr.io/<repository>:<tag> points to.
#
# Usage: ghcr_tag_digest.sh <repository> <tag>
#
# Asks the registry for exactly that tag (HEAD /v2/<repository>/manifests/<tag>)
# with a pull token minted from GITHUB_ACTOR/GITHUB_TOKEN. Prints the
# Docker-Content-Digest on 200 and "absent" on 404. Any other answer (401, 403,
# 429, 5xx, network or token errors, a 200 without a valid digest) is retried
# and then fails with exit status 1, so callers never mistake an error for a
# missing tag. GHCR_ATTEMPTS and GHCR_RETRY_DELAY (seconds) tune the retries.
set -euo pipefail

repository="$1"
tag="$2"
attempts="${GHCR_ATTEMPTS:-4}"
delay="${GHCR_RETRY_DELAY:-15}"
: "${GITHUB_ACTOR:?}" "${GITHUB_TOKEN:?}"

accept="application/vnd.oci.image.index.v1+json"
accept+=",application/vnd.oci.image.manifest.v1+json"
accept+=",application/vnd.docker.distribution.manifest.list.v2+json"
accept+=",application/vnd.docker.distribution.manifest.v2+json"

for ((attempt = 1; attempt <= attempts; attempt++)); do
  answer="token request failed"
  if token="$(curl -fsS -u "${GITHUB_ACTOR}:${GITHUB_TOKEN}" \
    "https://ghcr.io/token?service=ghcr.io&scope=repository:${repository}:pull" |
    python3 -c 'import json, sys; print(json.load(sys.stdin).get("token", ""))')" && [[ -n "${token}" ]]; then
    # -D - writes the response headers to stdout; the last line is the status.
    if response="$(curl -sS -I -o /dev/null -D - -w '\n%{http_code}' \
      -H "Authorization: Bearer ${token}" -H "Accept: ${accept}" \
      "https://ghcr.io/v2/${repository}/manifests/${tag}")"; then
      status="$(tail -n 1 <<< "${response}")"
      case "${status}" in
        200)
          digest="$(tr -d '\r' <<< "${response}" | awk 'tolower($1) == "docker-content-digest:" { print $2; exit }')"
          if [[ "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
            echo "${digest}"
            exit 0
          fi
          answer="200 without a valid Docker-Content-Digest"
          ;;
        404)
          echo absent
          exit 0
          ;;
        *)
          answer="HTTP ${status}"
          ;;
      esac
    else
      answer="manifest request failed"
    fi
  fi
  echo "attempt ${attempt}/${attempts}: ghcr.io/${repository}:${tag}: ${answer}" >&2
  if ((attempt < attempts)); then
    sleep $((attempt * delay))
  fi
done
echo "::error::registry gave no clear answer for ghcr.io/${repository}:${tag}" >&2
exit 1
