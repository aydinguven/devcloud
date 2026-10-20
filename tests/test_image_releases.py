"""Kubernetes image releases: never overwrite a deploy tag, and keep builds alive while publishing."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.routes.image_release_routes as release_routes
from app.models.deploy_registry_settings import DeployRegistrySettings
from app.models.kubernetes import (
    ImageReleaseStatus,
    K8sApplication,
    K8sProject,
    MlflowImageRelease,
)
from app.models.mlflow_deployment import MlflowModelBuild, MlflowModelBuildStatus
from app.models.model_container_registry_settings import ModelContainerRegistrySettings
from app.models.user import User
from app.orchestrator import image_release_service as service
from app.orchestrator import mlflow_deployment_service as deployment_service
from app.security.secrets import encrypt_secret
from tests.conftest import TestingSessionLocal

MODEL = "PayguardAnomalyUnified"
DEPLOY_REF = "btgvalmbxp1:8525/mbnllmd/mbnllmdml-swiftanomaly:1.0.0"


class FakeMlflowClient:
    def __init__(self):
        self.tags: list[tuple[str, str, str, str]] = []

    async def get_model_version(self, name, version):
        return {"model_version": {"name": name, "version": version, "run_id": "run-3", "source": "s3://m/3"}}

    async def set_model_version_tag(self, name, version, key, value):
        self.tags.append((name, version, key, value))


async def _seed(db, *, auth_source="active_directory") -> tuple[User, K8sApplication]:
    user = User(
        username="k015570",
        email="k015570@test.com",
        hashed_password="x",
        auth_source=auth_source,
    )
    db.add_all(
        [
            user,
            ModelContainerRegistrySettings(
                id=1, enabled=True, registry_url="registry.internal:5000"
            ),
            DeployRegistrySettings(
                id=1,
                enabled=True,
                registry_url="btgvalmbxp1:8525",
                username="aimldeployer",
                encrypted_password=encrypt_secret("nexus-secret"),
                tls_verify=False,
            ),
        ]
    )
    project = K8sProject(rfs_code="mbnllmd", name="Payguard", namespace="mbnllmd")
    db.add(project)
    await db.flush()
    application = K8sApplication(
        project_id=project.id, app_name="mbnllmdml-swiftanomaly", mlflow_model_name=MODEL
    )
    db.add(application)
    await db.commit()
    return user, application


def _ready_build(user: User) -> MlflowModelBuild:
    return MlflowModelBuild(
        user_id=user.id,
        model_name=MODEL,
        model_version="3",
        model_uri=f"models:/{MODEL}/3",
        status=MlflowModelBuildStatus.READY,
        completed_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def fake_registry(monkeypatch):
    """Deploy registry contents keyed by reference, plus every copy made."""
    state = {"tags": {}, "copies": []}

    def digest(*, image_ref, username="", password="", tls_verify=True):
        return state["tags"].get(image_ref)

    def copy(**kwargs):
        state["copies"].append(kwargs)
        state["tags"][kwargs["destination_ref"]] = "sha256:published"

    monkeypatch.setattr(service, "AsyncSessionLocal", TestingSessionLocal)
    monkeypatch.setattr(service, "registry_tag_digest", digest)
    monkeypatch.setattr(service, "copy_registry_image", copy)
    return state


@pytest.mark.asyncio
async def test_publish_copies_ready_build_and_records_digest(db_session, monkeypatch, fake_registry):
    user, application = await _seed(db_session)
    build = _ready_build(user)
    release = MlflowImageRelease(
        user_id=user.id, application_id=application.id, model_name=MODEL, model_version="3", version="1.0.0"
    )
    db_session.add_all([build, release])
    await db_session.commit()
    client = FakeMlflowClient()

    async def fake_client(db, user_id):
        return client

    monkeypatch.setattr(service, "_mlflow_client_for_user", fake_client)

    await service.process_image_release(release.id)

    await db_session.refresh(release)
    assert release.status == ImageReleaseStatus.PUBLISHED, release.error_message
    assert release.image_ref == DEPLOY_REF
    assert release.digest == "sha256:published"
    [copy] = fake_registry["copies"]
    # The ready build is reused: its intermediate tag is the copy source.
    assert copy["source_ref"] == deployment_service._image_tag(build, "registry.internal:5000")
    assert copy["destination_ref"] == DEPLOY_REF
    assert copy["destination_password"] == "nexus-secret"
    assert copy["destination_tls_verify"] is False
    assert release.mlflow_tagged is True
    assert (MODEL, "3", "aifactory.image", DEPLOY_REF) in client.tags


@pytest.mark.asyncio
async def test_publish_refuses_a_tag_that_already_exists(db_session, monkeypatch, fake_registry):
    user, application = await _seed(db_session)
    fake_registry["tags"][DEPLOY_REF] = "sha256:someone-else"
    release = MlflowImageRelease(
        user_id=user.id, application_id=application.id, model_name=MODEL, model_version="3", version="1.0.0"
    )
    db_session.add(release)
    await db_session.commit()

    async def fake_client(db, user_id):
        return FakeMlflowClient()

    monkeypatch.setattr(service, "_mlflow_client_for_user", fake_client)

    await service.process_image_release(release.id)

    await db_session.refresh(release)
    assert release.status == ImageReleaseStatus.FAILED
    assert "zaten var" in release.error_message
    assert fake_registry["copies"] == []
    # It failed before building anything.
    assert release.build_id is None


@pytest.mark.asyncio
async def test_build_is_kept_while_a_release_publishes_it(db_session):
    user, application = await _seed(db_session)
    build = _ready_build(user)
    db_session.add(build)
    await db_session.flush()
    release = MlflowImageRelease(
        user_id=user.id,
        application_id=application.id,
        build_id=build.id,
        model_name=MODEL,
        model_version="3",
        version="1.0.0",
        status=ImageReleaseStatus.PUBLISHING,
    )
    db_session.add(release)
    await db_session.commit()

    assert await deployment_service.enroll_orphaned_model_builds(db_session) == 0
    assert not await deployment_service.garbage_collect_model_build(db_session, build.id)

    # A recent failure keeps the build so a retry does not rebuild it.
    release.status = ImageReleaseStatus.FAILED
    release.completed_at = datetime.now(timezone.utc)
    await db_session.commit()
    assert await deployment_service.enroll_orphaned_model_builds(db_session) == 0

    release.status = ImageReleaseStatus.PUBLISHED
    await db_session.commit()
    assert await deployment_service.enroll_orphaned_model_builds(db_session) == 1


@pytest.mark.asyncio
async def test_failure_after_the_copy_records_the_pushed_tag(db_session, monkeypatch, fake_registry):
    """A pushed tag must never end up as a failed release that blocks its own retry."""
    user, application = await _seed(db_session)
    release = MlflowImageRelease(
        user_id=user.id, application_id=application.id, model_name=MODEL, model_version="3", version="1.0.0"
    )
    db_session.add_all([_ready_build(user), release])
    await db_session.commit()
    client = FakeMlflowClient()

    async def fake_client(db, user_id):
        return client

    monkeypatch.setattr(service, "_mlflow_client_for_user", fake_client)
    digest = service.registry_tag_digest
    reads_after_copy = []

    def flaky_digest(**kwargs):
        if fake_registry["copies"] and not reads_after_copy:
            reads_after_copy.append(kwargs["image_ref"])
            raise RuntimeError("registry timeout")
        return digest(**kwargs)

    monkeypatch.setattr(service, "registry_tag_digest", flaky_digest)

    await service.process_image_release(release.id)

    await db_session.refresh(release)
    assert reads_after_copy == [DEPLOY_REF]
    assert release.status == ImageReleaseStatus.PUBLISHED, release.error_message
    assert release.digest == "sha256:published"
    assert release.mlflow_tagged is True


@pytest.mark.asyncio
async def test_interrupted_publish_adopts_the_tag_it_pushed(db_session, fake_registry):
    user, application = await _seed(db_session)
    release = MlflowImageRelease(
        user_id=user.id,
        application_id=application.id,
        model_name=MODEL,
        model_version="3",
        version="1.0.0",
        image_ref=DEPLOY_REF,
        status=ImageReleaseStatus.PUBLISHING,
        lease_owner="old-controller:1",
        lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db_session.add(release)
    await db_session.commit()
    fake_registry["tags"][DEPLOY_REF] = "sha256:pushed-before-restart"

    await service.recover_interrupted_image_releases()

    await db_session.refresh(release)
    assert release.status == ImageReleaseStatus.PUBLISHED
    assert release.digest == "sha256:pushed-before-restart"


async def _headers(client, username: str) -> tuple[dict[str, str], int]:
    response = await client.post(
        "/api/auth/register",
        json={"username": username, "email": f"{username}@test.com", "password": "Password123!"},
    )
    body = response.json()
    return {"Authorization": f"Bearer {body['access_token']}"}, body["user"]["id"]


@pytest.mark.asyncio
async def test_publish_api_requires_ad_user_and_a_free_version(client, db_session, monkeypatch):
    _, application = await _seed(db_session, auth_source="internal")
    headers, user_id = await _headers(client, "publisher")

    async def fake_client(db, user_id):
        return FakeMlflowClient()

    monkeypatch.setattr(release_routes, "get_mlflow_client", fake_client)
    payload = {"application_id": application.id, "model_name": MODEL, "model_version": "3", "version": "1.0.0"}

    response = await client.post("/api/mlflow/image-releases", json=payload, headers=headers)
    assert response.status_code == 403

    user = await db_session.get(User, user_id)
    user.auth_source = "active_directory"
    await db_session.commit()
    response = await client.post("/api/mlflow/image-releases", json=payload, headers=headers)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == ImageReleaseStatus.QUEUED

    response = await client.post("/api/mlflow/image-releases", json=payload, headers=headers)
    assert response.status_code == 409

    releases = (await db_session.execute(select(MlflowImageRelease))).scalars().all()
    assert [item.version for item in releases] == ["1.0.0"]
