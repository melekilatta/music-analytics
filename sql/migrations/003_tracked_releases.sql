-- Миграция 003. Безопасна для повторного запуска (IF NOT EXISTS).

-- Новые релизы отслеживаемых артистов (источник: Spotify /artists/{id}/albums).
-- Снимки popularity альбомов и их треков пишутся в bronze.raw_response
-- (entity_type = 'album' и 'track'), здесь хранится только справочник.
CREATE TABLE IF NOT EXISTS meta.tracked_releases (
  spotify_album_id  TEXT PRIMARY KEY,
  spotify_artist_id TEXT NOT NULL,
  name              TEXT,
  album_group       TEXT,                 -- album / single
  release_date      DATE NOT NULL,
  spotify_track_ids TEXT[],               -- заполняется при первом снимке альбома
  first_seen_date   DATE NOT NULL DEFAULT CURRENT_DATE,
  is_active         BOOLEAN NOT NULL DEFAULT TRUE,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_tracked_releases_active
  ON meta.tracked_releases (is_active, release_date);
