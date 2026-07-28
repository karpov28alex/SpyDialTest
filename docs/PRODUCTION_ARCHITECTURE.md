# Dialog Spy — Production Architecture

## Status

This document is the mandatory engineering baseline for Dialog Spy. A release is not production-ready until every release gate at the end of this document passes.

## Monorepo layout

```text
backend/
  app/
    api/
      public/
      admin/
      dependencies/
      errors/
      middleware/
    bot/
      handlers/
      keyboards/
      states/
      commands/
    business/
      connections/
      dialogs/
      messages/
      edits/
      deletions/
      protected_media/
    notifications/
      policies/
      templates/
      delivery/
    storage/
      interfaces/
      local/
      s3/
      signing/
    telegram/
      webhook/
      updates/
      clients/
      validation/
    workers/
      celery_app.py
      tasks/
      schedules/
    billing/
      access/
      trials/
      referrals/
      subscriptions/
      payments/
    admin/
      auth/
      users/
      broadcasts/
      audit/
      system/
    shared/
      config/
      db/
      logging/
      security/
      observability/
  alembic/
  tests/
frontend/
  miniapp/
  admin/
  shared/
infra/
  nginx/
  docker/
  monitoring/
  backup/
docs/
```

## Mandatory architectural rules

1. Routers only validate input, authorize, call an application service, and serialize output.
2. React components contain presentation logic only. Data access and business rules live in hooks/services.
3. PostgreSQL is the source of truth for all durable operations.
4. Redis/Celery is never the sole storage for important state.
5. Every Telegram update is deduplicated by unique `update_id`.
6. Every outbound notification is represented by a durable outbox record committed in the same database transaction as the event that created it.
7. Workers consume committed outbox rows. No Redis enqueue is allowed before the database transaction commits.
8. Protected media delivery must re-check `is_protected = true` immediately before sending.
9. Ordinary new messages and ordinary media are archived only and are never sent to the owner by the bot.
10. Every admin read of private dialogs/media creates an audit record.

## Backend stack

- Python 3.12
- FastAPI
- aiogram 3.x
- SQLAlchemy 2
- Alembic
- PostgreSQL
- Redis
- Celery
- Pydantic Settings
- structlog
- OpenTelemetry-compatible tracing hooks

## Frontend stack

- React
- TypeScript
- Vite
- React Router
- TanStack Query
- Telegram WebApp SDK
- Chart.js for admin analytics

## Telegram update pipeline

```text
Telegram HTTPS webhook
  -> secret validation
  -> schema validation
  -> deduplication claim
  -> short database transaction
  -> domain service
  -> event/outbox rows
  -> commit
  -> immediate HTTP 200
  -> Celery worker processes outbox
  -> Telegram Bot API / storage
```

Failed updates are stored with sanitized payload, traceback, correlation ID, attempt count, and resolution state.

## Message versioning

For every edit:

- keep current content in `messages`;
- insert the previous content into `message_versions`;
- create a domain event containing old and new content;
- create exactly one notification outbox row if enabled;
- use an idempotency key based on Telegram update ID and message ID.

For deletion:

- set `is_deleted` and `deleted_at`;
- never erase saved text, caption, versions, or media;
- create exactly one notification outbox row.

## Protected media policy

Protected media behavior is isolated behind a `ProtectedMediaPolicy` interface. The default policy requires:

1. the original archived media row has `is_protected = true`;
2. the owner sends a supported reply referencing the archived original;
3. access is active;
4. delivery has not already completed for the same original/reply pair;
5. the worker re-checks protection before download and before send.

A normal replied-to photo must never be classified as protected.

## Mini App navigation

React Router is the only navigation source of truth.

- one Telegram BackButton subscription for the lifetime of the application;
- one browser history integration;
- nested screens call `navigate(-1)` through a shared navigation service;
- route loaders are cancellable;
- stale requests cannot update a new route;
- root routes hide Telegram BackButton;
- nested routes show it;
- fullscreen calls are guarded and never block startup.

Required routes:

```text
/
/dialogs
/dialogs/:dialogId
/profile
/settings
/subscription
/faq
/connect
```

## Authentication

### Mini App

- validate Telegram `initData` hash and `auth_date`;
- enforce maximum age and replay protection;
- issue short-lived access token;
- rotate refresh token;
- never trust a previous local token before validating the current Telegram user.

### Web admin

- Argon2id password hashing;
- short-lived access JWT;
- refresh token rotation in HttpOnly Secure SameSite cookie;
- login rate limiting;
- session revocation and revoke-all;
- login audit log.

## Media storage

Storage is abstracted behind an interface:

- local private volume for development;
- private S3-compatible bucket for production;
- no public object URLs;
- signed short-lived download URLs;
- object ownership checked on every request;
- checksum and download status persisted.

## Admin capabilities

- dashboard and charts;
- users, filters, pagination;
- user details and access controls;
- VIP grants with idempotency and audit;
- dialogs and media access with audit;
- referral/campaign links;
- broadcasts with recipient snapshots;
- help content versioning;
- error retry/resolve;
- infrastructure health.

## Deployment

Production compose services:

```text
api
worker
scheduler
postgres
redis
nginx
miniapp
admin
```

Requirements:

- non-root containers;
- read-only application filesystem where possible;
- healthchecks;
- restart policies;
- secrets excluded from images and Git;
- database migrations run as a dedicated deploy step;
- rolling-compatible frontend assets with content hashes;
- HTML `Cache-Control: no-store`;
- hashed assets immutable for one year.

## Testing

### Backend

- unit tests for domain policies;
- integration tests against PostgreSQL and Redis;
- API tests;
- Telegram update fixture tests;
- outbox idempotency tests;
- notification matrix tests;
- authorization/object ownership tests;
- migration upgrade tests.

### Frontend

- unit tests for hooks/services;
- component tests;
- React Router navigation tests;
- 100-cycle BackButton regression test;
- media viewer tests;
- stale request cancellation tests.

### E2E

- at least two Telegram users;
- Telegram Business connection;
- new/edit/delete flows;
- protected media flow where supported by the Telegram client/API;
- ordinary media non-forwarding;
- Mini App user isolation;
- admin authorization and audit.

## Release gates

A production release is blocked unless all of the following pass:

- Docker build and startup;
- healthchecks;
- migrations on a clean database and an upgraded database;
- webhook installation and delivery;
- registration and idempotent `/start`;
- Mini App authentication for two distinct users;
- fullscreen startup with safe-area handling;
- all required routes and BackButton scenarios;
- dialog archive and media rendering;
- edit old/new version persistence and notification;
- deletion persistence and notification;
- protected media policy tests;
- ordinary media never forwarded;
- referral link CRUD and statistics;
- admin login, permissions, and audit;
- broadcast snapshot, retry, cancel, and rate-limit behavior;
- no unhandled exceptions in logs;
- backup creation and tested restore.

Passing unit tests alone is not sufficient. Telegram E2E and backup restore are mandatory before declaring production readiness.
