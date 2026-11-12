-- Step 5 · Fail loudly. Run the matching block on each shard ONLY.
-- {lo} and {hi} are filled in by m6/setup_shards.py from bucket_map:
--   1-shard baseline : shard_0 -> 0..63
--   2 shards         : shard_0 -> 0..31, shard_1 -> 32..63
alter table m6.users drop constraint if exists users_belong_here;
alter table m6.users add constraint users_belong_here
  check (m6.shard_bucket(id) between {lo} and {hi});

-- Test (on shard_0 in the 2-shard layout this id is in bucket 37, so it must fail):
-- insert into m6.users (id, email, display_name, password_hash)
-- values ('1d25ffe5-b534-402c-b01d-1b8cd2735f23', 'test@example.com', 'x', 'x');
