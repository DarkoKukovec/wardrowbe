import logging
import os
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.schemas.family import (
    FamilyCreate,
    FamilyCreateResponse,
    FamilyMember,
    FamilyResponse,
    FamilyUpdate,
    FamilyWashingResponse,
    InviteCodeResponse,
    InviteMemberRequest,
    InviteResponse,
    JoinByTokenRequest,
    JoinFamilyRequest,
    JoinFamilyResponse,
    MemberWashingItems,
    MessageResponse,
    PendingInvite,
    UpdateMemberRoleRequest,
)
from app.schemas.item import ItemFilter, ItemListResponse, ItemResponse, LogWashRequest
from app.schemas.notification import EmailConfig
from app.services.family_service import FamilyService
from app.services.item_service import ItemService
from app.services.notification_providers import EmailProvider, build_family_invite_email
from app.utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/families", tags=["Families"])


def require_admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )


def require_family_admin(user: User) -> None:
    if user.family_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="You are not in a family",
        )
    require_admin(user)


@router.get("/me", response_model=FamilyResponse)
async def get_my_family(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> FamilyResponse:
    if current_user.family_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="You are not in a family",
        )

    family_service = FamilyService(db)
    family = await family_service.get_user_family(current_user)

    if family is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Family not found",
        )

    pending_invites = await family_service.get_pending_invites(family)

    return FamilyResponse(
        id=family.id,
        name=family.name,
        invite_code=family.invite_code,
        members=[
            FamilyMember(
                id=m.id,
                display_name=m.display_name,
                email=m.email,
                avatar_url=m.avatar_url,
                role=m.role,
                created_at=m.created_at,
            )
            for m in family.members
            if m.is_active
        ],
        pending_invites=[
            PendingInvite(
                id=i.id,
                email=i.email,
                created_at=i.created_at,
                expires_at=i.expires_at,
            )
            for i in pending_invites
        ],
        created_at=family.created_at,
    )


@router.post("", response_model=FamilyCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_family(
    family_data: FamilyCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> FamilyCreateResponse:
    if current_user.family_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You are already in a family. Leave your current family first.",
        )

    family_service = FamilyService(db)
    family = await family_service.create(current_user, family_data)
    await db.commit()

    return FamilyCreateResponse(
        id=family.id,
        name=family.name,
        invite_code=family.invite_code,
        role="admin",
    )


@router.patch("/me", response_model=FamilyResponse)
async def update_family(
    family_data: FamilyUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> FamilyResponse:
    require_family_admin(current_user)

    family_service = FamilyService(db)
    family = await family_service.get_user_family(current_user)

    if family is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Family not found",
        )

    family = await family_service.update(family, family_data)
    await db.commit()

    pending_invites = await family_service.get_pending_invites(family)

    return FamilyResponse(
        id=family.id,
        name=family.name,
        invite_code=family.invite_code,
        members=[
            FamilyMember(
                id=m.id,
                display_name=m.display_name,
                email=m.email,
                avatar_url=m.avatar_url,
                role=m.role,
                created_at=m.created_at,
            )
            for m in family.members
            if m.is_active
        ],
        pending_invites=[
            PendingInvite(
                id=i.id,
                email=i.email,
                created_at=i.created_at,
                expires_at=i.expires_at,
            )
            for i in pending_invites
        ],
        created_at=family.created_at,
    )


@router.post("/me/regenerate-code", response_model=InviteCodeResponse)
async def regenerate_invite_code(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> InviteCodeResponse:
    require_family_admin(current_user)

    family_service = FamilyService(db)
    family = await family_service.get_user_family(current_user)

    if family is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Family not found",
        )

    new_code = await family_service.regenerate_invite_code(family)
    await db.commit()

    return InviteCodeResponse(invite_code=new_code)


@router.post("/join", response_model=JoinFamilyResponse)
async def join_family(
    request: JoinFamilyRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> JoinFamilyResponse:
    if current_user.family_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You are already in a family. Leave your current family first.",
        )

    family_service = FamilyService(db)
    family = await family_service.join_family(current_user, request.invite_code)

    if family is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invalid invite code",
        )

    await db.commit()

    return JoinFamilyResponse(
        family_id=family.id,
        family_name=family.name,
        role="member",
    )


@router.post("/join-by-token", response_model=JoinFamilyResponse)
async def join_family_by_token(
    request: JoinByTokenRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> JoinFamilyResponse:
    if current_user.family_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You are already in a family. Leave your current family first.",
        )

    family_service = FamilyService(db)
    invite = await family_service.get_invite_by_token(request.token)

    if invite is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invalid or expired invite link",
        )

    if invite.email.lower() != current_user.email.lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This invite was sent to a different email address",
        )

    family = await family_service.accept_invite_by_token(invite, current_user)
    await db.commit()

    if family is None:
        logger.error("Family %s not found after accepting invite %s", invite.family_id, invite.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Family not found",
        )

    return JoinFamilyResponse(
        family_id=family.id,
        family_name=family.name,
        role=current_user.role,
    )


@router.post("/me/leave", response_model=MessageResponse)
async def leave_family(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MessageResponse:
    if current_user.family_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="You are not in a family",
        )

    family_service = FamilyService(db)
    success = await family_service.leave_family(current_user)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot leave: you are the only admin. Transfer admin role first or remove all other members.",
        )

    await db.commit()
    return MessageResponse(message="Left family successfully")


