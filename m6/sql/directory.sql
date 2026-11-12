-- Step 6 · Directory and bucket map: run on shard_0 ONLY (the home database).
create table if not exists m6.user_directory (
  email   text primary key,                      -- stored lower-cased
  user_id uuid not null unique default gen_random_uuid()
);

create table if not exists m6.bucket_map (
  bucket int  primary key check (bucket between 0 and 63),
  shard  text not null
);

-- {n_shards} = 1 for the single-database baseline, 2 after sharding.
delete from m6.bucket_map;
insert into m6.bucket_map (bucket, shard)
select b, case when {n_shards} = 1 or b < 32 then 'shard_0' else 'shard_1' end
from generate_series(0, 63) as b;

-- Check: select shard, count(*) from m6.bucket_map group by shard;
