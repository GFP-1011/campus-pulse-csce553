-- Paste into Supabase Dashboard → SQL Editor for screenshots (before / after index).
SET search_path = m4b;

-- 1) Baseline: no secondary indexes (run `python m4b.py bench --configs none` or drop them):
EXPLAIN (ANALYZE, BUFFERS)
SELECT p.id, p.body, u.username, count(l.id) AS likes
FROM (SELECT * FROM posts WHERE id < 150000 ORDER BY id DESC LIMIT 20) p
JOIN users u ON u.id = p.author_id
LEFT JOIN likes l ON l.post_id = p.id
GROUP BY p.id, p.body, u.username ORDER BY p.id DESC;
-- Look for: Seq Scan on likes ... Rows Removed / ~1,000,000 rows read

EXPLAIN (ANALYZE, BUFFERS)
SELECT p.id, p.body, p.created_at, u.username,
       (SELECT count(*) FROM likes l WHERE l.post_id = p.id) AS likes
FROM friendships f
JOIN posts p ON p.author_id = f.friend_id
JOIN users u ON u.id = p.author_id
WHERE f.user_id = 42
ORDER BY p.created_at DESC LIMIT 20;

-- 2) Add the minimal index set:
CREATE INDEX IF NOT EXISTS likes_post_id       ON likes (post_id);
CREATE INDEX IF NOT EXISTS posts_author_id_id  ON posts (author_id, id DESC);
CREATE INDEX IF NOT EXISTS likes_user_id_id    ON likes (user_id, id DESC);
CREATE INDEX IF NOT EXISTS friendships_user_id ON friendships (user_id);
ANALYZE likes, posts, friendships;

-- 3) Re-run the two EXPLAIN statements above: expect Index Scan / Index Only Scan, < 1 ms.
