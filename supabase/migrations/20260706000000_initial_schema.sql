--
-- PostgreSQL database dump
--


-- Dumped from database version 17.6
-- Dumped by pg_dump version 18.4

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

-- CREATE SCHEMA "public"; (already exists on Supabase)


--
-- Name: SCHEMA "public"; Type: COMMENT; Schema: -; Owner: -
--

-- comment on schema skipped (owned by supabase_admin)


SET default_tablespace = '';

SET default_table_access_method = "heap";

--
-- Name: approvals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."approvals" (
    "trip_id" "text" NOT NULL,
    "user_id" "text" NOT NULL,
    "approved_at" "text" NOT NULL
);


--
-- Name: auth_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."auth_sessions" (
    "token" "text" NOT NULL,
    "user_id" "text" NOT NULL,
    "created_at" "text" NOT NULL
);


--
-- Name: bookings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."bookings" (
    "trip_id" "text" NOT NULL,
    "item_key" "text" NOT NULL,
    "category" "text" NOT NULL,
    "scope" "text" DEFAULT 'group'::"text" NOT NULL,
    "title" "text" NOT NULL,
    "provider" "text" DEFAULT 'self'::"text" NOT NULL,
    "link" "text",
    "status" "text" DEFAULT 'to_book'::"text" NOT NULL,
    "confirmation" "text",
    "note" "text",
    "booked_by" "text",
    "booked_at" "text",
    "details" "text",
    "stale" integer DEFAULT 0 NOT NULL,
    "created_at" "text" NOT NULL
);


--
-- Name: category_progress; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."category_progress" (
    "trip_id" "text" NOT NULL,
    "user_id" "text" NOT NULL,
    "category" "text" NOT NULL,
    "status" "text" NOT NULL,
    "updated_at" "text" NOT NULL
);


--
-- Name: payments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."payments" (
    "trip_id" "text" NOT NULL,
    "user_id" "text" NOT NULL,
    "amount_cents" integer NOT NULL,
    "currency" "text" DEFAULT 'USD'::"text" NOT NULL,
    "mode" "text" DEFAULT 'full'::"text" NOT NULL,
    "receipt" "text" NOT NULL,
    "email_status" "text" DEFAULT 'none'::"text" NOT NULL,
    "paid_at" "text" NOT NULL
);


--
-- Name: post_comments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."post_comments" (
    "id" "text" NOT NULL,
    "post_id" "text" NOT NULL,
    "user_id" "text" NOT NULL,
    "text" "text" NOT NULL,
    "created_at" "text" NOT NULL
);


--
-- Name: post_likes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."post_likes" (
    "post_id" "text" NOT NULL,
    "user_id" "text" NOT NULL,
    "liked_at" "text" NOT NULL
);


--
-- Name: post_media; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."post_media" (
    "id" "text" NOT NULL,
    "post_id" "text" NOT NULL,
    "position" integer NOT NULL,
    "media_type" "text" NOT NULL,
    "url" "text" NOT NULL
);


--
-- Name: posts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."posts" (
    "id" "text" NOT NULL,
    "user_id" "text" NOT NULL,
    "trip_id" "text",
    "trip_snapshot" "text",
    "caption" "text" DEFAULT ''::"text" NOT NULL,
    "destination" "text",
    "vibes" "text" DEFAULT '[]'::"text" NOT NULL,
    "created_at" "text" NOT NULL
);


--
-- Name: questionnaires; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."questionnaires" (
    "trip_id" "text" NOT NULL,
    "user_id" "text" NOT NULL,
    "data" "text" NOT NULL,
    "submitted_at" "text" NOT NULL
);


--
-- Name: stars; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."stars" (
    "trip_id" "text" NOT NULL,
    "user_id" "text" NOT NULL,
    "category" "text" NOT NULL,
    "option_id" "text" NOT NULL
);


--
-- Name: trip_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."trip_members" (
    "trip_id" "text" NOT NULL,
    "user_id" "text" NOT NULL,
    "joined_at" "text" NOT NULL
);


