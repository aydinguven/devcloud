"""Admin API: offline download bundle publisher."""

from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)

from app.auth.dependencies import get_current_admin_user
from app.download_updates import (
    DownloadUpdateDisabled,
    DownloadUpdateInProgress,
    download_update_manager,
)
from app.models.user import User

router = APIRouter()


@router.get("/downloads/status")
async def get_download_update_status(
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Admin: Return durable status for the offline download publisher."""
    return download_update_manager.get_status()


@router.post("/downloads/update", status_code=status.HTTP_202_ACCEPTED)
async def start_download_update(
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Admin: Build and atomically publish the current offline bundle."""
    try:
        return download_update_manager.start()
    except DownloadUpdateDisabled as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except DownloadUpdateInProgress as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.get("/downloads/{bundle_role}/status")
async def get_role_download_update_status(
    bundle_role: Literal["server", "worker"],
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Admin: Return durable status for one offline bundle role."""
    return download_update_manager.get_status(bundle_role)


@router.post(
    "/downloads/{bundle_role}/update",
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_role_download_update(
    bundle_role: Literal["server", "worker"],
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Admin: Build and atomically publish one offline bundle role."""
    try:
        return download_update_manager.start(bundle_role)
    except DownloadUpdateDisabled as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except DownloadUpdateInProgress as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.post("/downloads/clean")
async def clean_old_downloads(
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Admin: Remove older offline bundles and temporary files to reclaim disk space."""
    try:
        return download_update_manager.clean_old_bundles()
    except DownloadUpdateInProgress as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
