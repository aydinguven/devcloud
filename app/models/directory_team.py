from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Where a directory team sits in the organization.
TEAM_STATUS_UNIT = "unit"  # the müdürlük's own team (its MÜDÜR is a member)
TEAM_STATUS_TEAM = "team"  # a team inside a müdürlük
TEAM_STATUS_DIVISION = "division"  # reports straight to the Genel Müdür
TEAM_STATUS_UNASSIGNED = "unassigned"  # no müdürlük; see ``reason``


class DirectoryTeam(Base):
    """One AD ``department`` as placed by the last bulk directory sync.

    The müdürlük of a team is the majority of its AD members' manager chains,
    so it is known even for members whose own chain is broken and for teams
    whose members have never logged in. Every sync replaces all rows.
    """

    __tablename__ = "directory_teams"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    team_key: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    directorate: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    organization_unit: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=TEAM_STATUS_UNASSIGNED)
    # Chain reason for unassigned teams (app.auth.ldap CHAIN_*).
    reason: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Head of the team's müdürlük, or the Genel Müdür for division teams.
    head_username: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    head_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
