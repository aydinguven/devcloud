import os
import shutil
import zipfile
import logging
from pathlib import Path
from app.models.workspace import Workspace
from app.orchestrator.podman_service import podman_service

logger = logging.getLogger("devcloud.backup")


def create_workspace_zip_backup(storage_path: str | Path, output_zip_path: Path) -> Path:
    """Pack all files from the workspace persistent directory into a .zip archive."""
    src_dir = Path(storage_path)
    if not src_dir.exists():
        src_dir.mkdir(parents=True, exist_ok=True)

    output_zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(src_dir):
            for file in files:
                full_path = Path(root) / file
                rel_path = full_path.relative_to(src_dir)
                # Skip temp/lock/git cache files if huge
                if any(part in {".venv", ".cache", "__pycache__"} for part in rel_path.parts):
                    continue
                try:
                    zipf.write(full_path, arcname=str(rel_path))
                except Exception as exc:
                    logger.warning(f"Skipped archiving {rel_path}: {exc}")
    return output_zip_path


def restore_workspace_from_zip(zip_file_path: Path, target_storage_dir: Path) -> None:
    """Extract files from an uploaded zip archive safely into the workspace storage dir."""
    target_storage_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_file_path, "r") as zipf:
        for member in zipf.infolist():
            # Security guard against ZipSlip vulnerability (relative path traversal)
            filename = member.filename
            if ".." in filename or filename.startswith("/") or filename.startswith("\\"):
                continue
            zipf.extract(member, target_storage_dir)


async def snapshot_workspace_to_image(workspace: Workspace, new_template_id: str) -> tuple[bool, str]:
    """Create a new local Podman image from a running or stopped workspace container."""
    image_tag = f"localhost/devcloud-custom-{new_template_id}:latest"
    success = await podman_service.commit_container(workspace.container_name, image_tag)
    if not success:
        return False, f"Failed to snapshot container {workspace.container_name}"
    return True, image_tag
