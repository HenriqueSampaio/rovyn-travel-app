-- Wishlist: travelers save a post's itinerary snapshot to copy later.
-- post_id has no FK on purpose — the saved snapshot outlives the post.
CREATE TABLE IF NOT EXISTS "public"."wishlist_items" (
    "id"            TEXT PRIMARY KEY,
    "user_id"       TEXT NOT NULL REFERENCES "public"."users"("id"),
    "post_id"       TEXT NOT NULL,
    "author_name"   TEXT NOT NULL,
    "destination"   TEXT,
    "caption"       TEXT NOT NULL DEFAULT '',
    "trip_snapshot" TEXT NOT NULL,
    "added_at"      TEXT NOT NULL,
    UNIQUE ("user_id", "post_id")
);
