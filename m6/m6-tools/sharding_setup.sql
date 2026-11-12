-- CSCE 553 · Scaling writes: handout SQL (tested on Postgres)
-- Part A: run on EVERY shard (shard_0 and shard_1)

-- Run this same file on EVERY shard (Supabase > SQL Editor)

-- 1. The bucket rule: identical on every database
CREATE OR REPLACE FUNCTION shard_bucket(u uuid)
RETURNS int LANGUAGE sql IMMUTABLE AS $$
  SELECT (('x' || substr(md5(u::text), 1, 8))::bit(32)::bigint % 64)::int
$$;

-- 2. Tables (same shape on every shard, different rows)
CREATE TABLE users (
  id              uuid PRIMARY KEY,
  email           text NOT NULL,
  hashed_password text NOT NULL,
  account_type    text NOT NULL DEFAULT 'customer',
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE products (            -- full copy on every shard
  id          uuid PRIMARY KEY,
  name        text NOT NULL,
  description text,
  price       numeric(10,2) NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cart_items (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES users(id),
  product_id uuid NOT NULL REFERENCES products(id),
  quantity   int  NOT NULL CHECK (quantity > 0),
  UNIQUE (user_id, product_id)
);

CREATE TABLE orders (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL REFERENCES users(id),
  total_amount numeric(10,2) NOT NULL DEFAULT 0,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, id)
);

CREATE TABLE order_items (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL,
  order_id   uuid NOT NULL,
  product_id uuid NOT NULL REFERENCES products(id),
  quantity   int  NOT NULL,
  price      numeric(10,2) NOT NULL,
  FOREIGN KEY (user_id, order_id) REFERENCES orders(user_id, id)
);

CREATE INDEX ON orders (user_id, created_at DESC);
CREATE INDEX ON order_items (user_id, order_id);

-- 3. Checkout: one local transaction on the user's shard
CREATE OR REPLACE FUNCTION checkout(p_user uuid)
RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE v_order uuid := gen_random_uuid();
BEGIN
  PERFORM 1 FROM cart_items WHERE user_id = p_user FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'cart is empty'; END IF;

  INSERT INTO orders (id, user_id) VALUES (v_order, p_user);

  INSERT INTO order_items (user_id, order_id, product_id, quantity, price)
  SELECT p_user, v_order, c.product_id, c.quantity, p.price
  FROM cart_items c JOIN products p ON p.id = c.product_id
  WHERE c.user_id = p_user;

  UPDATE orders SET total_amount =
    (SELECT sum(quantity * price) FROM order_items WHERE order_id = v_order)
  WHERE id = v_order;

  DELETE FROM cart_items WHERE user_id = p_user;
  RETURN v_order;
END $$;

-- Part B: run on shard_0 ONLY
ALTER TABLE users ADD CONSTRAINT users_belong_here
  CHECK (shard_bucket(id) BETWEEN 0 AND 31);

-- Part C: run on shard_1 ONLY
ALTER TABLE users ADD CONSTRAINT users_belong_here
  CHECK (shard_bucket(id) BETWEEN 32 AND 63);

-- Part D: run on shard_0 ONLY (directory; shard_bucket already exists there)

CREATE TABLE user_directory (
  email   text PRIMARY KEY,            -- store lower-cased
  user_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid()
);

CREATE TABLE bucket_map (
  bucket int  PRIMARY KEY CHECK (bucket BETWEEN 0 AND 63),
  shard  text NOT NULL
);

INSERT INTO bucket_map (bucket, shard)
SELECT b, CASE WHEN b < 32 THEN 'shard_0' ELSE 'shard_1' END
FROM generate_series(0, 63) AS b;

-- Part E: products, run the SAME statement on every shard
INSERT INTO products (id, name, price)
VALUES ('7b0c2f6e-3a52-4f7e-9c11-2d4e8a6b9f01', 'Blue Trail Backpack', 49.00)
ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, price = EXCLUDED.price;

-- Part F: checks
-- on each shard:   SELECT count(*) FROM users;
-- on shard_0:      SELECT m.shard, count(*) FROM user_directory d
--                  JOIN bucket_map m ON m.bucket = shard_bucket(d.user_id) GROUP BY m.shard;
-- wrong-shard rows (expect 0) on shard_0: SELECT count(*) FROM users WHERE shard_bucket(id) NOT BETWEEN 0 AND 31;

-- ============================================================
-- APPENDIX: event management teams (run on EVERY shard)
-- Event management: run on EVERY shard (after shard_bucket and users exist)
create table events (
  id           uuid primary key default gen_random_uuid(),
  organizer_id uuid not null,
  title        text not null,
  starts_at    timestamptz not null,
  seats_left   int  not null check (seats_left >= 0)
);

create table registrations (
  id          uuid primary key default gen_random_uuid(),
  event_id    uuid not null references events(id),
  attendee_id uuid not null,
  created_at  timestamptz not null default now(),
  unique (event_id, attendee_id)
);

-- copy kept on the ATTENDEE's shard for the "My tickets" page
create table my_tickets (
  attendee_id uuid not null references users(id),
  event_id    uuid not null,
  title       text not null,
  starts_at   timestamptz not null,
  primary key (attendee_id, event_id)
);

create or replace function register(p_event uuid, p_attendee uuid)
returns uuid language plpgsql as $$
declare v_id uuid;
begin
  update events set seats_left = seats_left - 1
  where id = p_event and seats_left > 0;
  if not found then raise exception 'event is full'; end if;
  insert into registrations (event_id, attendee_id)
  values (p_event, p_attendee) returning id into v_id;
  return v_id;
end $$;
-- shard check for events: shard_0 uses 0..31, shard_1 uses 32..63
alter table events add constraint events_belong_here
  check (shard_bucket(id) between 0 and 31);   -- shard_0 version

-- ============================================================
-- APPENDIX: social media teams (run on EVERY shard)
-- Social media: run on EVERY shard (after shard_bucket and users exist)
create table posts (
  id         uuid primary key default gen_random_uuid(),
  author_id  uuid not null references users(id),
  body       text not null,
  created_at timestamptz not null default now()
);

create table likes (                      -- lives with the post
  post_id    uuid not null references posts(id),
  liker_id   uuid not null,
  created_at timestamptz not null default now(),
  primary key (post_id, liker_id)
);

create table following (                  -- on the follower's shard
  follower_id uuid not null references users(id),
  followee_id uuid not null,
  primary key (follower_id, followee_id)
);

create table followers (                  -- on the followee's shard
  followee_id uuid not null references users(id),
  follower_id uuid not null,
  primary key (followee_id, follower_id)
);
