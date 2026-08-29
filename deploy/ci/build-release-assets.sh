#!/usr/bin/env bash
set -Eeuo pipefail

readonly WORKSPACE=/workspace
readonly ASSET_DIR=/release-assets
readonly RELEASE_VENV=/tmp/devcloud-release-venv
readonly CONTAINER_STORAGE_CONF=/tmp/devcloud-containers-storage.conf

export LANG=C.UTF-8
export PYTHONDONTWRITEBYTECODE=1
export CONTAINERS_STORAGE_CONF="${CONTAINER_STORAGE_CONF}"

required_variables=(
  GITHUB_SHA
  DEVCLOUD_VERSION
  SHORT_SHA
  PLATFORM_FILENAME
  ASSET_BASE_URL
  QUAY_REGISTRY
  QUAY_REPOSITORY
  PUBLISH_QUAY
  SIGN_RELEASE
)
for variable in "${required_variables[@]}"; do
  [[ -n "${!variable:-}" ]] || {
    echo "Release build is missing ${variable}." >&2
    exit 1
  }
done

dnf install -y \
  createrepo_c \
  dnf-plugins-core \
  findutils \
  git \
  gnupg2 \
  gzip \
  pigz \
  podman \
  python3 \
  python3-pip \
  tar

cat > "${CONTAINER_STORAGE_CONF}" <<'EOF'
[storage]
driver = "vfs"
runroot = "/run/containers/storage"
graphroot = "/var/lib/containers/storage"
EOF

dnf download --help >/dev/null
podman info >/dev/null

cd "${WORKSPACE}"
git config --global --add safe.directory "${WORKSPACE}"
git diff --exit-code
git diff --cached --exit-code

python3 -m venv "${RELEASE_VENV}"
"${RELEASE_VENV}/bin/python" -m pip install --disable-pip-version-check --upgrade pip
"${RELEASE_VENV}/bin/python" -m pip install --disable-pip-version-check -r requirements.txt
export PATH="${RELEASE_VENV}/bin:${PATH}"

python -m pytest -q -p no:cacheprovider

signing_key=""
if [[ "${SIGN_RELEASE}" == "true" ]]; then
  [[ -n "${RELEASE_GPG_PRIVATE_KEY:-}" ]] || {
    echo "sign_release requires RELEASE_GPG_PRIVATE_KEY." >&2
    exit 1
  }

  printf '%s' "${RELEASE_GPG_PRIVATE_KEY}" | gpg --batch --import
  signing_key="${RELEASE_GPG_KEY_ID:-}"
  if [[ -z "${signing_key}" ]]; then
    signing_key="$(
      gpg --batch --with-colons --list-secret-keys |
        awk -F: '$1 == "fpr" { print $10; exit }'
    )"
  fi
  [[ -n "${signing_key}" ]] || {
    echo "No imported GPG signing key was found." >&2
    exit 1
  }

  gpg --batch --yes --output "${ASSET_DIR}/devcloud-release-keyring.gpg" --export "${signing_key}"
fi

quay_logged_in=false
cleanup() {
  if [[ "${quay_logged_in}" == "true" ]]; then
    podman logout "${QUAY_REGISTRY}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

bash deploy/container/build-controller-image.sh
bash deploy/container/build-worker-image.sh

if ! podman image exists localhost/devcloud-postgresql:16; then
  podman pull quay.io/sclorg/postgresql-16-c10s:latest
  podman tag quay.io/sclorg/postgresql-16-c10s:latest localhost/devcloud-postgresql:16
fi

if [[ "${PUBLISH_QUAY}" == "true" ]]; then
  [[ -n "${QUAY_USERNAME:-}" && -n "${QUAY_PASSWORD:-}" ]] || {
    echo "Quay publishing requires QUAY_USERNAME and QUAY_PASSWORD." >&2
    exit 1
  }

  printf '%s' "${QUAY_PASSWORD}" |
    podman login --username "${QUAY_USERNAME}" --password-stdin "${QUAY_REGISTRY}"
  quay_logged_in=true

  for role in controller worker; do
    local_image="localhost/devcloud-${role}:${DEVCLOUD_VERSION}"
    for remote_tag in "${role}-${DEVCLOUD_VERSION}" "${role}-${DEVCLOUD_VERSION}-${SHORT_SHA}"; do
      remote_image="${QUAY_REGISTRY}/${QUAY_REPOSITORY}:${remote_tag}"
      podman tag "${local_image}" "${remote_image}"
      podman push "${remote_image}"
    done
  done
fi

signing_arguments=()
if [[ -n "${signing_key}" ]]; then
  signing_arguments=(--signing-key "${signing_key}")
fi

build_arguments=(
  --output-dir "${ASSET_DIR}"
  --controller-source "${QUAY_REGISTRY}/${QUAY_REPOSITORY}:controller-${DEVCLOUD_VERSION}-${SHORT_SHA}"
  --worker-source "${QUAY_REGISTRY}/${QUAY_REPOSITORY}:worker-${DEVCLOUD_VERSION}-${SHORT_SHA}"
  --channel-output "${ASSET_DIR}/devcloud-update-channel.json"
  --channel-url "${ASSET_BASE_URL}/${PLATFORM_FILENAME}"
)
python deploy/build_platform_update.py "${build_arguments[@]}" "${signing_arguments[@]}"

python deploy/package_offline.py \
  --bundle-role server \
  --output-dir "${ASSET_DIR}" \
  --skip-image-build
python deploy/package_offline.py \
  --bundle-role worker \
  --output-dir "${ASSET_DIR}" \
  --skip-image-build

python - "${ASSET_DIR}/${PLATFORM_FILENAME}" "${signing_key}" <<'PY'
import os
import sys
from pathlib import Path

from app.installer.platform import CommandRunner
from app.installer.release import prepare_release
from app.platform_release import load_platform_release

bundle = Path(sys.argv[1])
signing_key = sys.argv[2]
keyring = Path("/release-assets/devcloud-release-keyring.gpg")
with prepare_release(
    bundle,
    runner=CommandRunner(),
    keyring=keyring,
    require_signature=bool(signing_key),
) as prepared:
    release = load_platform_release(prepared.root)
    assert release.version == os.environ["DEVCLOUD_VERSION"]
    assert release.source_commit == os.environ["GITHUB_SHA"]
PY

for role in server worker; do
  if [[ "${role}" == "server" ]]; then
    pattern='devcloud-offline-v*.tar.gz'
  else
    pattern='devcloud-worker-offline-v*.tar.gz'
  fi
  bundle="$(find "${ASSET_DIR}" -maxdepth 1 -type f -name "${pattern}" -print -quit)"
  [[ -n "${bundle}" ]] || {
    echo "Missing ${role} offline bundle." >&2
    exit 1
  }
  verify_root="/tmp/verify-${role}"
  rm -rf -- "${verify_root}"
  install -d -m 0755 "${verify_root}"
  tar -xf "${bundle}" -C "${verify_root}"
  extracted="$(find "${verify_root}" -mindepth 1 -maxdepth 1 -type d -print -quit)"
  python deploy/package_offline.py \
    --verify "${extracted}" \
    --expected-role "${role}" \
    --check-runtime
done

find "${ASSET_DIR}" -maxdepth 1 -type f -print0 |
  sort -z |
  xargs -0 sha256sum
chmod -R a+rX "${ASSET_DIR}"
