-- "Check your work" (lecture slide 41), adapted to schema m6.
-- On each shard: customers here
select count(*) as users_here from m6.users;
-- On shard_0: users per shard according to the directory
select m.shard, count(*) from m6.user_directory d
join m6.bucket_map m on m.bucket = m6.shard_bucket(d.user_id) group by m.shard order by 1;
-- On each shard: rows on the wrong shard (expect 0); use 32..63 on shard_1
select count(*) as wrong_shard from m6.users where m6.shard_bucket(id) not between {lo} and {hi};
