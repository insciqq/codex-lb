#!/usr/bin/env bash
# Point ghcr.io/<repository>:latest at the published image of a release.
# Used by .github/workflows/fork-image-latest.yml.
#
# Usage: ghcr_point_latest.sh <repository> <full commit SHA> <version> [<expected digest>]
#
# The digest is read from the existing sha-<commit> tag; nothing is built. The
# <version> tag must name the same digest, and so must <expected digest> when
# given. A missing tag, a mismatch or any registry lookup error
# (.github/scripts/ghcr_tag_digest.sh fails closed) exits with status 1 before
# :latest is touched. Unlike <version> and sha-<commit>, :latest is moved.
set -euo pipefail

repository="$1"
sha="$2"
version="$3"
expected="${4:-}"
here="$(dirname "${BASH_SOURCE[0]}")"
image="ghcr.io/${repository}"

if [[ ! "${sha}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::not a full commit SHA: '${sha}'" >&2
  exit 1
fi

digest="$(bash "${here}/ghcr_tag_digest.sh" "${repository}" "sha-${sha}")"
if [[ "${digest}" == absent ]]; then
  echo "::error::${image}:sha-${sha} is not published; latest only names a published release" >&2
  exit 1
fi
versioned="$(bash "${here}/ghcr_tag_digest.sh" "${repository}" "${version}")"
if [[ "${versioned}" != "${digest}" ]]; then
  echo "::error::${image}:${version} names ${versioned}, but ${image}:sha-${sha} names ${digest}" >&2
  exit 1
fi
if [[ -n "${expected}" && "${expected}" != "${digest}" ]]; then
  echo "::error::${image}:sha-${sha} names ${digest}, expected ${expected}" >&2
  exit 1
fi

current="$(bash "${here}/ghcr_tag_digest.sh" "${repository}" latest)"
if [[ "${current}" == "${digest}" ]]; then
  echo "${image}:latest already points to ${digest}"
  exit 0
fi
docker buildx imagetools create --tag "${image}:latest" "${image}@${digest}"
current="$(bash "${here}/ghcr_tag_digest.sh" "${repository}" latest)"
if [[ "${current}" != "${digest}" ]]; then
  echo "::error::${image}:latest points to ${current} after tagging, expected ${digest}" >&2
  exit 1
fi
echo "tagged ${image}:latest -> ${digest} (${version})"
