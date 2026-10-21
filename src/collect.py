import os
import sys
import time
import json
from datetime import date, datetime
from typing import Optional, Dict, Any, Set, List

import requests
import psycopg2
from psycopg2.extras import Json
from dotenv import load_dotenv

load_dotenv(override=True)

# Конфигурация из .env
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_USER = os.getenv("PG_USER", "pas_user")
PG_PASSWORD = os.getenv("PG_PASSWORD", "pas_secret_password")
PG_DB = os.getenv("PG_DB", "music_analytics")

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

MAX_TRACKED_TRACKS = int(os.getenv("MAX_TRACKED_TRACKS", "20"))
CHART_LIMIT = int(os.getenv("CHART_LIMIT", "20"))
DEEZER_GENRE_IDS = [
    int(x.strip()) for x in os.getenv("DEEZER_GENRE_IDS", "0").split(",") if x.strip()
]
TRACK_RETENTION_DAYS = int(os.getenv("TRACK_RETENTION_DAYS", "60"))
REQUEST_DELAY_SEC = float(os.getenv("REQUEST_DELAY_SEC", "0.25"))

DEEZER_BASE = "https://api.deezer.com"
SPOTIFY_BASE = "https://api.spotify.com/v1"


class SpotifyClient:
    """Клиент для Spotify API с автоматическим получением токена и обработкой 429."""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token: Optional[str] = None
        self.token_expires_at: float = 0.0
        self.session = requests.Session()

    def _ensure_token(self):
        if self.token and time.time() < self.token_expires_at - 60:
            return
        if not self.client_id or not self.client_secret:
            raise RuntimeError("Не заданы SPOTIFY_CLIENT_ID или SPOTIFY_CLIENT_SECRET в .env")

        resp = self.session.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
            timeout=(10, 30),
        )
        resp.raise_for_status()
        data = resp.json()
        self.token = data["access_token"]
        self.token_expires_at = time.time() + int(data.get("expires_in", 3600))

    def get(self, endpoint: str, **params) -> tuple[int, Optional[Dict[str, Any]]]:
        self._ensure_token()
        url = f"{SPOTIFY_BASE}{endpoint}"
        headers = {"Authorization": f"Bearer {self.token}"}

        for attempt in range(3):
            try:
                resp = self.session.get(url, headers=headers, params=params, timeout=(10, 30))
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 5))
                    print(f"[Spotify 429] Превышен лимит, ждем {retry_after} сек...")
                    time.sleep(retry_after + 1)
                    continue
                if resp.status_code == 200:
                    return resp.status_code, resp.json()
                return resp.status_code, {"error_status": resp.status_code, "text": resp.text[:500]}
            except requests.RequestException as e:
                if attempt == 2:
                    return 0, {"exception": str(e)}
                time.sleep(2)
        return 0, None


def deezer_get(session: requests.Session, endpoint: str, **params) -> tuple[int, Optional[Dict[str, Any]]]:
    url = f"{DEEZER_BASE}{endpoint}"
    for attempt in range(3):
        try:
            resp = session.get(url, params=params, timeout=(10, 30))
            if resp.status_code == 200:
                data = resp.json()
                # У Deezer ошибки часто приходят с HTTP 200 внутри поля "error"
                if isinstance(data, dict) and "error" in data:
                    err_code = data["error"].get("code", 0)
                    if err_code == 4:  # Quota limit exceeded
                        time.sleep(5)
                        continue
                    return 400, data
                return 200, data
            return resp.status_code, {"error_status": resp.status_code, "text": resp.text[:500]}
        except requests.RequestException as e:
            if attempt == 2:
                return 0, {"exception": str(e)}
            time.sleep(2)
    return 0, None


