--
-- PostgreSQL database dump
--

\restrict 7gYvJGc7OfAnm4iIDXimH2fOYOcBD8wB1QMErH5p7T4OKjus6iWRZqu46zNhdnP

-- Dumped from database version 15.19 (Debian 15.19-1.pgdg13+2)
-- Dumped by pg_dump version 15.19 (Debian 15.19-1.pgdg13+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: auth; Type: SCHEMA; Schema: -; Owner: postgres
--

CREATE SCHEMA auth;


ALTER SCHEMA auth OWNER TO postgres;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: pg_database_owner
--

CREATE SCHEMA public;


ALTER SCHEMA public OWNER TO pg_database_owner;

--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: pg_database_owner
--

COMMENT ON SCHEMA public IS 'standard public schema';


--
-- Name: account_role_enum; Type: TYPE; Schema: public; Owner: postgres
--

CREATE TYPE public.account_role_enum AS ENUM (
    'owner',
    'admin',
    'agent',
    'viewer'
);


ALTER TYPE public.account_role_enum OWNER TO postgres;

--
-- Name: uid(); Type: FUNCTION; Schema: auth; Owner: postgres
--

CREATE FUNCTION auth.uid() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;


ALTER FUNCTION auth.uid() OWNER TO postgres;

--
-- Name: agency_master_account_id(uuid); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.agency_master_account_id(a uuid) RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER
    AS $$ select p.account_id from agencies ag join profiles p on p.user_id=ag.owner_user_id where ag.id=a $$;


ALTER FUNCTION public.agency_master_account_id(a uuid) OWNER TO postgres;

--
-- Name: is_account_member(uuid, public.account_role_enum); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.is_account_member(acc uuid, min_role public.account_role_enum DEFAULT 'viewer'::public.account_role_enum) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    AS $$
 select is_rco() or exists(select 1 from profiles p where p.user_id=auth.uid() and p.account_id=acc and p.account_role <= min_role) $$;


ALTER FUNCTION public.is_account_member(acc uuid, min_role public.account_role_enum) OWNER TO postgres;

--
-- Name: is_agency_member(uuid); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.is_agency_member(a uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    AS $$ select exists(select 1 from agency_members where agency_id=a and user_id=auth.uid()) $$;


ALTER FUNCTION public.is_agency_member(a uuid) OWNER TO postgres;

--
-- Name: is_rco(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.is_rco() RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    AS $$ select exists(select 1 from profiles where user_id=auth.uid() and platform_role='rco') $$;


ALTER FUNCTION public.is_rco() OWNER TO postgres;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: users; Type: TABLE; Schema: auth; Owner: postgres
--

CREATE TABLE auth.users (
    id uuid NOT NULL
);


ALTER TABLE auth.users OWNER TO postgres;

--
-- Name: accounts; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.accounts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text,
    agency_id uuid
);


ALTER TABLE public.accounts OWNER TO postgres;

--
-- Name: agencies; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.agencies (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text,
    owner_user_id uuid NOT NULL,
    status text DEFAULT 'active'::text NOT NULL
);


ALTER TABLE public.agencies OWNER TO postgres;

--
-- Name: agency_members; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.agency_members (
    agency_id uuid NOT NULL,
    user_id uuid NOT NULL
);


ALTER TABLE public.agency_members OWNER TO postgres;

--
-- Name: contacts; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.contacts (
    id uuid DEFAULT gen_random_uuid() NOT NULL
);


ALTER TABLE public.contacts OWNER TO postgres;

--
-- Name: deals; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.deals (
    id uuid DEFAULT gen_random_uuid() NOT NULL
);


ALTER TABLE public.deals OWNER TO postgres;

--
-- Name: pipeline_stages; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.pipeline_stages (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    pipeline_id uuid NOT NULL,
    name text
);


ALTER TABLE public.pipeline_stages OWNER TO postgres;

--
-- Name: pipelines; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.pipelines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid NOT NULL,
    name text
);


ALTER TABLE public.pipelines OWNER TO postgres;

--
-- Name: profiles; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.profiles (
    user_id uuid NOT NULL,
    account_id uuid,
    account_role public.account_role_enum,
    platform_role text
);


ALTER TABLE public.profiles OWNER TO postgres;

--
-- Name: whatsapp_config; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.whatsapp_config (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid NOT NULL
);


ALTER TABLE public.whatsapp_config OWNER TO postgres;

--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: auth; Owner: postgres
--

ALTER TABLE ONLY auth.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: accounts accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT accounts_pkey PRIMARY KEY (id);


--
-- Name: agencies agencies_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.agencies
    ADD CONSTRAINT agencies_pkey PRIMARY KEY (id);


--
-- Name: agency_members agency_members_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.agency_members
    ADD CONSTRAINT agency_members_pkey PRIMARY KEY (agency_id, user_id);


--
-- Name: contacts contacts_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.contacts
    ADD CONSTRAINT contacts_pkey PRIMARY KEY (id);


--
-- Name: deals deals_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.deals
    ADD CONSTRAINT deals_pkey PRIMARY KEY (id);


--
-- Name: pipeline_stages pipeline_stages_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.pipeline_stages
    ADD CONSTRAINT pipeline_stages_pkey PRIMARY KEY (id);


--
-- Name: pipelines pipelines_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.pipelines
    ADD CONSTRAINT pipelines_pkey PRIMARY KEY (id);


--
-- Name: profiles profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.profiles
    ADD CONSTRAINT profiles_pkey PRIMARY KEY (user_id);


--
-- Name: whatsapp_config whatsapp_config_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.whatsapp_config
    ADD CONSTRAINT whatsapp_config_pkey PRIMARY KEY (id);


--
-- Name: agency_members agency_members_agency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.agency_members
    ADD CONSTRAINT agency_members_agency_id_fkey FOREIGN KEY (agency_id) REFERENCES public.agencies(id);


--
-- Name: pipeline_stages pipeline_stages_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.pipeline_stages
    ADD CONSTRAINT pipeline_stages_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES public.pipelines(id);


--
-- Name: SCHEMA auth; Type: ACL; Schema: -; Owner: postgres
--

GRANT USAGE ON SCHEMA auth TO authenticated;


--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: pg_database_owner
--

GRANT USAGE ON SCHEMA public TO authenticated;
GRANT USAGE ON SCHEMA public TO service_role;
GRANT USAGE ON SCHEMA public TO anon;


--
-- Name: FUNCTION uid(); Type: ACL; Schema: auth; Owner: postgres
--

GRANT ALL ON FUNCTION auth.uid() TO authenticated;


--
-- Name: FUNCTION agency_master_account_id(a uuid); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.agency_master_account_id(a uuid) TO authenticated;
GRANT ALL ON FUNCTION public.agency_master_account_id(a uuid) TO service_role;


--
-- Name: FUNCTION is_account_member(acc uuid, min_role public.account_role_enum); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.is_account_member(acc uuid, min_role public.account_role_enum) TO authenticated;
GRANT ALL ON FUNCTION public.is_account_member(acc uuid, min_role public.account_role_enum) TO service_role;


--
-- Name: FUNCTION is_agency_member(a uuid); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.is_agency_member(a uuid) TO authenticated;
GRANT ALL ON FUNCTION public.is_agency_member(a uuid) TO service_role;


--
-- Name: FUNCTION is_rco(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION public.is_rco() TO authenticated;
GRANT ALL ON FUNCTION public.is_rco() TO service_role;


--
-- Name: TABLE accounts; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.accounts TO authenticated;
GRANT SELECT ON TABLE public.accounts TO service_role;


--
-- Name: TABLE agencies; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.agencies TO authenticated;
GRANT SELECT ON TABLE public.agencies TO service_role;


--
-- Name: TABLE agency_members; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.agency_members TO authenticated;
GRANT SELECT ON TABLE public.agency_members TO service_role;


--
-- Name: TABLE contacts; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.contacts TO authenticated;
GRANT SELECT ON TABLE public.contacts TO service_role;


--
-- Name: TABLE deals; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.deals TO authenticated;
GRANT SELECT ON TABLE public.deals TO service_role;


--
-- Name: TABLE pipeline_stages; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.pipeline_stages TO authenticated;
GRANT SELECT ON TABLE public.pipeline_stages TO service_role;


--
-- Name: TABLE pipelines; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.pipelines TO authenticated;
GRANT SELECT ON TABLE public.pipelines TO service_role;


--
-- Name: TABLE profiles; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.profiles TO authenticated;
GRANT SELECT ON TABLE public.profiles TO service_role;


--
-- Name: TABLE whatsapp_config; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.whatsapp_config TO authenticated;
GRANT SELECT ON TABLE public.whatsapp_config TO service_role;


--
-- PostgreSQL database dump complete
--

\unrestrict 7gYvJGc7OfAnm4iIDXimH2fOYOcBD8wB1QMErH5p7T4OKjus6iWRZqu46zNhdnP

