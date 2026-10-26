"""Ежедневный сборщик снимков Deezer и Spotify в bronze.raw_response.

Запуск: python src/collect.py
Конфигурация: переменные окружения / файл .env (см. .env.example).
Код выхода: 0 - все этапы прошли, 1 - хотя бы один этап завершился ошибкой.
"""
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import psycopg2
import requests
from dotenv import load_dotenv
from psycopg2.extras import Json

# override=False (по умолчанию): переменные, уже заданные в окружении
# (Docker, планировщик, CI), не перезаписываются значениями из .env
load_dotenv()


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"Не задана обязательная переменная окружения {name} (см. .env.example)")
    return value


PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "15432"))
PG_USER = require_env("PG_USER")
PG_PASSWORD = require_env("PG_PASSWORD")
PG_DB = require_env("PG_DB")

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

MAX_TRACKED_TRACKS = int(os.getenv("MAX_TRACKED_TRACKS", "20"))
CHART_LIMIT = int(os.getenv("CHART_LIMIT", "20"))
DEEZER_GENRE_IDS = [
    int(x.strip()) for x in os.getenv("DEEZER_GENRE_IDS", "0").split(",") if x.strip()
]
TRACK_RETENTION_DAYS = int(os.getenv("TRACK_RETENTION_DAYS", "60"))
REQUEST_DELAY_SEC = float(os.getenv("REQUEST_DELAY_SEC", "0.25"))

# Защита от зависаний и каскада ошибок
MAX_RETRY_AFTER_SEC = int(os.getenv("MAX_RETRY_AFTER_SEC", "120"))
MAX_CONSECUTIVE_FAILURES = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "10"))
# Через сколько дней повторять поиск по ISRC для треков, не найденных в Spotify
ISRC_RECHECK_DAYS = int(os.getenv("ISRC_RECHECK_DAYS", "14"))

# Новые релизы отслеживаемых артистов (Spotify)
ENABLE_RELEASES = os.getenv("ENABLE_RELEASES", "true").lower() in ("1", "true", "yes")
RELEASE_DISCOVERY_DAYS = int(os.getenv("RELEASE_DISCOVERY_DAYS", "14"))  # релиз младше N дней считается новым
RELEASE_TRACK_DAYS = int(os.getenv("RELEASE_TRACK_DAYS", "28"))          # сколько дней снимаем релиз после выхода
MAX_TRACKED_RELEASES = int(os.getenv("MAX_TRACKED_RELEASES", "150"))     # лимит одновременно активных релизов

DEEZER_BASE = "https://api.deezer.com"
SPOTIFY_BASE = "https://api.spotify.com/v1"
REQUEST_TIMEOUT = (10, 30)  # (подключение, чтение), сек
MAX_ATTEMPTS = 3

Response = Tuple[int, Optional[Dict[str, Any]]]


class RateLimited(Exception):
    """Сервер просит ждать дольше допустимого (MAX_RETRY_AFTER_SEC)."""


class StageAborted(Exception):
    """Этап остановлен: слишком много неудачных запросов подряд."""


# ---------------------------------------------------------------------------
# HTTP-клиенты
# ---------------------------------------------------------------------------
def _retry_after_seconds(resp: requests.Response, default: int = 5) -> int:
    try:
        return int(float(resp.headers.get("Retry-After", default)))
    except (TypeError, ValueError):
        return default


