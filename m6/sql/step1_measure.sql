-- Step 1 · Confirm that writes are the bottleneck (lecture slide 29).
create extension if not exists pg_stat_statements with schema extensions;

select left(query, 60)        as statement,
       calls,
       round(total_exec_time) as total_ms
from extensions.pg_stat_statements
where query ~* '^\s*(insert|update|delete)'
order by total_exec_time desc
limit 10;

-- Campus Pulse writes run inside functions (publish_post, fanout), so the
-- report also lists the top statements of any kind with their write share:
select left(query, 70) as statement, calls, round(total_exec_time) as total_ms,
       round(mean_exec_time::numeric, 2) as mean_ms, wal_bytes
from extensions.pg_stat_statements
order by total_exec_time desc limit 10;
