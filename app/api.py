from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession, joinedload

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.security import HTTPAuthorizationCredentials

from app.auth import bearer, create_session, get_current_user, hash_token, password_hash
from app.database import get_db
from app.models import Friendship, Like, Post, Session, User
from app.schemas import (
    FriendCreate,
    LoginRequest,
    MessageOut,
    PostCreate,
    PostOut,
    PostUpdate,
    TokenOut,
    UserCreate,
    UserOut,
)


router = APIRouter(prefix="/api")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def post_to_schema(post: Post, current_user_id: int) -> PostOut:
    return PostOut(
        id=post.id,
        body=post.body,
        author=UserOut.model_validate(post.author),
        like_count=len(post.likes),
        liked_by_me=any(like.user_id == current_user_id for like in post.likes),
        created_at=post.created_at,
        updated_at=post.updated_at,
    )


@router.post("/auth/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: DbSession = Depends(get_db)):
    email = normalize_email(str(payload.email))
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=409, detail="An account with that email already exists")
    user = User(
        email=email,
        display_name=payload.display_name.strip(),
        password_hash=password_hash.hash(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_session(db, user)
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


@router.post("/auth/login", response_model=TokenOut)
def login(payload: LoginRequest, db: DbSession = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == normalize_email(str(payload.email))))
    if user is None or not password_hash.verify(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    token = create_session(db, user)
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


@router.post("/auth/logout", response_model=MessageOut)
def logout(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
    current_user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    del current_user
    record = db.scalar(select(Session).where(Session.token_hash == hash_token(credentials.credentials)))
    if record:
        db.delete(record)
        db.commit()
    return MessageOut(message="Logged out")


@router.get("/users", response_model=list[UserOut])
def search_users(
    q: str = Query(default="", max_length=100),
    current_user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    statement = select(User).where(User.id != current_user.id)
    if q.strip():
        pattern = f"%{q.strip()}%"
        statement = statement.where(or_(User.display_name.ilike(pattern), User.email.ilike(pattern)))
    return list(db.scalars(statement.order_by(User.display_name).limit(50)))


@router.post("/friends", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def add_friend(
    payload: FriendCreate,
    current_user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    if payload.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot add yourself as a friend")
    friend = db.get(User, payload.user_id)
    if friend is None:
        raise HTTPException(status_code=404, detail="User not found")
    db.add(Friendship(user_id=current_user.id, friend_id=friend.id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="User is already your friend")
    return friend


@router.get("/friends", response_model=list[UserOut])
def list_friends(
    current_user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    statement = (
        select(User)
        .join(Friendship, Friendship.friend_id == User.id)
        .where(Friendship.user_id == current_user.id)
        .order_by(User.display_name)
    )
    return list(db.scalars(statement))


@router.post("/messages", response_model=PostOut, status_code=status.HTTP_201_CREATED)
def create_post(
    payload: PostCreate,
    current_user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    post = Post(author_id=current_user.id, body=payload.body.strip())
    db.add(post)
    db.commit()
    statement = select(Post).options(joinedload(Post.author), joinedload(Post.likes)).where(Post.id == post.id)
    post = db.execute(statement).unique().scalar_one()
    return post_to_schema(post, current_user.id)


@router.get("/messages", response_model=list[PostOut])
def list_posts(
    current_user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    statement = select(Post).options(joinedload(Post.author), joinedload(Post.likes)).order_by(Post.created_at.desc()).limit(100)
    posts = db.execute(statement).unique().scalars().all()
    return [post_to_schema(post, current_user.id) for post in posts]


@router.patch("/messages/{post_id}", response_model=PostOut)
def update_post(
    post_id: int,
    payload: PostUpdate,
    current_user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own posts")
    post.body = payload.body.strip()
    db.commit()
    statement = select(Post).options(joinedload(Post.author), joinedload(Post.likes)).where(Post.id == post.id)
    post = db.execute(statement).unique().scalar_one()
    return post_to_schema(post, current_user.id)


@router.post("/messages/{post_id}/like", response_model=PostOut)
def like_post(
    post_id: int,
    current_user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    if db.get(Post, post_id) is None:
        raise HTTPException(status_code=404, detail="Post not found")
    existing = db.scalar(select(Like).where(Like.post_id == post_id, Like.user_id == current_user.id))
    if existing is None:
        db.add(Like(post_id=post_id, user_id=current_user.id))
        db.commit()
    statement = select(Post).options(joinedload(Post.author), joinedload(Post.likes)).where(Post.id == post_id)
    post = db.execute(statement).unique().scalar_one()
    return post_to_schema(post, current_user.id)