class SpotifyClient:
    """Клиент Spotify API: токен, повтор при 401/5xx, ограниченное ожидание при 429."""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token: Optional[str] = None
        self.token_expires_at: float = 0.0
        self.session = requests.Session()

    def _ensure_token(self) -> None:
        if self.token and time.time() < self.token_expires_at - 60:
            return
        if not self.client_id or not self.client_secret:
            raise RuntimeError("Не заданы SPOTIFY_CLIENT_ID или SPOTIFY_CLIENT_SECRET")

        resp = self.session.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            # неверные ключи - не временный сбой, повторять бессмысленно
            raise RuntimeError(f"Spotify: не удалось получить токен (HTTP {resp.status_code})")
        data = resp.json()
        self.token = data["access_token"]
        self.token_expires_at = time.time() + int(data.get("expires_in", 3600))

    def get(self, endpoint: str, **params) -> Response:
        url = f"{SPOTIFY_BASE}{endpoint}"
        last: Response = (0, {"exception": "запрос не выполнялся"})

        for attempt in range(MAX_ATTEMPTS):
            try:
                self._ensure_token()
                resp = self.session.get(
                    url,
                    headers={"Authorization": f"Bearer {self.token}"},
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as e:
                last = (0, {"exception": str(e)})
                time.sleep(2 * (attempt + 1))
                continue

            if resp.status_code == 200:
                return 200, resp.json()

            if resp.status_code == 401:  # токен протух - получим новый и повторим
                self.token = None
                last = (401, {"error_status": 401, "text": resp.text[:300]})
                continue

            if resp.status_code == 429:
                retry_after = _retry_after_seconds(resp)
                if retry_after > MAX_RETRY_AFTER_SEC:
                    raise RateLimited(f"Spotify просит ждать {retry_after} сек (лимит {MAX_RETRY_AFTER_SEC})")
                print(f"[Spotify 429] лимит запросов, жду {retry_after} сек...")
                time.sleep(retry_after + 1)
                last = (429, {"error_status": 429})
                continue

            if resp.status_code >= 500:
                last = (resp.status_code, {"error_status": resp.status_code, "text": resp.text[:300]})
                time.sleep(2 * (attempt + 1))
                continue

            # остальные 4xx (403, 404...) повторять нет смысла
            return resp.status_code, {"error_status": resp.status_code, "text": resp.text[:500]}

        return last


def deezer_get(session: requests.Session, endpoint: str, **params) -> Response:
    url = f"{DEEZER_BASE}{endpoint}"
    last: Response = (0, {"exception": "запрос не выполнялся"})

    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            last = (0, {"exception": str(e)})
            time.sleep(2 * (attempt + 1))
            continue

        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                last = (502, {"text": resp.text[:300]})
                time.sleep(2 * (attempt + 1))
                continue
            # У Deezer ошибки часто приходят с HTTP 200 внутри поля "error"
            if isinstance(data, dict) and "error" in data:
                err = data["error"] if isinstance(data["error"], dict) else {}
                if err.get("code") == 4:  # превышена квота запросов
                    time.sleep(5)
                    last = (429, data)
                    continue
                return 400, data
            return 200, data

        if resp.status_code >= 500 or resp.status_code == 429:
            last = (resp.status_code, {"error_status": resp.status_code, "text": resp.text[:300]})
            time.sleep(2 * (attempt + 1))
            continue

        return resp.status_code, {"error_status": resp.status_code, "text": resp.text[:500]}

    return last


# ---------------------------------------------------------------------------
# Журнал запусков и запись в bronze
# ---------------------------------------------------------------------------
class RunLogger:
    """Запись в meta.ingestion_log и сохранение ответов в bronze.raw_response."""

    def __init__(self, conn, source: str, endpoint: str, snapshot_date: date):
        self.conn = conn
        self.source = source
        self.endpoint = endpoint
        self.snapshot_date = snapshot_date
        self.requests_total = 0
        self.requests_failed = 0
        self.rows_inserted = 0
        self.items_skipped = 0  # треки, не поместившиеся в лимит справочника
        self.consecutive_failures = 0
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
        """entity_id, уже успешно записанные за snapshot_date."""
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

    def save_raw(self, entity_type: str, entity_id: str, http_status: int,
                 payload: Optional[Dict[str, Any]]) -> None:
        self.requests_total += 1
        if http_status != 200 or payload is None:
            self.requests_failed += 1
            self.consecutive_failures += 1
            self.errors.append(f"{entity_type}:{entity_id} -> status {http_status}")
            if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise StageAborted(
                    f"{self.consecutive_failures} неудачных запросов подряд, этап остановлен"
                )
            return

        self.consecutive_failures = 0
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bronze.raw_response
                    (source, entity_type, entity_id, snapshot_date, run_id, http_status, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source, entity_type, entity_id, snapshot_date) DO NOTHING;
                """,
                (self.source, entity_type, str(entity_id),
                 self.snapshot_date, self.run_id, http_status, Json(payload)),
            )
            self.rows_inserted += cur.rowcount
        self.conn.commit()

    def finish(self, fatal_error: Optional[str] = None) -> None:
        if fatal_error:
            # если что-то уже записано, запуск частично успешен
            status = "partial" if self.rows_inserted > 0 else "failed"
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
                    items_skipped = %s,
                    error = %s
                WHERE run_id = %s;
                """,
                (status, self.requests_total, self.requests_failed, self.rows_inserted,
                 self.items_skipped, err_text, self.run_id),
            )
        self.conn.commit()
        print(
            f"[{self.source}] Завершено (run_id={self.run_id}, status={status}): "
            f"запросов={self.requests_total}, ошибок={self.requests_failed}, "
            f"новых строк в bronze={self.rows_inserted}, не поместилось в справочник={self.items_skipped}"
        )