class RunLogger:
    """Управляет записью в meta.ingestion_log и сохранением в bronze.raw_response."""

    def __init__(self, conn, source: str, endpoint: str, snapshot_date: date):
        self.conn = conn
        self.source = source
        self.endpoint = endpoint
        self.snapshot_date = snapshot_date
        self.requests_total = 0
        self.requests_failed = 0
        self.rows_inserted = 0
        self.errors: List[str] = []
        self.run_id = self._start_run()

    def _start_run(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO meta.ingestion_log (source, endpoint, status)
                VALUES (%s, %s, 'running')
                RETURNING run_id;
                """,
                (self.source, self.endpoint),
            )
            run_id = cur.fetchone()[0]
        self.conn.commit()
        return run_id

    def get_already_collected_ids(self, entity_type: str) -> Set[str]:
        """Возвращает множество entity_id, которые уже успешно записаны за сегодня."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id
                FROM bronze.raw_response
                WHERE source = %s
                  AND entity_type = %s
                  AND snapshot_date = %s
                  AND http_status = 200;
                """,
                (self.source, entity_type, self.snapshot_date),
            )
            return {row[0] for row in cur.fetchall()}

    def save_raw(self, entity_type: str, entity_id: str, http_status: int, payload: Optional[Dict[str, Any]]):
        self.requests_total += 1
        if http_status != 200 or payload is None:
            self.requests_failed += 1
            self.errors.append(f"{entity_type}:{entity_id} -> status {http_status}")
            return

        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bronze.raw_response
                    (source, entity_type, entity_id, snapshot_date, run_id, http_status, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source, entity_type, entity_id, snapshot_date) DO NOTHING;
                """,
                (
                    self.source,
                    entity_type,
                    str(entity_id),
                    self.snapshot_date,
                    self.run_id,
                    http_status,
                    Json(payload),
                ),
            )
            self.rows_inserted += cur.rowcount
        self.conn.commit()

    def finish(self, fatal_error: Optional[str] = None):
        if fatal_error:
            status = "failed"
            self.errors.append(fatal_error)
        elif self.requests_failed > 0 and self.requests_failed == self.requests_total:
            status = "failed"
        elif self.requests_failed > 0:
            status = "partial"
        else:
            status = "success"

        err_text = "; ".join(self.errors[:10]) if self.errors else None
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE meta.ingestion_log
                SET finished_at = now(),
                    status = %s,
                    requests_total = %s,
                    requests_failed = %s,
                    rows_inserted = %s,
                    error = %s
                WHERE run_id = %s;
                """,
                (status, self.requests_total, self.requests_failed, self.rows_inserted, err_text, self.run_id),
            )
        self.conn.commit()
        print(
            f"[{self.source}] Завершено (run_id={self.run_id}, status={status}): "
            f"запросов={self.requests_total}, ошибок={self.requests_failed}, "
            f"новых строк в bronze={self.rows_inserted}"
        )


def upsert_chart_tracks_to_catalog(conn, chart_tracks: List[Dict[str, Any]], today: date):
    """Добавляет новые треки из чартов в справочник (до лимита MAX_TRACKED_TRACKS) и обновляет last_chart_date."""
    with conn.cursor() as cur:
        # Деактивируем треки, которые не появлялись в чартах более TRACK_RETENTION_DAYS дней
        cur.execute(
            """
            UPDATE meta.tracked_tracks
            SET is_active = FALSE, updated_at = now()
            WHERE is_active = TRUE
              AND (%s::date - last_chart_date) > %s;
            """,
            (today, TRACK_RETENTION_DAYS),
        )

        # Считаем текущее число активных треков
        cur.execute("SELECT count(*) FROM meta.tracked_tracks WHERE is_active = TRUE;")
        active_count = cur.fetchone()[0]

        for item in chart_tracks:
            dz_track_id = str(item["id"])
            dz_artist_id = str((item.get("artist") or {}).get("id", ""))
            title = item.get("title", "")

            # Проверяем, есть ли уже такой трек в справочнике
            cur.execute(
                "SELECT deezer_track_id, is_active FROM meta.tracked_tracks WHERE deezer_track_id = %s;",
                (dz_track_id,),
            )
            existing = cur.fetchone()

            if existing:
                cur.execute(
                    """
                    UPDATE meta.tracked_tracks
                    SET last_chart_date = %s, is_active = TRUE, updated_at = now()
                    WHERE deezer_track_id = %s;
                    """,
                    (today, dz_track_id),
                )
            elif active_count < MAX_TRACKED_TRACKS:
                cur.execute(
                    """
                    INSERT INTO meta.tracked_tracks
                        (deezer_track_id, deezer_artist_id, title, first_seen_date, last_chart_date, is_active)
                    VALUES (%s, %s, %s, %s, %s, TRUE);
                    """,
                    (dz_track_id, dz_artist_id, title, today, today),
                )
                active_count += 1
    conn.commit()


def main():
    today = date.today()
    print(f"=== Старт сбора данных за {today} (лимит треков: {MAX_TRACKED_TRACKS}) ===")

    conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname=PG_DB
    )
    dz_session = requests.Session()

    # ---------------------------------------------------------
    # ЭТАП 1: Сбор данных Deezer (чарты -> справочник -> треки -> артисты)
    # ---------------------------------------------------------
    dz_logger = RunLogger(conn, source="deezer", endpoint="charts_tracks_artists", snapshot_date=today)
    try:
        done_charts = dz_logger.get_already_collected_ids("chart")
        all_chart_items: List[Dict[str, Any]] = []

        for genre_id in DEEZER_GENRE_IDS:
            chart_entity_id = f"genre_{genre_id}"
            status, data = deezer_get(dz_session, f"/chart/{genre_id}/tracks", limit=CHART_LIMIT)
            time.sleep(REQUEST_DELAY_SEC)

            if chart_entity_id not in done_charts:
                dz_logger.save_raw("chart", chart_entity_id, status, data)

            if status == 200 and data and "data" in data:
                all_chart_items.extend(data["data"])

        # Обновляем справочник отслеживаемых треков
        if all_chart_items:
            upsert_chart_tracks_to_catalog(conn, all_chart_items, today)

        # Загружаем список всех активных отслеживаемых треков
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT deezer_track_id, isrc, deezer_artist_id
                FROM meta.tracked_tracks
                WHERE is_active = TRUE
                ORDER BY first_seen_date ASC
                LIMIT %s;
                """,
                (MAX_TRACKED_TRACKS,),
            )
            active_tracks = cur.fetchall()

        done_dz_tracks = dz_logger.get_already_collected_ids("track")
        done_dz_artists = dz_logger.get_already_collected_ids("artist")
        dz_artists_to_fetch: Set[str] = set()

        for dz_track_id, current_isrc, dz_artist_id in active_tracks:
            if dz_artist_id:
                dz_artists_to_fetch.add(dz_artist_id)

            # Если трек за сегодня уже собран и у него известен ISRC в справочнике — пропускаем запрос
            if dz_track_id in done_dz_tracks and current_isrc:
                continue

            status, full_track = deezer_get(dz_session, f"/track/{dz_track_id}")
            time.sleep(REQUEST_DELAY_SEC)

            if dz_track_id not in done_dz_tracks:
                dz_logger.save_raw("track", dz_track_id, status, full_track)

            if status == 200 and full_track:
                fetched_isrc = full_track.get("isrc")
                fetched_artist_id = str((full_track.get("artist") or {}).get("id", dz_artist_id or ""))
                if fetched_artist_id:
                    dz_artists_to_fetch.add(fetched_artist_id)

                if fetched_isrc and fetched_isrc != current_isrc:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE meta.tracked_tracks
                            SET isrc = %s, deezer_artist_id = COALESCE(NULLIF(%s, ''), deezer_artist_id), updated_at = now()
                            WHERE deezer_track_id = %s;
                            """,
                            (fetched_isrc, fetched_artist_id, dz_track_id),
                        )
                    conn.commit()

        # Собираем артистов Deezer (nb_fan, nb_album)
        for dz_artist_id in dz_artists_to_fetch:
            if not dz_artist_id or dz_artist_id in done_dz_artists:
                continue
            status, artist_data = deezer_get(dz_session, f"/artist/{dz_artist_id}")
            time.sleep(REQUEST_DELAY_SEC)
            dz_logger.save_raw("artist", dz_artist_id, status, artist_data)

        dz_logger.finish()
    except Exception as e:
        dz_logger.finish(fatal_error=str(e))
        raise

    # ---------------------------------------------------------
    # ЭТАП 2: Сбор данных Spotify (разовый маппинг ISRC -> треки -> артисты -> альбомы)
    # ---------------------------------------------------------
    sp_logger = RunLogger(conn, source="spotify", endpoint="tracks_artists_albums", snapshot_date=today)
    try:
        sp_client = SpotifyClient(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT deezer_track_id, isrc, spotify_track_id, spotify_artist_id, spotify_album_id
                FROM meta.tracked_tracks
                WHERE is_active = TRUE AND isrc IS NOT NULL
                ORDER BY first_seen_date ASC
                LIMIT %s;
                """,
                (MAX_TRACKED_TRACKS,),
            )
            tracks_for_sp = cur.fetchall()

        done_sp_tracks = sp_logger.get_already_collected_ids("track")
        done_sp_artists = sp_logger.get_already_collected_ids("artist")
        done_sp_albums = sp_logger.get_already_collected_ids("album")

        sp_artists_to_fetch: Set[str] = set()
        sp_albums_to_fetch: Set[str] = set()

        for dz_track_id, isrc, sp_track_id, sp_artist_id, sp_album_id in tracks_for_sp:
            # 1. Разовый поиск по ISRC, если spotify_track_id еще не сохранен в справочнике
            if not sp_track_id:
                status, search_res = sp_client.get("/search", q=f"isrc:{isrc}", type="track", limit=1)
                time.sleep(REQUEST_DELAY_SEC)

                if status == 200 and search_res:
                    items = ((search_res.get("tracks") or {}).get("items")) or []
                    if items:
                        sp_item = items[0]
                        sp_track_id = sp_item["id"]
                        sp_album_id = (sp_item.get("album") or {}).get("id")
                        sp_artists = sp_item.get("artists") or []
                        sp_artist_id = sp_artists[0]["id"] if sp_artists else None

                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE meta.tracked_tracks
                                SET spotify_track_id = %s,
                                    spotify_artist_id = %s,
                                    spotify_album_id = %s,
                                    updated_at = now()
                                WHERE deezer_track_id = %s;
                                """,
                                (sp_track_id, sp_artist_id, sp_album_id, dz_track_id),
                            )
                        conn.commit()
                        # Т.к. поиск уже вернул полный объект трека с popularity, сразу сохраним его за сегодня!
                        if sp_track_id not in done_sp_tracks:
                            sp_logger.save_raw("track", sp_track_id, 200, sp_item)
                            done_sp_tracks.add(sp_track_id)
                else:
                    # Логируем неуспешный поиск
                    sp_logger.save_raw("isrc_search", isrc, status, search_res)

            if not sp_track_id:
                continue

            if sp_artist_id:
                sp_artists_to_fetch.add(sp_artist_id)
            if sp_album_id:
                sp_albums_to_fetch.add(sp_album_id)

            # 2. Ежедневный опрос /tracks/{id}, если еще не собран сегодня
            if sp_track_id not in done_sp_tracks:
                status, tr_data = sp_client.get(f"/tracks/{sp_track_id}")
                time.sleep(REQUEST_DELAY_SEC)
                sp_logger.save_raw("track", sp_track_id, status, tr_data)

        # 3. Ежедневный опрос артистов Spotify (/artists/{id} -> popularity, followers.total, genres)
        for artist_id in sp_artists_to_fetch:
            if artist_id in done_sp_artists:
                continue
            status, art_data = sp_client.get(f"/artists/{artist_id}")
            time.sleep(REQUEST_DELAY_SEC)
            sp_logger.save_raw("artist", artist_id, status, art_data)

        # 4. Ежедневный опрос альбомов Spotify (/albums/{id} -> popularity, release_date)
        for album_id in sp_albums_to_fetch:
            if album_id in done_sp_albums:
                continue
            status, alb_data = sp_client.get(f"/albums/{album_id}")
            time.sleep(REQUEST_DELAY_SEC)
            sp_logger.save_raw("album", album_id, status, alb_data)

        sp_logger.finish()
    except Exception as e:
        sp_logger.finish(fatal_error=str(e))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()