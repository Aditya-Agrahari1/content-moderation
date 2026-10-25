"""Caller identity from X-User-Id / X-Workspace-Id.

This is a test-harness identity mechanism, NOT production authentication. Checks run before
any media lookup, so a 401/403 never reveals whether a media item exists.
"""

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from app import db


@dataclass(frozen=True)
class Caller:
    user_id: str
    workspace_id: str
    role: str
    workspace_verified: bool

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


async def get_caller(
    x_user_id: str | None = Header(default=None),
    x_workspace_id: str | None = Header(default=None),
) -> Caller:
    if not x_user_id or not x_workspace_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    row = await db.pool().fetchrow(
        "SELECT u.role, w.verified FROM users u JOIN workspaces w ON w.id = u.workspace_id "
        "WHERE u.id = $1 AND u.workspace_id = $2",
        x_user_id,
        x_workspace_id,
    )
    if row is None:
        raise HTTPException(status_code=403, detail="Forbidden")
    return Caller(x_user_id, x_workspace_id, row["role"], row["verified"])


async def require_admin(caller: Caller = Depends(get_caller)) -> Caller:
    if not caller.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return caller