# ---------------------------------------------------------------------------
# Справочник отслеживаемых треков
# ---------------------------------------------------------------------------
def upsert_chart_tracks_to_catalog(conn, chart_tracks: List[Dict[str, Any]], today: date) -> int:
    """Добавляет треки из чартов в справочник (до лимита MAX_TRACKED_TRACKS).

    Возвращает число треков, которые не удалось добавить из-за лимита.
    """
    skipped = 0
    seen: Set[str] = set()

    with conn.cursor() as cur:
        # Деактивируем треки, не появлявшиеся в чартах дольше TRACK_RETENTION_DAYS
        cur.execute(
            """
            UPDATE meta.tracked_tracks
            SET is_active = FALSE, updated_at = now()
            WHERE is_active = TRUE
              AND (%s::date - last_chart_date) > %s;
            """,
            (today, TRACK_RETENTION_DAYS),
        )

        cur.execute("SELECT count(*) FROM meta.tracked_tracks WHERE is_active = TRUE;")
        active_count = cur.fetchone()[0]

        for item in chart_tracks:
            if item.get("id") is None:
                continue
            dz_track_id = str(item["id"])
            if dz_track_id in seen:  # трек может быть сразу в нескольких жанровых чартах
                continue
            seen.add(dz_track_id)

            dz_artist_id = str((item.get("artist") or {}).get("id", ""))
            title = item.get("title", "")

            cur.execute(
                "SELECT is_active FROM meta.tracked_tracks WHERE deezer_track_id = %s;",
                (dz_track_id,),
            )
            row = cur.fetchone()

            if row is not None:
                was_active = row[0]
                if was_active:
                    cur.execute(
                        "UPDATE meta.tracked_tracks SET last_chart_date = %s, updated_at = now() "
                        "WHERE deezer_track_id = %s;",
                        (today, dz_track_id),
                    )
                elif active_count < MAX_TRACKED_TRACKS:  # вернулся в чарт - реактивируем
                    cur.execute(
                        "UPDATE meta.tracked_tracks SET last_chart_date = %s, is_active = TRUE, "
                        "updated_at = now() WHERE deezer_track_id = %s;",
                        (today, dz_track_id),
                    )
                    active_count += 1
                else:
                    skipped += 1
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
            else:
                skipped += 1

    conn.commit()
    return skipped


