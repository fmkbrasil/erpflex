from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from fastapi import Request

from .db import (
    AccessProfile, AppUser, AuditLog, Branch, Company, ProfilePermission,
    UserBranchAccess, UserCompanyAccess,
)


@dataclass
class AccessContext:
    user: AppUser | None
    company: Company
    branch: Branch | None
    profile: AccessProfile | None
    permissions: set[str]
    companies: list[Company]
    branches: list[Branch]


def auth_configured(db) -> bool:
    if (os.getenv("APP_PASSWORD") or "").strip():
        return True
    return db.scalar(select(AppUser.id).where(AppUser.active.is_(True)).limit(1)) is not None


def current_user(db, request: Request) -> AppUser | None:
    uid = request.session.get("user_id")
    if not uid:
        return None
    user = db.get(AppUser, int(uid))
    if not user or not user.active:
        return None
    return user


def access_context(db, request: Request) -> AccessContext:
    user = current_user(db, request)
    if user:
        accesses = db.execute(
            select(UserCompanyAccess, Company, AccessProfile)
            .join(Company, UserCompanyAccess.company_id == Company.id)
            .join(AccessProfile, UserCompanyAccess.profile_id == AccessProfile.id)
            .where(UserCompanyAccess.user_id == user.id, UserCompanyAccess.active.is_(True), Company.active.is_(True))
            .order_by(Company.name)
        ).all()
        if user.is_superuser:
            companies = db.scalars(select(Company).where(Company.active.is_(True)).order_by(Company.name)).all()
        else:
            companies = [row[1] for row in accesses]
        if not companies:
            fallback = db.scalar(select(Company).where(Company.active.is_(True)).order_by(Company.id).limit(1))
            if not fallback:
                raise RuntimeError("Nenhuma empresa ativa configurada.")
            companies = [fallback]
        allowed_ids = {c.id for c in companies}
        selected = int(request.session.get("company_id") or 0)
        if selected not in allowed_ids:
            selected = companies[0].id
            request.session["company_id"] = selected
        company = next(c for c in companies if c.id == selected)
        profile = None
        if not user.is_superuser:
            match = next((row for row in accesses if row[1].id == selected), None)
            profile = match[2] if match else None
        perms = {"*"} if user.is_superuser else set(
            db.scalars(select(ProfilePermission.permission).where(ProfilePermission.profile_id == profile.id)).all()
        ) if profile else set()
        all_branches = db.scalars(select(Branch).where(Branch.company_id == selected, Branch.active.is_(True)).order_by(Branch.name)).all()
        if user.is_superuser:
            branches = list(all_branches)
        else:
            allowed_branch_ids = set(db.scalars(
                select(UserBranchAccess.branch_id).join(Branch, UserBranchAccess.branch_id == Branch.id)
                .where(UserBranchAccess.user_id == user.id, UserBranchAccess.active.is_(True), Branch.company_id == selected, Branch.active.is_(True))
            ).all())
            branches = [b for b in all_branches if b.id in allowed_branch_ids]
        branch = None
        if branches:
            selected_branch = int(request.session.get("branch_id") or 0)
            branch_ids = {b.id for b in branches}
            if selected_branch not in branch_ids:
                selected_branch = branches[0].id
                request.session["branch_id"] = selected_branch
            branch = next(b for b in branches if b.id == selected_branch)
        return AccessContext(user, company, branch, profile, perms, list(companies), list(branches))

    company = db.scalar(select(Company).where(Company.active.is_(True)).order_by(Company.id).limit(1))
    if not company:
        raise RuntimeError("Nenhuma empresa ativa configurada.")
    branch = db.scalar(select(Branch).where(Branch.company_id == company.id, Branch.active.is_(True)).order_by(Branch.id).limit(1))
    return AccessContext(None, company, branch, None, {"*"}, [company], [branch] if branch else [])


def can(ctx: AccessContext, permission: str) -> bool:
    return "*" in ctx.permissions or permission in ctx.permissions


def add_audit(db, ctx: AccessContext, action: str, entity: str | None = None, entity_id: str | int | None = None, detail: str | None = None):
    db.add(AuditLog(
        company_id=ctx.company.id if ctx.company else None,
        branch_id=ctx.branch.id if ctx.branch else None,
        user_id=ctx.user.id if ctx.user else None,
        action=action,
        entity=entity,
        entity_id=str(entity_id) if entity_id is not None else None,
        detail=detail,
        created_at=datetime.utcnow(),
    ))
