-- CSCE 553 M6 · Campus Pulse sharding
-- shard_schema.sql: run this SAME file on EVERY shard (Steps 4 and 9).
-- Adapted from the handout sharding_setup.sql (Part A + social-media appendix).
-- Everything lives in schema m6 so the M1/M2 tables in public stay untouched.

create schema if not exists m6;

-- 1. The bucket rule: identical on every database (handout, unchanged)
create or replace function m6.shard_bucket(u uuid)
returns int language sql immutable as $$
  select (('x' || substr(md5(u::text), 1, 8))::bit(32)::bigint % 64)::int
$$;

-- 2. Owner table. Every id is a uuid; the users_belong_here check is added
--    per shard by shard_check.sql (Step 5).
create table if not exists m6.users (
  id            uuid primary key,
  email         text not null,             -- unique system-wide via the directory
  display_name  text not null,
  password_hash text not null,
  created_at    timestamptz not null default now()
);

-- Sessions live on the user's shard (the token itself carries the shard name).
create table if not exists m6.sessions (
  token_hash text primary key,
  user_id    uuid not null references m6.users(id) on delete cascade,
  expires_at timestamptz not null,
  created_at timestamptz not null default now()
);
create index if not exists sessions_user on m6.sessions (user_id);

-- 3. Posts live on the AUTHOR's shard. The tsvector + GIN index is the
--    real-time search (inverted index) that every insert must now maintain.
create table if not exists m6.posts (
  id         uuid primary key,
  author_id  uuid not null references m6.users(id),
  body       text not null,
  search     tsvector generated always as (to_tsvector('english', body)) stored,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (author_id, id)
);
create index if not exists posts_author_created on m6.posts (author_id, created_at desc);
create index if not exists posts_search on m6.posts using gin (search);

-- Hashtags extracted from the body: child rows carry the owner (author_id).
create table if not exists m6.post_tags (
  author_id uuid not null,
  post_id   uuid not null,
  tag       text not null,
  primary key (post_id, tag),
  foreign key (author_id, post_id) references m6.posts (author_id, id) on delete cascade
);
create index if not exists post_tags_tag on m6.post_tags (tag);

-- 4. Likes live WITH THE POST (author's shard), one row per like, no counter.
create table if not exists m6.likes (
  author_id  uuid not null,                 -- routing column (post's author)
  post_id    uuid not null,
  liker_id   uuid not null,                 -- may live on another shard: no FK
  created_at timestamptz not null default now(),
  primary key (post_id, liker_id),
  foreign key (author_id, post_id) references m6.posts (author_id, id) on delete cascade
);

-- 5. A follow touches two owners: one row on each side (handout appendix).
create table if not exists m6.following (     -- on the follower's shard
  follower_id uuid not null references m6.users(id),
  followee_id uuid not null,
  created_at  timestamptz not null default now(),
  primary key (follower_id, followee_id)
);
create table if not exists m6.followers (     -- on the followee's shard
  followee_id uuid not null references m6.users(id),
  follower_id uuid not null,
  created_at  timestamptz not null default now(),
  primary key (followee_id, follower_id)
);

-- 6. Home feed, copy at write time: a small row on the READER's shard.
create table if not exists m6.feed_items (
  owner_id   uuid not null references m6.users(id) on delete cascade,
  created_at timestamptz not null,
  post_id    uuid not null,
  author_id  uuid not null,
  preview    text not null,
  primary key (owner_id, created_at, post_id)
);

-- 7. Publish: one local transaction on the author's shard.
--    Inserts the post, its tags, and feed copies for the author and for
--    every follower who lives on THIS shard; returns followers on other shards.
create or replace function m6.publish_post(
    p_post uuid, p_author uuid, p_body text, p_tags text[],
    out post_created_at timestamptz, out remote_followers uuid[])
language plpgsql as $$
begin
  post_created_at := clock_timestamp();
  insert into m6.posts (id, author_id, body, created_at, updated_at)
  values (p_post, p_author, p_body, post_created_at, post_created_at);

  insert into m6.post_tags (author_id, post_id, tag)
  select p_author, p_post, t from unnest(p_tags) as t
  on conflict do nothing;

  insert into m6.feed_items (owner_id, created_at, post_id, author_id, preview)
  select x.owner, post_created_at, p_post, p_author, left(p_body, 80)
  from (select p_author as owner
        union
        select f.follower_id from m6.followers f where f.followee_id = p_author) x
  where exists (select 1 from m6.users u where u.id = x.owner);

  select coalesce(array_agg(f.follower_id), '{}') into remote_followers
  from m6.followers f
  where f.followee_id = p_author
    and not exists (select 1 from m6.users u where u.id = f.follower_id);
end $$;

-- Copy written second, on each remote follower's shard; safe to retry.
create or replace function m6.fanout(
    p_post uuid, p_author uuid, p_preview text, p_created timestamptz, p_owners uuid[])
returns int language plpgsql as $$
declare n int;
begin
  insert into m6.feed_items (owner_id, created_at, post_id, author_id, preview)
  select o, p_created, p_post, p_author, p_preview
  from unnest(p_owners) as o
  where exists (select 1 from m6.users u where u.id = o)
  on conflict do nothing;
  get diagnostics n = row_count;
  return n;
end $$;
