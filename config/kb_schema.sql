-- KB Schema для total-recall
-- Hot storage: временные записи с TTL 7 дней
-- Cold storage: перманентные записи

CREATE TABLE kb_hot (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_url  TEXT,
  source_tool TEXT,
  title       TEXT NOT NULL,
  summary     TEXT NOT NULL,
  content     TEXT NOT NULL,
  category    TEXT DEFAULT 'search',
  created_at  TIMESTAMPTZ DEFAULT now(),
  expires_at  TIMESTAMPTZ DEFAULT now() + interval '7 days',
  promoted    BOOLEAN DEFAULT false
);

CREATE TABLE kb_cold (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_url       TEXT,
  source_tool      TEXT,
  title            TEXT NOT NULL,
  summary          TEXT NOT NULL,
  content          TEXT NOT NULL,
  category         TEXT DEFAULT 'search',
  created_at       TIMESTAMPTZ NOT NULL,
  promoted_at      TIMESTAMPTZ DEFAULT now(),
  last_accessed_at TIMESTAMPTZ,
  access_count     INT DEFAULT 0,
  is_stale         BOOLEAN DEFAULT false
);

-- Индексы для ускорения поиска
CREATE INDEX idx_kb_hot_category ON kb_hot(category);
CREATE INDEX idx_kb_hot_expires ON kb_hot(expires_at);
CREATE INDEX idx_kb_cold_category ON kb_cold(category);
CREATE INDEX idx_kb_cold_last_accessed ON kb_cold(last_accessed_at);
