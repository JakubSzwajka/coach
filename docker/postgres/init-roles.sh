#!/usr/bin/env bash
set -euo pipefail

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${GARMIN_COACH_POSTGRES_APP_USER:?GARMIN_COACH_POSTGRES_APP_USER is required}"
: "${GARMIN_COACH_POSTGRES_APP_PASSWORD:?GARMIN_COACH_POSTGRES_APP_PASSWORD is required}"

if [[ "$POSTGRES_USER" == "$GARMIN_COACH_POSTGRES_APP_USER" ]]; then
  echo 'PostgreSQL migration owner and application role must differ' >&2
  exit 1
fi

psql --set ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set admin_user="$POSTGRES_USER" \
  --set database="$POSTGRES_DB" \
  --set app_user="$GARMIN_COACH_POSTGRES_APP_USER" \
  --set app_password="$GARMIN_COACH_POSTGRES_APP_PASSWORD" <<'SQL'
CREATE ROLE :"app_user"
  LOGIN
  PASSWORD :'app_password'
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT;
REVOKE ALL ON DATABASE :"database" FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database" TO :"app_user";
GRANT USAGE ON SCHEMA public TO :"app_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"admin_user" IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_user";
SQL
