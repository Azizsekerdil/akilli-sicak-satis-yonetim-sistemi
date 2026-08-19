"""users.is_bootstrap_credential — first-run credential gate

Adds the flag that marks the account created by the first-run bootstrap.

While it is set the account may sign in only from the machine the server runs
on (see ``app.services.auth_service.authenticate``).  It is cleared by the
first successful password change and nothing ever sets it back, so neither an
administrative password reset nor a re-run of the bootstrap can restore
first-run privileges.

Existing installations upgrade to ``False``: those accounts are already in use
with an operator-chosen password, and re-arming a local-only gate on them
would lock working remote sessions out for no security gain.

Revision ID: b1f4c7d92a08
Revises: 5a64e755bb99
Create Date: 2026-08-19
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b1f4c7d92a08"
down_revision: Union[str, None] = "5a64e755bb99"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_bootstrap_credential",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    # The server default exists only so the column can be added NOT NULL to a
    # populated table; the application always supplies the value explicitly.
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.alter_column("is_bootstrap_credential", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("is_bootstrap_credential")
