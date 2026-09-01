import pytest
from httpx import AsyncClient
from app.models.user import User, UserRole
from app.models.custom_template import CustomTemplate
from app.orchestrator.templates import resolve_template, get_template


async def get_admin_headers(client: AsyncClient, db_session) -> dict[str, str]:
    resp = await client.post(
        "/api/auth/register",
        json={
            "username": "admin_tpl_user",
            "email": "admin_tpl@test.com",
            "password": "Password123!",
            "full_name": "Admin Tpl",
        },
    )
    data = resp.json()
    token = data["access_token"]
    user_id = data["user"]["id"]

    # Promote to admin
    user = await db_session.get(User, user_id)
    user.role = UserRole.ADMIN
    db_session.add(user)
    await db_session.commit()

    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_custom_template_lifecycle(client: AsyncClient, db_session):
    headers = await get_admin_headers(client, db_session)

    # 1. Insert custom template directly into DB
    ct = CustomTemplate(
        id="custom-rust-web",
        name="Rust Web Backend",
        description="Actix-web & SQLx setup",
        category="Rust",
        icon="rust",
        image_tag="localhost/devcloud-custom-rust-web:latest",
        default_port=8080,
        ide_type="vscode",
        containerfile="FROM codercom/code-server\nRUN cargo install",
        is_ready=True,
    )
    db_session.add(ct)
    await db_session.commit()

    # 2. List templates via admin API
    res = await client.get("/api/admin/templates", headers=headers)
    assert res.status_code == 200
    tpls = res.json()
    assert any(t["id"] == "custom-rust-web" for t in tpls)

    # 3. Test resolve_template helper
    resolved = await resolve_template(db_session, "custom-rust-web")
    assert resolved is not None
    assert resolved.name == "Rust Web Backend"
    assert resolved.default_port == 8080
    assert resolved.ide_type == "vscode"

    # 4. Delete custom template
    del_res = await client.delete("/api/admin/templates/custom-rust-web", headers=headers)
    assert del_res.status_code == 200
