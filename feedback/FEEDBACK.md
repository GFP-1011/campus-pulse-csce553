# Feedback — Campus Pulse

**Relative standing in this class: C.** Read from the code on 11 November 2026.

This letter is search and sharding. Those are the two pieces that set the standing. Other notes are at the end and are separate from it.

The last commit is 9 September. The repository has no full-text search and no sharding. Relative to the class, that is a C.

## Search

`GET /api/users?q=` matches display name and email with `ILIKE` and stops at 50. `GET /api/messages` returns the 100 newest posts and has no query string. There is no `tsvector`, no rank, and no page envelope.

The two searches this product needs:

- People: prefix or full text on the display name, a page, and a total, so a match past 50 is reachable.
- Posts: full text on the body, newest first, the same page shape. The feed can stay "newest posts." Search is how a person finds a post that has left that window.

The page should call those routes. A cap of 100 with no query is not a search.

## Sharding

Users, posts, likes, and friendships are one database. Nothing chooses a shard.

For this app, posts and their likes belong on the author's shard. A person's own posts are then one database. The global feed has to ask every shard and merge by `(created_at, id)`, and the total has to be the sum of the counts, not the length of the first page. People search is a directory: either one shard holds the user rows you search, or you fan out and say so. A search function installed on only one database will drop everyone who lives on the other.

That routing is not in this repository. Adding it is what moves the standing. The current feed, people query, and a new post search are the three paths to put on it first.

## Software engineering notes

These do not change the standing.

- The page is a client of one API, edits are author-only, and a session is required to read the feed. Keep that when you add search and shards.
- Like counts are `len(post.likes)` after the feed loads every like on the page. A `count(*)` and an `EXISTS` for "liked by me" stay flat as likes grow.
- Friendships are stored and the feed does not use them. Either the feed is the whole campus on purpose, or it is posts by people you added and the query joins `friendships`. The row is one direction, and there is no remove.
- There is no comment, no unlike, and no way to tell that the user search omitted rows past 50. Comments, with delete limited to the author, are what make a post a conversation.
