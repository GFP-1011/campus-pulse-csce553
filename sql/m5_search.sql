-- M5: full-text search for Campus Pulse posts (PostgreSQL / Supabase).
-- Follows "Recipe for Adding Search": document -> denormalized view -> GIN index
-- -> freshness -> search function. Safe to re-run.
--
-- Search document = one post.
--   text    : post body (weight A) + author display name (weight B); #hashtags are part of the body
--   filters : author_id, hashtag, created_at >= since
--   facets  : hashtag
--   sort    : relevance (ts_rank) or newest
--   display : body, author name, created_at, like_count
-- Visibility: Campus Pulse has a single public feed (every signed-in user sees every post),
-- so no per-viewer filter is needed. If private posts are added later, filter them inside
-- search.search_posts, because a materialized view ignores row-level security.

create schema if not exists search;

-- The search schema is not exposed through the Supabase Data API; only the app's own
-- database connection (DATABASE_URL) can read it.
revoke all on schema search from public;
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on schema search from anon, authenticated';
  end if;
end $$;

-- Step 2 - denormalize: join posts + users + like counts once, at refresh time.
drop materialized view if exists search.post_search;
create materialized view search.post_search as
select p.id,
       p.author_id,
       u.display_name::text                         as author_name,
       p.body,
       p.created_at,
       coalesce(lc.n, 0)::bigint                    as like_count,
       coalesce(array(select distinct lower(m[1])
                      from regexp_matches(p.body, '#([A-Za-z0-9_]+)', 'g') as m),
                '{}'::text[])                       as tags,
       setweight(to_tsvector('english', coalesce(p.body, '')), 'A') ||
       setweight(to_tsvector('english', coalesce(u.display_name, '')), 'B') as fts
from public.posts p
join public.users u on u.id = p.author_id
left join (select post_id, count(*) as n from public.likes group by post_id) lc
       on lc.post_id = p.id;

-- Step 3 - index.
create unique index post_search_id      on search.post_search (id);          -- needed for REFRESH ... CONCURRENTLY
create index        post_search_fts     on search.post_search using gin (fts);   -- inverted index
create index        post_search_tags    on search.post_search using gin (tags);  -- hashtag filter
create index        post_search_created on search.post_search (created_at desc); -- "newest" sort

-- Step 5 - one search function (ranked, filtered, paged; total = number of matches).
create or replace function search.search_posts(
    q        text,
    p_author integer     default null,
    p_tag    text        default null,
    p_since  timestamptz default null,
    p_sort   text        default 'relevance',
    lim      integer     default 20,
    off      integer     default 0)
returns table (id integer, author_id integer, author_name text, body text,
               created_at timestamptz, like_count bigint, tags text[],
               rank real, total bigint)
language sql stable as $$
  with query as (select websearch_to_tsquery('english', q) as tsq),
  matches as (
    select s.*, ts_rank(s.fts, query.tsq) as rank
    from search.post_search s, query
    where s.fts @@ query.tsq
      and (p_author is null or s.author_id = p_author)
      and (p_tag    is null or s.tags @> array[lower(p_tag)])
      and (p_since  is null or s.created_at >= p_since)
  )
  select m.id, m.author_id, m.author_name, m.body, m.created_at, m.like_count,
         m.tags, m.rank, count(*) over () as total
  from matches m
  order by case when p_sort = 'newest' then null else m.rank end desc nulls last,
           m.created_at desc, m.id desc
  limit least(lim, 50) offset off;
$$;

-- Step 7 - facets: hashtag counts for the current text match (before the tag filter).
create or replace function search.search_tag_facets(
    q        text,
    p_author integer     default null,
    p_since  timestamptz default null,
    lim      integer     default 8)
returns table (tag text, n bigint)
language sql stable as $$
  select t.tag, count(*) as n
  from search.post_search s
  cross join lateral unnest(s.tags) as t(tag)
  where s.fts @@ websearch_to_tsquery('english', q)
    and (p_author is null or s.author_id = p_author)
    and (p_since  is null or s.created_at >= p_since)
  group by t.tag
  order by n desc, t.tag
  limit least(lim, 20);
$$;

analyze search.post_search;
