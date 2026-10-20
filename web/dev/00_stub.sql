-- Stub local do Supabase (SÓ para desenvolvimento do front; nunca aplicar em produção)
create role authenticated nologin; create role service_role nologin; create role anon nologin;
create role authenticator noinherit login password 'x';
grant anon, authenticated, service_role to authenticator;
create schema auth; create table auth.users (id uuid primary key, email text, nome text);
create function auth.uid() returns uuid language sql stable as $$
  select coalesce(nullif(current_setting('request.jwt.claim.sub', true), ''),
                  (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub'))::uuid $$;
grant usage on schema auth to authenticated, service_role, anon;
grant execute on function auth.uid() to authenticated, service_role, anon;
alter role service_role bypassrls;