# ---------------------------------------------------------------------------
# Этап 1: Deezer (чарты -> справочник -> треки -> артисты)
# ---------------------------------------------------------------------------
def collect_deezer(conn, today: date) -> None:
    dz_session = requests.Session()
    logger = RunLogger(conn, source="deezer", endpoint="charts_tracks_artists", snapshot_date=today)
    try:
        done_charts = logger.get_already_collected_ids("chart")
        all_chart_items: List[Dict[str, Any]] = []

        for genre_id in DEEZER_GENRE_IDS:
            chart_entity_id = f"genre_{genre_id}"
            status, data = deezer_get(dz_session, f"/chart/{genre_id}/tracks", limit=CHART_LIMIT)
            time.sleep(REQUEST_DELAY_SEC)

            if chart_entity_id not in done_charts:
                logger.save_raw("chart", chart_entity_id, status, data)

            if status == 200 and data and "data" in data:
                all_chart_items.extend(data["data"])

        if all_chart_items:
            logger.items_skipped = upsert_chart_tracks_to_catalog(conn, all_chart_items, today)

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

        done_tracks = logger.get_already_collected_ids("track")
        done_artists = logger.get_already_collected_ids("artist")
        artists_to_fetch: Set[str] = set()

        for dz_track_id, current_isrc, dz_artist_id in active_tracks:
            if dz_artist_id:
                artists_to_fetch.add(dz_artist_id)

            # трек уже собран сегодня и ISRC известен - запрос не нужен
            if dz_track_id in done_tracks and current_isrc:
                continue

            status, full_track = deezer_get(dz_session, f"/track/{dz_track_id}")
            time.sleep(REQUEST_DELAY_SEC)

            if dz_track_id not in done_tracks:
                logger.save_raw("track", dz_track_id, status, full_track)

            if status == 200 and full_track:
                fetched_isrc = full_track.get("isrc")
                fetched_artist_id = str((full_track.get("artist") or {}).get("id", dz_artist_id or ""))
                if fetched_artist_id:
                    artists_to_fetch.add(fetched_artist_id)

                if fetched_isrc and fetched_isrc != current_isrc:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE meta.tracked_tracks
                            SET isrc = %s,
                                deezer_artist_id = COALESCE(NULLIF(%s, ''), deezer_artist_id),
                                updated_at = now()
                            WHERE deezer_track_id = %s;
                            """,
                            (fetched_isrc, fetched_artist_id, dz_track_id),
                        )
                    conn.commit()

        for dz_artist_id in artists_to_fetch:
            if not dz_artist_id or dz_artist_id in done_artists:
                continue
            status, artist_data = deezer_get(dz_session, f"/artist/{dz_artist_id}")
            time.sleep(REQUEST_DELAY_SEC)
            logger.save_raw("artist", dz_artist_id, status, artist_data)

        logger.finish()
    except Exception as e:
        conn.rollback()
        logger.finish(fatal_error=f"{type(e).__name__}: {e}")
        raise


# ---------------------------------------------------------------------------
# Этап 2: Spotify (ISRC -> треки -> артисты -> альбомы)
# ---------------------------------------------------------------------------
def collect_spotify(conn, today: date) -> None:
    logger = RunLogger(conn, source="spotify", endpoint="tracks_artists_albums", snapshot_date=today)
    try:
        client = SpotifyClient(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT deezer_track_id, isrc, spotify_track_id, spotify_artist_id, spotify_album_id,
                       (spotify_track_id IS NULL
                        AND (spotify_match_status IS DISTINCT FROM 'not_found'
                             OR spotify_match_checked_at < now() - make_interval(days => %s))
                       ) AS needs_search
                FROM meta.tracked_tracks
                WHERE is_active = TRUE AND isrc IS NOT NULL
                ORDER BY first_seen_date ASC
                LIMIT %s;
                """,
                (ISRC_RECHECK_DAYS, MAX_TRACKED_TRACKS),
            )
            tracks_for_sp = cur.fetchall()

        done_tracks = logger.get_already_collected_ids("track")
        done_artists = logger.get_already_collected_ids("artist")
        done_albums = logger.get_already_collected_ids("album")

        artists_to_fetch: Set[str] = set()
        albums_to_fetch: Set[str] = set()

        for dz_track_id, isrc, sp_track_id, sp_artist_id, sp_album_id, needs_search in tracks_for_sp:
            # 1. Поиск по ISRC (однократно; не найденные повторяются раз в ISRC_RECHECK_DAYS дней)
            if needs_search:
                status, search_res = client.get("/search", q=f"isrc:{isrc}", type="track", limit=5)
                time.sleep(REQUEST_DELAY_SEC)

                if status == 200 and search_res is not None:
                    items = ((search_res.get("tracks") or {}).get("items")) or []
                    if items:
                        # один ISRC может соответствовать нескольким релизам;
                        # берём версию с максимальной popularity - выбор стабилен
                        sp_item = max(items, key=lambda x: x.get("popularity") or 0)
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
                                    spotify_match_status = 'matched',
                                    spotify_match_checked_at = now(),
                                    updated_at = now()
                                WHERE deezer_track_id = %s;
                                """,
                                (sp_track_id, sp_artist_id, sp_album_id, dz_track_id),
                            )
                        conn.commit()
                        # поиск уже вернул полный объект трека (с popularity) - сохраняем его
                        if sp_track_id not in done_tracks:
                            logger.save_raw("track", sp_track_id, 200, sp_item)
                            done_tracks.add(sp_track_id)
                    else:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE meta.tracked_tracks
                                SET spotify_match_status = 'not_found',
                                    spotify_match_checked_at = now(),
                                    updated_at = now()
                                WHERE deezer_track_id = %s;
                                """,
                                (dz_track_id,),
                            )
                        conn.commit()
                else:
                    # ошибка поиска - статус не меняем, попробуем при следующем запуске
                    logger.save_raw("isrc_search", isrc, status, search_res)

            if not sp_track_id:
                continue

            if sp_artist_id:
                artists_to_fetch.add(sp_artist_id)
            if sp_album_id:
                albums_to_fetch.add(sp_album_id)

            # 2. Ежедневный снимок трека
            if sp_track_id not in done_tracks:
                status, tr_data = client.get(f"/tracks/{sp_track_id}")
                time.sleep(REQUEST_DELAY_SEC)
                logger.save_raw("track", sp_track_id, status, tr_data)

        # 3. Артисты: popularity, followers.total, genres
        for artist_id in artists_to_fetch:
            if artist_id in done_artists:
                continue
            status, art_data = client.get(f"/artists/{artist_id}")
            time.sleep(REQUEST_DELAY_SEC)
            logger.save_raw("artist", artist_id, status, art_data)

        # 4. Альбомы: popularity, release_date
        for album_id in albums_to_fetch:
            if album_id in done_albums:
                continue
            status, alb_data = client.get(f"/albums/{album_id}")
            time.sleep(REQUEST_DELAY_SEC)
            logger.save_raw("album", album_id, status, alb_data)

        logger.finish()
    except Exception as e:
        conn.rollback()
        logger.finish(fatal_error=f"{type(e).__name__}: {e}")
        raise


