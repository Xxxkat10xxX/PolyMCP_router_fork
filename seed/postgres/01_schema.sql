-- Детерминированные демо-данные. На них опираются задачи бенчмарка (bench/tasks.json),
-- поэтому при изменении данных нужно обновить ожидаемые ответы.

CREATE TABLE users (
    id          integer PRIMARY KEY,
    email       text NOT NULL UNIQUE,
    country     text NOT NULL,
    plan        text NOT NULL CHECK (plan IN ('free', 'pro', 'enterprise')),
    created_at  timestamptz NOT NULL
);

-- 60 пользователей: регистрация каждые 5 дней начиная с 2026-01-06.
INSERT INTO users (id, email, country, plan, created_at)
SELECT i,
       'user' || i || '@example.com',
       (ARRAY['RU', 'KZ', 'DE', 'US', 'BR'])[1 + (i % 5)],
       (ARRAY['free', 'free', 'pro', 'enterprise'])[1 + (i % 4)],
       timestamptz '2026-01-01 10:00:00+00' + make_interval(days => i * 5)
FROM generate_series(1, 60) AS i;

CREATE TABLE bugs (
    id               text PRIMARY KEY,
    title            text NOT NULL,
    severity         text NOT NULL CHECK (severity IN ('critical', 'high', 'medium', 'low')),
    status           text NOT NULL CHECK (status IN ('open', 'in_progress', 'closed')),
    component        text NOT NULL,
    reported_at      timestamptz NOT NULL,
    fixed_in_commit  text
);

INSERT INTO bugs VALUES
 ('BUG-101', 'Double charge when payment retry times out',   'critical', 'open',        'billing', '2026-10-02 09:12+00', NULL),
 ('BUG-102', 'Avatar upload fails for PNG over 5 MB',        'low',      'closed',      'api',     '2026-10-03 11:40+00', 'a3f9c21'),
 ('BUG-103', 'Search ignores Cyrillic diacritics',           'medium',   'open',        'api',     '2026-10-05 15:02+00', NULL),
 ('BUG-104', 'Worker queue stalls after Redis failover',     'high',     'in_progress', 'worker',  '2026-10-07 08:25+00', NULL),
 ('BUG-105', 'Invoice PDF shows wrong VAT rate for KZ',      'high',     'open',        'billing', '2026-10-09 13:30+00', NULL),
 ('BUG-106', 'Password reset email sent twice',              'low',      'closed',      'auth',    '2026-10-11 10:10+00', '7be0d44'),
 ('BUG-107', 'Token refresh race logs users out',            'critical', 'open',        'auth',    '2026-10-14 17:45+00', NULL),
 ('BUG-108', 'Dashboard chart off by one day in UTC+5',      'medium',   'closed',      'api',     '2026-10-16 12:00+00', 'c41e8b0'),
 ('BUG-109', 'Webhook signature check skipped on retry',     'critical', 'in_progress', 'api',     '2026-10-20 09:55+00', NULL),
 ('BUG-110', 'Slow /reports endpoint for enterprise plans',  'high',     'open',        'api',     '2026-10-23 14:18+00', NULL),
 ('BUG-111', 'Worker OOM on CSV export over 1M rows',        'medium',   'open',        'worker',  '2026-10-27 16:31+00', NULL),
 ('BUG-112', 'Typo in German onboarding email',              'low',      'open',        'auth',    '2026-10-30 08:05+00', NULL);

CREATE TABLE deployments (
    id           integer PRIMARY KEY,
    service      text NOT NULL,
    version      text NOT NULL,
    status       text NOT NULL CHECK (status IN ('success', 'failed', 'rolled_back')),
    deployed_at  timestamptz NOT NULL
);

INSERT INTO deployments VALUES
 (1, 'api',     'v2.14.0', 'success',     '2026-11-02 10:00+00'),
 (2, 'billing', 'v1.8.3',  'success',     '2026-11-03 12:30+00'),
 (3, 'worker',  'v3.1.0',  'failed',      '2026-11-05 09:15+00'),
 (4, 'worker',  'v3.1.1',  'success',     '2026-11-05 11:40+00'),
 (5, 'auth',    'v0.9.7',  'rolled_back', '2026-11-08 18:20+00'),
 (6, 'api',     'v2.15.0', 'success',     '2026-11-12 10:05+00'),
 (7, 'billing', 'v1.9.0',  'failed',      '2026-11-17 15:50+00');

CREATE INDEX bugs_status_idx ON bugs (status);
ANALYZE users;
ANALYZE bugs;
ANALYZE deployments;