@router.post("/me/invite", response_model=InviteResponse, status_code=status.HTTP_201_CREATED)
async def invite_member(
    invite_data: InviteMemberRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> InviteResponse:
    require_family_admin(current_user)

    family_service = FamilyService(db)
    family = await family_service.get_user_family(current_user)

    if family is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Family not found",
        )

    invite = await family_service.create_invite(family, current_user, invite_data)
    await db.commit()

    app_url = os.getenv("APP_URL", "http://localhost:3000")
    provider = EmailProvider(EmailConfig(address=invite.email))
    if provider.is_configured():
        email = build_family_invite_email(
            to=invite.email,
            family_name=family.name,
            inviter_name=current_user.display_name,
            invite_token=invite.token,
            app_url=app_url,
        )
        result = await provider.send(email)
        if not result.get("success"):
            logger.warning("Failed to send family invite email: %s", result.get("error"))

    return InviteResponse(
        id=invite.id,
        email=invite.email,
        expires_at=invite.expires_at,
    )


@router.delete("/me/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_invite(
    invite_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    require_family_admin(current_user)

    family_service = FamilyService(db)
    invite = await family_service.get_invite_by_id(invite_id)

    if invite is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invite not found",
        )

    # Verify invite belongs to user's family
    if invite.family_id != current_user.family_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invite not found",
        )

    await family_service.cancel_invite(invite)
    await db.commit()


@router.patch("/me/members/{member_id}", response_model=FamilyMember)
async def update_member_role(
    member_id: UUID,
    request: UpdateMemberRoleRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> FamilyMember:
    require_family_admin(current_user)

    if member_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot change your own role",
        )

    family_service = FamilyService(db)
    family = await family_service.get_user_family(current_user)

    if family is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Family not found",
        )

    member = await family_service.update_member_role(family, member_id, request.role)

    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found in your family",
        )

    await db.commit()

    return FamilyMember(
        id=member.id,
        display_name=member.display_name,
        email=member.email,
        avatar_url=member.avatar_url,
        role=member.role,
        created_at=member.created_at,
    )


@router.delete("/me/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    member_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    require_family_admin(current_user)

    if member_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove yourself. Use /leave instead.",
        )

    family_service = FamilyService(db)
    family = await family_service.get_user_family(current_user)

    if family is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Family not found",
        )

    success = await family_service.remove_member(family, member_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found in your family",
        )

    await db.commit()


@router.get("/me/members/{member_id}/items", response_model=ItemListResponse)
async def get_family_member_items(
    member_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    type: str | None = None,
    subtype: str | None = None,
    colors: str | None = None,
    item_status: str | None = Query(None, alias="status"),
    favorite: bool | None = None,
    needs_wash: bool | None = None,
    is_archived: bool = False,
    search: str | None = None,
    sort_by: str | None = None,
    sort_order: str = "desc",
) -> ItemListResponse:
    if current_user.family_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="You are not in a family",
        )

    family_service = FamilyService(db)
    family = await family_service.get_user_family(current_user)

    if family is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Family not found",
        )

    # Verify the requested member belongs to the same family
    member = next((m for m in family.members if m.id == member_id and m.is_active), None)
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found in your family",
        )

    color_list = colors.split(",") if colors else None

    filters = ItemFilter(
        type=type,
        subtype=subtype,
        colors=color_list,
        status=item_status,
        favorite=favorite,
        needs_wash=needs_wash,
        is_archived=is_archived,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    item_service = ItemService(db)
    items, total = await item_service.get_list(
        user_id=member_id,
        filters=filters,
        page=page,
        page_size=page_size,
    )

    return ItemListResponse(
        items=[ItemResponse.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


@router.get("/me/washing", response_model=FamilyWashingResponse)
async def get_family_washing(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> FamilyWashingResponse:
    if current_user.family_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="You are not in a family",
        )

    family_service = FamilyService(db)
    family = await family_service.get_user_family(current_user)

    if family is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Family not found",
        )

    item_service = ItemService(db)
    wash_filters = ItemFilter(needs_wash=True, is_archived=False)

    member_washing_list: list[MemberWashingItems] = []
    total = 0

    for member in family.members:
        if not member.is_active:
            continue

        items, count = await item_service.get_list(
            user_id=member.id,
            filters=wash_filters,
            page=1,
            page_size=100,
        )

        if count > 0:
            member_washing_list.append(
                MemberWashingItems(
                    member_id=member.id,
                    member_name=member.display_name,
                    member_avatar_url=member.avatar_url,
                    items=[ItemResponse.model_validate(item) for item in items],
                )
            )
            total += count

    return FamilyWashingResponse(members=member_washing_list, total=total)


@router.post("/me/members/{member_id}/items/{item_id}/wash", response_model=ItemResponse)
async def log_family_member_item_wash(
    member_id: UUID,
    item_id: UUID,
    request: LogWashRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ItemResponse:
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    if current_user.family_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="You are not in a family",
        )

    family_service = FamilyService(db)
    family = await family_service.get_user_family(current_user)

    if family is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Family not found",
        )

    member = next((m for m in family.members if m.id == member_id and m.is_active), None)
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found in your family",
        )

    item_service = ItemService(db)
    item = await item_service.get_by_id(item_id, member_id)

    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Item not found",
        )

    if item.wears_since_wash == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Item is already clean (0 wears since last wash)",
        )

    if request.washed_at is None:
        try:
            user_tz = ZoneInfo(current_user.timezone or "UTC")
        except Exception:
            user_tz = ZoneInfo("UTC")
        washed_at = datetime.now(UTC).astimezone(user_tz).date()
    else:
        washed_at = request.washed_at

    await item_service.log_wash(
        item=item,
        washed_at=washed_at,
        method=request.method,
        notes=request.notes,
    )

    await db.refresh(item)
    return ItemResponse.model_validate(item)