# ---------------------------------------------------------------------------
def parse_release_date(album: Dict[str, Any]) -> Optional[date]:
    """Дата релиза из ответа Spotify. Только точность 'day': для 'month'/'year' день неизвестен."""
    if album.get("release_date_precision") != "day":
        return None
    try:
        return date.fromisoformat(album["release_date"])
    except (KeyError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Этап 3: Spotify, новые релизы отслеживаемых артистов
# (все треки новых альбомов, независимо от попадания в чарт - контрольная группа)
# ---------------------------------------------------------------------------
def collect_spotify_releases(conn, today: date) -> None:
    if not ENABLE_RELEASES:
        print("[spotify/releases] отключено (ENABLE_RELEASES=false)")
        return

    logger = RunLogger(conn, source="spotify", endpoint="new_releases", snapshot_date=today)
    try:
        client = SpotifyClient(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)

        done_listings = logger.get_already_collected_ids("artist_albums")
        done_albums = logger.get_already_collected_ids("album")
        done_tracks = logger.get_already_collected_ids("track")

        with conn.cursor() as cur:
            # релизы старше RELEASE_TRACK_DAYS больше не снимаем (освобождаем лимит)
            cur.execute(
                """
                UPDATE meta.tracked_releases
                SET is_active = FALSE, updated_at = now()
                WHERE is_active = TRUE AND release_date < %s;
                """,
                (today - timedelta(days=RELEASE_TRACK_DAYS),),
            )
            cur.execute(
                """
                SELECT DISTINCT spotify_artist_id
                FROM meta.tracked_tracks
                WHERE is_active = TRUE AND spotify_artist_id IS NOT NULL;
                """
            )
            artist_ids = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT count(*) FROM meta.tracked_releases WHERE is_active = TRUE;")
            active_releases = cur.fetchone()[0]
        conn.commit()

        # 1. Поиск новых релизов: один запрос на артиста в день
        cutoff = today - timedelta(days=RELEASE_DISCOVERY_DAYS)
        latest_allowed = today + timedelta(days=1)  # запас на разницу часовых поясов
        for artist_id in artist_ids:
            if artist_id in done_listings:
                continue
            status, data = client.get(
                f"/artists/{artist_id}/albums", include_groups="album,single", limit=10
            )
            time.sleep(REQUEST_DELAY_SEC)

            if status == 200 and data is not None:
                with conn.cursor() as cur:
                    for alb in data.get("items") or []:
                        rel_date = parse_release_date(alb)
                        if rel_date is None or rel_date < cutoff or rel_date > latest_allowed:
                            continue
                        cur.execute(
                            "SELECT 1 FROM meta.tracked_releases WHERE spotify_album_id = %s;",
                            (alb["id"],),
                        )
                        if cur.fetchone():
                            continue
                        if active_releases >= MAX_TRACKED_RELEASES:
                            logger.items_skipped += 1
                            continue
                        cur.execute(
                            """
                            INSERT INTO meta.tracked_releases
                                (spotify_album_id, spotify_artist_id, name, album_group,
                                 release_date, first_seen_date)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (spotify_album_id) DO NOTHING;
                            """,
                            (alb["id"], artist_id, alb.get("name"), alb.get("album_group"),
                             rel_date, today),
                        )
                        active_releases += cur.rowcount
                conn.commit()  # сначала справочник, потом отметка "собрано" - безопасно при сбое
            logger.save_raw("artist_albums", artist_id, status, data)

        # 2. Ежедневные снимки активных релизов: альбом и все его треки
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT spotify_album_id, spotify_track_ids
                FROM meta.tracked_releases
                WHERE is_active = TRUE
                ORDER BY release_date DESC;
                """
            )
            releases = cur.fetchall()

        for album_id, track_ids in releases:
            if not (album_id in done_albums and track_ids):
                status, alb_data = client.get(f"/albums/{album_id}")
                time.sleep(REQUEST_DELAY_SEC)
                if album_id not in done_albums:
                    logger.save_raw("album", album_id, status, alb_data)
                    done_albums.add(album_id)
                if status == 200 and alb_data:
                    ids = [
                        t["id"] for t in ((alb_data.get("tracks") or {}).get("items") or [])
                        if t.get("id")
                    ]
                    if ids and ids != track_ids:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE meta.tracked_releases
                                SET spotify_track_ids = %s, updated_at = now()
                                WHERE spotify_album_id = %s;
                                """,
                                (ids, album_id),
                            )
                        conn.commit()
                    track_ids = ids or track_ids

            for track_id in track_ids or []:
                if track_id in done_tracks:
                    continue
                status, tr_data = client.get(f"/tracks/{track_id}")
                time.sleep(REQUEST_DELAY_SEC)
                logger.save_raw("track", track_id, status, tr_data)
                done_tracks.add(track_id)

        # 3. Добор: у треков за сегодня могут быть артисты (гости) и альбомы без снимка.
        #    Берём недостающие из Bronze, поэтому добор работает и после сбоя с повторным запуском.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT b.payload->'artists'->0->>'id'
                FROM bronze.raw_response b
                WHERE b.source = 'spotify' AND b.entity_type = 'track'
                  AND b.snapshot_date = %s AND b.http_status = 200
                  AND b.payload->'artists'->0->>'id' IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM bronze.raw_response a
                    WHERE a.source = 'spotify' AND a.entity_type = 'artist'
                      AND a.snapshot_date = b.snapshot_date
                      AND a.entity_id = b.payload->'artists'->0->>'id');
                """,
                (today,),
            )
            missing_artists = [r[0] for r in cur.fetchall()]
            cur.execute(
                """
                SELECT DISTINCT b.payload->'album'->>'id'
                FROM bronze.raw_response b
                WHERE b.source = 'spotify' AND b.entity_type = 'track'
                  AND b.snapshot_date = %s AND b.http_status = 200
                  AND b.payload->'album'->>'id' IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM bronze.raw_response a
                    WHERE a.source = 'spotify' AND a.entity_type = 'album'
                      AND a.snapshot_date = b.snapshot_date
                      AND a.entity_id = b.payload->'album'->>'id');
                """,
                (today,),
            )
            missing_albums = [r[0] for r in cur.fetchall()]

        for artist_id in missing_artists:
            status, art_data = client.get(f"/artists/{artist_id}")
            time.sleep(REQUEST_DELAY_SEC)
            logger.save_raw("artist", artist_id, status, art_data)

        for album_id in missing_albums:
            status, alb_data = client.get(f"/albums/{album_id}")
            time.sleep(REQUEST_DELAY_SEC)
            logger.save_raw("album", album_id, status, alb_data)

        logger.finish()
    except Exception as e:
        conn.rollback()
        logger.finish(fatal_error=f"{type(e).__name__}: {e}")
        raise


# ---------------------------------------------------------------------------
def main() -> int:
    # Дата снимка - по UTC, чтобы не зависеть от часового пояса машины
    today = datetime.now(timezone.utc).date()
    print(f"=== Старт сбора данных за {today} UTC (лимит треков: {MAX_TRACKED_TRACKS}) ===")

    conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname=PG_DB
    )
    failed: List[str] = []
    try:
        # Этапы независимы: сбой одного источника не отменяет сбор из другого
        for name, stage in (
            ("deezer", collect_deezer),
            ("spotify", collect_spotify),
            ("spotify/releases", collect_spotify_releases),
        ):
            try:
                stage(conn, today)
            except Exception as e:
                print(f"[{name}] этап прерван: {type(e).__name__}: {e}")
                failed.append(name)
    finally:
        conn.close()

    if failed:
        print(f"=== Завершено с ошибками: {', '.join(failed)} ===")
        return 1
    print("=== Сбор завершён успешно ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
