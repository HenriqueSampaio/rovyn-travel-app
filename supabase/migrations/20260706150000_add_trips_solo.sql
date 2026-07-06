--
-- Solo trips: one traveler — no invite surface, joins rejected, tailored copy.
-- IF NOT EXISTS keeps this replayable on databases where the app's former
-- built-in startup migration already added the column before the schema
-- moved under Supabase CLI ownership.
--

ALTER TABLE "public"."trips" ADD COLUMN IF NOT EXISTS "solo" integer DEFAULT 0 NOT NULL;
