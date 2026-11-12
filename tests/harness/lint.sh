#!/usr/bin/env bash
#
# Runs the three linters of CLAUDE.md section 6 through pinned images, because none is installed
# locally. Exit code 0 only if all of them pass. Needs Docker; the first run pulls the images.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly SHELLCHECK_IMAGE="koalaman/shellcheck:v0.10.0"
readonly YAMLLINT_IMAGE="cytopia/yamllint:1.26"
readonly HADOLINT_IMAGE="hadolint/hadolint:v2.12.0"
readonly YAMLLINT_RULES='{extends: default, rules: {document-start: disable, line-length: {max: 130}, truthy: {check-keys: false}, comments: {min-spaces-from-content: 1}, indentation: {indent-sequences: consistent}}}'

status=0

shell_files=(
  hermes_webui/rootfs/etc/profile.d/hermes.sh
  hermes_webui/rootfs/etc/s6-overlay/scripts/hermes-init.sh
  hermes_webui/rootfs/etc/s6-overlay/scripts/hermes-stop.sh
  hermes_webui/rootfs/etc/s6-overlay/s6-rc.d/hermes-gateway/finish
  hermes_webui/rootfs/etc/s6-overlay/s6-rc.d/hermes-gateway/run
  hermes_webui/rootfs/etc/s6-overlay/s6-rc.d/hermes-webui/run
  hermes_webui/rootfs/etc/s6-overlay/s6-rc.d/nginx/run
  hermes_webui/rootfs/opt/hermes-app/bin/claude
  hermes_webui/rootfs/opt/hermes-app/bin/hermes
  hermes_webui/rootfs/opt/hermes-app/libexec-hermes
  tests/gate.sh
  tests/harness/lint.sh
  tests/testbed/ha-testbed.sh
  tests/testbed/pi-gate.sh
)
yaml_files=(
  repository.yaml
  hermes_webui/config.yaml
  hermes_webui/managed-config.yaml
  hermes_webui/translations/en.yaml
)

printf 'shellcheck (%s), %d files\n' "${SHELLCHECK_IMAGE}" "${#shell_files[@]}"
docker run --rm --network none --mount "type=bind,source=${REPO_ROOT},target=/mnt,readonly" --workdir /mnt \
  "${SHELLCHECK_IMAGE}" "${shell_files[@]}" || status=1

printf 'yamllint (%s), %d files\n' "${YAMLLINT_IMAGE}" "${#yaml_files[@]}"
docker run --rm --network none --mount "type=bind,source=${REPO_ROOT},target=/data,readonly" --workdir /data \
  "${YAMLLINT_IMAGE}" -d "${YAMLLINT_RULES}" "${yaml_files[@]}" || status=1

printf 'hadolint (%s)\n' "${HADOLINT_IMAGE}"
docker run --rm --network none -i "${HADOLINT_IMAGE}" hadolint - <"${REPO_ROOT}/hermes_webui/Dockerfile" || status=1

if ((status == 0)); then
  printf 'All linters passed.\n'
else
  printf 'A linter reported findings.\n' >&2
fi
exit "${status}"