--
-- Name: trips; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."trips" (
    "id" "text" NOT NULL,
    "name" "text" NOT NULL,
    "invite_code" "text" NOT NULL,
    "leader_id" "text" NOT NULL,
    "status" "text" DEFAULT 'waiting'::"text" NOT NULL,
    "recommendations" "text",
    "recommendations_generating" integer DEFAULT 0 NOT NULL,
    "itinerary" "text",
    "itinerary_generating" integer DEFAULT 0 NOT NULL,
    "selected_destination" "text",
    "destination" "text",
    "options" "text",
    "options_generating" integer DEFAULT 0 NOT NULL,
    "final_plan" "text",
    "final_plan_generating" integer DEFAULT 0 NOT NULL,
    "stay_media" "text",
    "created_at" "text" NOT NULL,
    "trip_mode" "text",
    "city_ideas" "text"
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."users" (
    "id" "text" NOT NULL,
    "email" "text" NOT NULL,
    "name" "text" NOT NULL,
    "password_hash" "text" DEFAULT ''::"text" NOT NULL,
    "created_at" "text" NOT NULL
);


--
-- Name: votes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."votes" (
    "trip_id" "text" NOT NULL,
    "user_id" "text" NOT NULL,
    "destination_name" "text" NOT NULL
);


--
-- Name: approvals approvals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."approvals"
    ADD CONSTRAINT "approvals_pkey" PRIMARY KEY ("trip_id", "user_id");


--
-- Name: auth_sessions auth_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."auth_sessions"
    ADD CONSTRAINT "auth_sessions_pkey" PRIMARY KEY ("token");


--
-- Name: bookings bookings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."bookings"
    ADD CONSTRAINT "bookings_pkey" PRIMARY KEY ("trip_id", "item_key");


--
-- Name: category_progress category_progress_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."category_progress"
    ADD CONSTRAINT "category_progress_pkey" PRIMARY KEY ("trip_id", "user_id", "category");


--
-- Name: payments payments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."payments"
    ADD CONSTRAINT "payments_pkey" PRIMARY KEY ("trip_id", "user_id");


--
-- Name: post_comments post_comments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."post_comments"
    ADD CONSTRAINT "post_comments_pkey" PRIMARY KEY ("id");


--
-- Name: post_likes post_likes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."post_likes"
    ADD CONSTRAINT "post_likes_pkey" PRIMARY KEY ("post_id", "user_id");


--
-- Name: post_media post_media_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."post_media"
    ADD CONSTRAINT "post_media_pkey" PRIMARY KEY ("id");


--
-- Name: posts posts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."posts"
    ADD CONSTRAINT "posts_pkey" PRIMARY KEY ("id");


--
-- Name: questionnaires questionnaires_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."questionnaires"
    ADD CONSTRAINT "questionnaires_pkey" PRIMARY KEY ("trip_id", "user_id");


--
-- Name: stars stars_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."stars"
    ADD CONSTRAINT "stars_pkey" PRIMARY KEY ("trip_id", "user_id", "category", "option_id");


--
-- Name: trip_members trip_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."trip_members"
    ADD CONSTRAINT "trip_members_pkey" PRIMARY KEY ("trip_id", "user_id");


--
-- Name: trips trips_invite_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."trips"
    ADD CONSTRAINT "trips_invite_code_key" UNIQUE ("invite_code");


--
-- Name: trips trips_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."trips"
    ADD CONSTRAINT "trips_pkey" PRIMARY KEY ("id");


--
-- Name: users users_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."users"
    ADD CONSTRAINT "users_email_key" UNIQUE ("email");


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."users"
    ADD CONSTRAINT "users_pkey" PRIMARY KEY ("id");


--
-- Name: votes votes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."votes"
    ADD CONSTRAINT "votes_pkey" PRIMARY KEY ("trip_id", "user_id");


--
-- Name: approvals approvals_trip_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."approvals"
    ADD CONSTRAINT "approvals_trip_id_fkey" FOREIGN KEY ("trip_id") REFERENCES "public"."trips"("id");


--
-- Name: approvals approvals_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."approvals"
    ADD CONSTRAINT "approvals_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id");


