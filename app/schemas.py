from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    display_name: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class FriendCreate(BaseModel):
    user_id: int


class PostCreate(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class PostUpdate(PostCreate):
    pass


class PostOut(BaseModel):
    id: int
    body: str
    author: UserOut
    like_count: int
    liked_by_me: bool
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    message: str

