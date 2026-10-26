-- Миграция 002. Безопасна для повторного запуска (IF NOT EXISTS).

-- Результат сопоставления трека со Spotify по ISRC
ALTER TABLE meta.tracked_tracks
  ADD COLUMN IF NOT EXISTS spotify_match_status     TEXT,         -- matched / not_found
  ADD COLUMN IF NOT EXISTS spotify_match_checked_at TIMESTAMPTZ;

-- Сколько треков не поместилось в лимит справочника за запуск
ALTER TABLE meta.ingestion_log
  ADD COLUMN IF NOT EXISTS items_skipped INT NOT NULL DEFAULT 0;

-- Ускоряет проверку "что уже собрано за сегодня"
CREATE INDEX IF NOT EXISTS ix_raw_response_snapshot
  ON bronze.raw_response (source, entity_type, snapshot_date);
