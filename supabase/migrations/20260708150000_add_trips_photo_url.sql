--
-- Trip profile photos: a WhatsApp-style group picture, uploaded by any
-- member and shown in the trip's round avatar. Stores the /media/... URL
-- of the uploaded file; NULL falls back to the tinted initial disc.
-- IF NOT EXISTS keeps this replayable on databases where the app's
-- built-in SQLite startup migration already added the column.
--

ALTER TABLE "public"."trips" ADD COLUMN IF NOT EXISTS "photo_url" text;
