-- M5 Step 4 - freshness. Run once in the Supabase SQL Editor after m5_search.sql.
-- First enable Cron: Supabase Dashboard -> Integrations -> Cron (installs pg_cron).
-- New and edited posts become searchable within five minutes; CONCURRENTLY lets
-- searches keep reading the old copy while the refresh runs (needs post_search_id).

select cron.schedule(
  'refresh-post-search',
  '*/5 * * * *',
  $$refresh materialized view concurrently search.post_search$$
);

-- Check:   select jobid, schedule, command from cron.job;
--          select * from cron.job_run_details order by start_time desc limit 5;
-- Remove:  select cron.unschedule('refresh-post-search');
-- Manual:  refresh materialized view concurrently search.post_search;
