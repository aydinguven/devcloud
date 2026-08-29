# Automated Platform Releases

DevCloud uses Git as a small release channel, not as storage for runtime
artifacts. The release workflow builds controller and worker images once,
packages those exact images into verified update and offline bundles, publishes
the images to Quay, uploads the bundles to a GitHub Release, and advances the
machine-managed `stable` branch.

## Workflows

- `.github/workflows/ci.yml` runs the complete Python test suite on
  GitHub-hosted Ubuntu runners for pushes and pull requests targeting `main`.
- `.github/workflows/release-platform.yml` runs only after a manual dispatch
  from `main` or an exact `vMAJOR.MINOR.PATCH` tag. It uses a GitHub-hosted
  Ubuntu runner and performs the build in a privileged, disposable Rocky Linux
  10 container.

The release workflow deliberately does not run for pull requests.

## Release runner

No self-hosted runner registration is required. GitHub creates a fresh
`ubuntu-24.04` runner for each release job. The workflow starts
`rockylinux/rockylinux:10.2` with Docker's `--privileged` option, installs the Rocky build
dependencies there, and runs Podman with the `vfs` storage driver. GitHub CLI
and Git operations that publish the finished artifacts remain on the host.

The standard GitHub-hosted runner has limited temporary storage. The workflow
requires at least 8 GiB free before starting. If future platform images make
the job exceed the standard runner, configure a larger GitHub-hosted runner or
return to a disposable self-hosted release VM.

The job requires outbound HTTPS access to GitHub, Python package indexes, Rocky
repositories, Red Hat UBI registries, Quay, and the PostgreSQL image registry.
The runner and Rocky build container are discarded after the job.

## Repository configuration

Create a GitHub Environment named `release`. Required reviewers are
recommended for production releases. Configure Actions workflow permissions to
allow the repository `GITHUB_TOKEN` to write repository contents and create
releases. GitHub-hosted runners must be enabled for the repository.

Add these environment or repository secrets when Quay publishing is enabled:

- `QUAY_USERNAME`: preferably a repository-scoped robot account;
- `QUAY_PASSWORD`: the corresponding robot token.

Optional release-signing secrets:

- `RELEASE_GPG_PRIVATE_KEY`: ASCII-armored private release key;
- `RELEASE_GPG_KEY_ID`: fingerprint of that key.

The signing key must be usable non-interactively in the release job. The
workflow exports its public key as `devcloud-release-keyring.gpg` alongside
the bundles.

The default Quay destination is
`quay.io/aaslangoren/devcloud`. Change `QUAY_REPOSITORY` in the release
workflow if the repository moves.

## Publishing

For an ordinary commit-specific release, open **Actions**, select
**Release platform bundles**, choose **Run workflow** on `main`, and leave
`release_tag` empty. The generated tag is:

`platform-vVERSION-SHORT_SHA`

For a formal version release, first update `app.__version__`, merge it to
`main`, and push the exact matching tag:

`vMAJOR.MINOR.PATCH`

The workflow rejects a version tag that does not match `app.__version__`.

The workflow publishes:

- immutable and versioned controller and worker tags to Quay;
- one controller-managed platform update bundle;
- one complete server offline bundle;
- one complete worker offline bundle;
- SHA-256 sidecars;
- the release channel descriptor;
- the public GPG keyring when signing is enabled.

Each generated archive is verified before publication. The `stable` branch is
owned by the workflow and contains only `devcloud-update-channel.json`. Do not
edit that branch manually.

## Updating a controller

After a successful release, a connected controller can resolve the channel
from GitHub:

```bash
bash /opt/devcloud/current/deploy/devcloud-setup.sh \
  --yes update \
  --source-type git \
  --repository https://github.com/aydinguven/devcloud.git \
  --ref stable
```

For temporary unsigned development releases, the installed controller must
explicitly allow unsigned updates and the command must add
`--allow-unsigned`. Production releases should enable signing and install the
published public key as `/etc/devcloud/release-keyring.gpg`.

On success, the controller republishes the same platform bundle to enrolled
workers. Workers obtain it through authenticated controller endpoints; they do
not need direct GitHub access.

## Failed or repeated runs

The release upload is rerunnable. Existing assets for the same release tag are
replaced, and `stable` advances only after all builds and verification steps
succeed. If bundle publication succeeds but the channel update fails, rerun the
same workflow after correcting branch permissions.

Each GitHub-hosted run starts clean, so there is no persistent Podman image
store or release workspace to maintain.
