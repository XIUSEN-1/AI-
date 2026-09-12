from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.auth import current_user, make_token, verify_password
from app.db import SessionLocal
from app.models import Klass, User

router = APIRouter(prefix="/api/auth", tags=["auth"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class StudentRegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    student_no: str = Field(min_length=1, max_length=32)
    invite_code: str | None = None


class PasswordLoginIn(BaseModel):
    username: str
    password: str


@router.post("/student")
def student_register(body: StudentRegisterIn, db: OrmSession = Depends(get_db)) -> dict:
    class_id = None
    if body.invite_code:
        klass = db.scalar(select(Klass).where(Klass.invite_code == body.invite_code))
        if klass is None:
            raise HTTPException(status_code=400, detail="邀请码无效")
        class_id = klass.id
    user = db.scalar(select(User).where(User.student_no == body.student_no))
    if user is None:
        user = User(name=body.name, student_no=body.student_no, role="student", class_id=class_id)
        db.add(user)
        db.commit()
        db.refresh(user)
    elif user.role != "student":
        raise HTTPException(status_code=400, detail="该学号不是学员账号，请使用账号密码登录")
    return {"token": make_token(user.id, user.role), "user": {"id": user.id, "name": user.name, "role": user.role}}


@router.post("/login")
def password_login(body: PasswordLoginIn, db: OrmSession = Depends(get_db)) -> dict:
    user = db.scalar(select(User).where(User.student_no == body.username))
    if user is None or user.password_hash is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"token": make_token(user.id, user.role), "user": {"id": user.id, "name": user.name, "role": user.role}}


@router.get("/me")
def me(user: dict = Depends(current_user), db: OrmSession = Depends(get_db)) -> dict:
    u = db.get(User, user["id"])
    if u is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    return {"id": u.id, "name": u.name, "role": u.role}
