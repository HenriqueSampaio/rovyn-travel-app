-- Track ownership and single-post claiming of social uploads.
CREATE TABLE IF NOT EXISTS "public"."media_uploads" (
    "url"        TEXT PRIMARY KEY,
    "owner_id"   TEXT NOT NULL REFERENCES "public"."users"("id"),
    "post_id"    TEXT REFERENCES "public"."posts"("id"),
    "media_type" TEXT NOT NULL,
    "created_at" TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS "media_uploads_owner_id_idx"
    ON "public"."media_uploads" ("owner_id");

CREATE INDEX IF NOT EXISTS "media_uploads_post_id_idx"
    ON "public"."media_uploads" ("post_id");
