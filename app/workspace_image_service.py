"""Normalize registry and uploaded workspace images into managed OCI archives."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from app.config import settings


class WorkspaceImageError(RuntimeError):
    pass


def image_storage_root() -> Path:
    return Path(settings.WORKSPACE_IMAGES_ROOT).resolve()


def image_archive_path(filename: str) -> Path:
    if not filename or Path(filename).name != filename:
        raise WorkspaceImageError("Invalid workspace image filename")
    root = image_storage_root()
    path = (root / filename).resolve()
    if path.parent != root:
        raise WorkspaceImageError("Workspace image path escapes its storage root")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_skopeo(arguments: list[str], *, environment: dict[str, str] | None = None) -> str:
    command = [settings.SKOPEO_BIN, *arguments]
    try:
        result = subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=True,
            timeout=settings.WORKSPACE_IMAGE_IMPORT_TIMEOUT_SECONDS,
            env={**os.environ, **(environment or {})},
        )
    except FileNotFoundError as exc:
        raise WorkspaceImageError(
            "skopeo is not installed in the controller runtime"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceImageError("Workspace image import timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise WorkspaceImageError(detail or "skopeo could not import the image") from exc
    return result.stdout


def _registry_auth_environment(
    source_ref: str, username: str, password: str
) -> tuple[dict[str, str], Path | None]:
    if not username and not password:
        return {}, None
    if not username or not password:
        raise WorkspaceImageError("Registry username and password must be supplied together")
    plain_ref = source_ref.removeprefix("docker://")
    registry = plain_ref.split("/", 1)[0]
    if not registry or registry in {".", ".."}:
        raise WorkspaceImageError("Registry reference has no valid registry host")
    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    handle = tempfile.NamedTemporaryFile(
        mode="w", prefix="devcloud-registry-auth-", suffix=".json", delete=False
    )
    auth_path = Path(handle.name)
    try:
        json.dump({"auths": {registry: {"auth": encoded}}}, handle)
        handle.close()
        os.chmod(auth_path, 0o600)
    except Exception:
        handle.close()
        auth_path.unlink(missing_ok=True)
        raise
    return {"REGISTRY_AUTH_FILE": str(auth_path)}, auth_path


def _inspect_archive(path: Path) -> dict:
    output = _run_skopeo(["inspect", f"oci-archive:{path}"])
    try:
        metadata = json.loads(output)
    except ValueError as exc:
        raise WorkspaceImageError("skopeo returned invalid image metadata") from exc
    if str(metadata.get("Os") or "linux").lower() != "linux":
        raise WorkspaceImageError("Only Linux workspace images are supported")
    architecture = str(metadata.get("Architecture") or "").lower()
    if architecture not in {"amd64", "x86_64"}:
        raise WorkspaceImageError(
            f"Only linux/amd64 workspace images are supported; received {architecture or 'unknown'}"
        )
    return metadata


def _normalize(
    *,
    source: str,
    image_ref: str,
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    root = image_storage_root()
    root.mkdir(parents=True, exist_ok=True)
    image_id = str(uuid.uuid4())
    filename = f"{image_id}.tar"
    destination = image_archive_path(filename)
    temporary = root / f".{image_id}.partial"
    try:
        _run_skopeo(
            [
                "copy",
                "--remove-signatures",
                "--override-os",
                "linux",
                "--override-arch",
                "amd64",
                source,
                f"oci-archive:{temporary}:{image_ref}",
            ],
            environment=environment,
        )
        metadata = _inspect_archive(temporary)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "id": image_id,
        "image_ref": image_ref,
        "digest": str(metadata.get("Digest") or ""),
        "sha256": sha256_file(destination),
        "filename": filename,
        "size": destination.stat().st_size,
        "architecture": "amd64",
    }


def import_registry_image(
    *,
    image_ref: str,
    source_ref: str,
    username: str = "",
    password: str = "",
) -> dict[str, object]:
    normalized = source_ref.strip()
    if not normalized:
        raise WorkspaceImageError("Registry image reference is required")
    if "://" in normalized and not normalized.startswith("docker://"):
        raise WorkspaceImageError("Only docker:// registry references are supported")
    source = normalized if normalized.startswith("docker://") else f"docker://{normalized}"
    environment, auth_path = _registry_auth_environment(source, username, password)
    try:
        return _normalize(
            source=source,
            image_ref=image_ref,
            environment=environment,
        )
    finally:
        if auth_path:
            auth_path.unlink(missing_ok=True)


def import_uploaded_archive(*, image_ref: str, upload_path: Path) -> dict[str, object]:
    errors: list[str] = []
    for transport in ("oci-archive", "docker-archive"):
        try:
            return _normalize(
                source=f"{transport}:{upload_path}",
                image_ref=image_ref,
            )
        except WorkspaceImageError as exc:
            errors.append(str(exc))
    raise WorkspaceImageError(
        "Uploaded file is not a supported OCI or Docker image archive: "
        + "; ".join(errors[-2:])
    )