--
-- Name: auth_sessions auth_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."auth_sessions"
    ADD CONSTRAINT "auth_sessions_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id");


--
-- Name: bookings bookings_booked_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."bookings"
    ADD CONSTRAINT "bookings_booked_by_fkey" FOREIGN KEY ("booked_by") REFERENCES "public"."users"("id");


--
-- Name: bookings bookings_trip_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."bookings"
    ADD CONSTRAINT "bookings_trip_id_fkey" FOREIGN KEY ("trip_id") REFERENCES "public"."trips"("id");


--
-- Name: category_progress category_progress_trip_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."category_progress"
    ADD CONSTRAINT "category_progress_trip_id_fkey" FOREIGN KEY ("trip_id") REFERENCES "public"."trips"("id");


--
-- Name: category_progress category_progress_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."category_progress"
    ADD CONSTRAINT "category_progress_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id");


--
-- Name: payments payments_trip_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."payments"
    ADD CONSTRAINT "payments_trip_id_fkey" FOREIGN KEY ("trip_id") REFERENCES "public"."trips"("id");


--
-- Name: payments payments_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."payments"
    ADD CONSTRAINT "payments_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id");


--
-- Name: post_comments post_comments_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."post_comments"
    ADD CONSTRAINT "post_comments_post_id_fkey" FOREIGN KEY ("post_id") REFERENCES "public"."posts"("id");


--
-- Name: post_comments post_comments_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."post_comments"
    ADD CONSTRAINT "post_comments_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id");


--
-- Name: post_likes post_likes_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."post_likes"
    ADD CONSTRAINT "post_likes_post_id_fkey" FOREIGN KEY ("post_id") REFERENCES "public"."posts"("id");


--
-- Name: post_likes post_likes_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."post_likes"
    ADD CONSTRAINT "post_likes_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id");


--
-- Name: post_media post_media_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."post_media"
    ADD CONSTRAINT "post_media_post_id_fkey" FOREIGN KEY ("post_id") REFERENCES "public"."posts"("id");


--
-- Name: posts posts_trip_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."posts"
    ADD CONSTRAINT "posts_trip_id_fkey" FOREIGN KEY ("trip_id") REFERENCES "public"."trips"("id");


--
-- Name: posts posts_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."posts"
    ADD CONSTRAINT "posts_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id");


--
-- Name: questionnaires questionnaires_trip_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."questionnaires"
    ADD CONSTRAINT "questionnaires_trip_id_fkey" FOREIGN KEY ("trip_id") REFERENCES "public"."trips"("id");


--
-- Name: questionnaires questionnaires_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."questionnaires"
    ADD CONSTRAINT "questionnaires_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id");


--
-- Name: stars stars_trip_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."stars"
    ADD CONSTRAINT "stars_trip_id_fkey" FOREIGN KEY ("trip_id") REFERENCES "public"."trips"("id");


--
-- Name: stars stars_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."stars"
    ADD CONSTRAINT "stars_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id");


--
-- Name: trip_members trip_members_trip_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."trip_members"
    ADD CONSTRAINT "trip_members_trip_id_fkey" FOREIGN KEY ("trip_id") REFERENCES "public"."trips"("id");


--
-- Name: trip_members trip_members_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."trip_members"
    ADD CONSTRAINT "trip_members_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id");


--
-- Name: trips trips_leader_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."trips"
    ADD CONSTRAINT "trips_leader_id_fkey" FOREIGN KEY ("leader_id") REFERENCES "public"."users"("id");


--
-- Name: votes votes_trip_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."votes"
    ADD CONSTRAINT "votes_trip_id_fkey" FOREIGN KEY ("trip_id") REFERENCES "public"."trips"("id");


--
-- Name: votes votes_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."votes"
    ADD CONSTRAINT "votes_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id");


--
-- PostgreSQL database dump complete
--


