"""Интерактивные предметный, операционный и прогнозный дашборды Music Analytics.

Запуск:
    streamlit run dashboard/app.py

Данные читаются только из Gold/Silver-витрин PostgreSQL; приложение не изменяет БД.
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import psycopg2
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="Music Analytics / signal room",
    page_icon="♫",
    layout="wide",
    initial_sidebar_state="expanded",
)

BG = "#0b1020"
PANEL = "#111a2f"
PANEL_SOFT = "#17233d"
TEXT = "#edf4ff"
MUTED = "#94a8c7"
GRID = "#273653"
CYAN = "#57d3cf"
AMBER = "#f6bd63"
CORAL = "#fb7185"
BLUE = "#78a7ff"
GREEN = "#9bdd88"

DEEZER_GENRE_NAMES = {
    0: "Общий чарт (Все)",
    106: "Электро",
    116: "Рэп / хип-хоп",
    132: "Поп",
    152: "Рок / Хаус",
    165: "R&B / Альтернатива",
}


def genre_label(genre_id: int) -> str:
    return DEEZER_GENRE_NAMES.get(genre_id, f"Жанр Deezer №{genre_id}")


PLOTLY_TEMPLATE = go.layout.Template(
    layout=go.Layout(
        paper_bgcolor=PANEL,
        plot_bgcolor=PANEL,
        font={"family": "Aptos, Segoe UI, sans-serif", "color": TEXT, "size": 13},
        title={"font": {"size": 16, "color": TEXT}},
        xaxis={"gridcolor": GRID, "linecolor": GRID, "zerolinecolor": GRID},
        yaxis={"gridcolor": GRID, "linecolor": GRID, "zerolinecolor": GRID},
        legend={"bgcolor": "rgba(0,0,0,0)", "font": {"color": MUTED}},
        margin={"l": 18, "r": 28, "t": 42, "b": 18},
        hoverlabel={"bgcolor": "#202d49", "font": {"color": TEXT}},
    )
)

st.markdown(
    f"""
    <style>
      :root {{ color-scheme: dark; }}
      .stApp {{ background: {BG}; color: {TEXT}; }}
      [data-testid="stSidebar"] {{ background: #0e1629; border-right: 1px solid {GRID}; }}
      [data-testid="stSidebar"] * {{ color: {TEXT}; }}
      .block-container {{ padding: 2.1rem 3.1rem 3.5rem; max-width: 1500px; }}
      h1, h2, h3 {{ letter-spacing: -0.035em; }}
      h1 {{ font-size: clamp(2.4rem, 4vw, 4.8rem) !important; line-height: .94 !important; margin-bottom: .35rem !important; }}
      h2 {{ font-size: 1.55rem !important; margin-top: 1.5rem !important; }}
      h3 {{ font-size: 1.05rem !important; }}
      .eyebrow {{ color: {CYAN}; font-family: ui-monospace, SFMono-Regular, monospace; font-size: .72rem; letter-spacing: .12em; text-transform: uppercase; margin-bottom: .7rem; }}
      .lede {{ color: {MUTED}; font-size: 1rem; line-height: 1.55; max-width: 720px; }}
      .rule {{ height: 1px; background: linear-gradient(90deg, {CYAN}, transparent 72%); margin: 1.2rem 0 1.45rem; opacity: .7; }}
      .metric {{ background: {PANEL}; border: 1px solid {GRID}; border-left: 3px solid {CYAN}; padding: 1rem 1.05rem .9rem; min-height: 108px; }}
      .metric.amber {{ border-left-color: {AMBER}; }}
      .metric.coral {{ border-left-color: {CORAL}; }}
      .metric.blue {{ border-left-color: {BLUE}; }}
      .metric-label {{ color: {MUTED}; font-size: .74rem; line-height: 1.2; margin-bottom: .7rem; }}
      .metric-value {{ color: {TEXT}; font-size: 1.75rem; font-weight: 700; letter-spacing: -.04em; line-height: 1; }}
      .metric-note {{ color: {MUTED}; font-size: .72rem; margin-top: .55rem; }}
      .section-note {{ color: {MUTED}; font-size: .88rem; line-height: 1.45; margin-top: -.4rem; }}
      div[data-testid="stDataFrame"] {{ border: 1px solid {GRID}; }}
      .stTabs [data-baseweb="tab-list"] {{ gap: 1.3rem; border-bottom: 1px solid {GRID}; }}
      .stTabs [data-baseweb="tab"] {{ color: {MUTED}; padding: .7rem .15rem; }}
      .stTabs [aria-selected="true"] {{ color: {TEXT}; border-bottom-color: {CYAN}; }}
      .stButton button, .stDownloadButton button {{ border-color: {GRID}; background: {PANEL}; color: {TEXT}; }}
      .stButton button:hover, .stDownloadButton button:hover {{ border-color: {CYAN}; color: {CYAN}; }}
      @media (max-width: 900px) {{ .block-container {{ padding: 1.3rem 1rem 2rem; }} }}
    </style>
    """,
    unsafe_allow_html=True,
)


def _env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


@st.cache_resource(show_spinner=False)
def get_connection():
    conn = psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", "15432")),
        user=_env("PG_USER"),
        password=_env("PG_PASSWORD"),
        dbname=_env("PG_DB"),
    )
    conn.autocommit = True
    return conn


@st.cache_data(ttl=60, show_spinner=False)
def query(sql: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    connection = get_connection()
    return pd.read_sql_query(sql, connection, params=params)


def safe_query(sql: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    try:
        return query(sql, params)
    except Exception as exc:
        st.error(f"Не удалось прочитать витрину: {exc}")
        return pd.DataFrame()


def fmt_number(value: Any, decimals: int = 0) -> str:
    if value is None or pd.isna(value):
        return "—"
    if decimals:
        return f"{float(value):,.{decimals}f}".replace(",", " ")
    return f"{int(round(float(value))):,}".replace(",", " ")


def fmt_percent(value: Any) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value) * 100:.1f}%"


def metric_card(label: str, value: str, note: str = "", accent: str = "") -> None:
    st.markdown(
        f'<div class="metric {accent}"><div class="metric-label">{label}</div>'
        f'<div class="metric-value">{value}</div><div class="metric-note">{note}</div></div>',
        unsafe_allow_html=True,
    )


def chart(fig: go.Figure, height: int = 360) -> None:
    fig.update_layout(template=PLOTLY_TEMPLATE, height=height)
    st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False, "responsive": True})


def empty_state(message: str) -> None:
    st.info(message)


def fetch_music_dates() -> list[date]:
    frame = safe_query("SELECT snapshot_date FROM gold.mart_music_daily ORDER BY snapshot_date")
    if frame.empty:
        return []
    return [pd.Timestamp(v).date() for v in frame["snapshot_date"]]


def render_header() -> None:
    st.markdown('<div class="eyebrow">music analytics / signal room</div>', unsafe_allow_html=True)
    st.title("Что сейчас звучит в данных?")
    st.markdown(
        '<p class="lede">Ежедневный срез популярности, чартов, альбомов и ML-прогнозов — '
        'от сырого API-ответа до решения музыкального аналитика.</p>',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="rule"></div>', unsafe_allow_html=True)


def render_sidebar(dates: list[date]) -> tuple[date | None, date | None]:
    st.sidebar.markdown('<div class="eyebrow">контекст среза</div>', unsafe_allow_html=True)
    st.sidebar.markdown("### Период наблюдения")
    if not dates:
        st.sidebar.warning("В mart_music_daily пока нет данных.")
        return None, None

    min_date, max_date = min(dates), max(dates)
    selected_range = st.sidebar.date_input(
        "Даты среза",
        value=(min_date, max_date),
        min_value=min_date,
        max_value=max_date,
        key="date_range",
    )
    if isinstance(selected_range, (tuple, list)):
        from_date, to_date = selected_range[0], selected_range[-1]
    else:
        from_date = to_date = selected_range

    if st.sidebar.button("Обновить данные", use_container_width=True):
        query.clear()
        st.rerun()

    st.sidebar.markdown("---")
    st.sidebar.caption("Конвейер: `collect.py` → `run_silver.py` → `run_gold.py` → `run_forecast.py`.")
    return from_date, to_date


def format_pos_delta(delta: Any) -> str:
    if delta is None or pd.isna(delta) or int(delta) == 0:
        return "•"
    d = int(delta)
    return f"▲{d}" if d > 0 else f"▼{abs(d)}"


def render_music_dashboard(from_date: date, to_date: date) -> None:
    params = (from_date, to_date)
    kpi = safe_query(
        """
        SELECT * FROM gold.mart_music_daily
        WHERE snapshot_date BETWEEN %s AND %s
        ORDER BY snapshot_date DESC
        """,
        params,
    )
    tracks = safe_query(
        """
        SELECT * FROM gold.fact_track_daily
        WHERE snapshot_date BETWEEN %s AND %s
        ORDER BY snapshot_date, spotify_popularity DESC NULLS LAST
        """,
        params,
    )
    charts_all = safe_query(
        """
        SELECT * FROM gold.fact_chart_position_daily
        WHERE snapshot_date BETWEEN %s AND %s
        """,
        params,
    )
    albums_daily = safe_query(
        """
        SELECT al.snapshot_date,
               al.spotify_album_id,
               al.name AS album_name,
               al.album_type,
               al.popularity AS album_popularity,
               al.release_date,
               ar.name AS artist_name
        FROM silver.sp_album_daily al
        LEFT JOIN silver.sp_artist_daily ar
          ON ar.snapshot_date = al.snapshot_date
         AND ar.spotify_artist_id = al.artist_id
        WHERE al.snapshot_date BETWEEN %s AND %s
        """,
        params,
    )

    if kpi.empty or tracks.empty:
        empty_state("За выбранный период нет Gold-данных. Сначала выполните `python src/run_gold.py`.")
        return

    # --- Переключатель жанра прямо над чартами (влияет и на Deezer, и на Spotify) ---
    genre_options = (
        sorted(int(v) for v in charts_all["genre_id"].dropna().unique())
        if not charts_all.empty
        else []
    )
    genre_map = {"Все жанры (весь каталог Spotify)": None}
    for gid in genre_options:
        genre_map[genre_label(gid)] = gid

    filter_col1, filter_col2 = st.columns([1.2, 1.8])
    with filter_col1:
        selected_genre_label = st.selectbox(
            "Фильтр по жанровому чарту (фильтрует Deezer и Spotify)",
            list(genre_map.keys()),
        )
    genre_id = genre_map[selected_genre_label]

    if genre_id is not None:
        charts = charts_all[charts_all["genre_id"] == genre_id].copy()
        matched_sp_ids = set(charts["spotify_track_id"].dropna().unique())
        tracks_filtered = tracks[tracks["spotify_track_id"].isin(matched_sp_ids)].copy()
        if tracks_filtered.empty:
            tracks_filtered = tracks.copy()
    else:
        charts = charts_all.copy()
        tracks_filtered = tracks.copy()

    latest_date = pd.to_datetime(tracks["snapshot_date"]).max()
    latest_tracks = tracks_filtered[pd.to_datetime(tracks_filtered["snapshot_date"]) == latest_date].copy()
    latest = kpi.iloc[0]
    previous = kpi.iloc[1] if len(kpi) > 1 else None
    change = "нет предыдущего среза"
    if previous is not None and not pd.isna(previous["avg_track_popularity"]):
        delta = float(latest["avg_track_popularity"]) - float(previous["avg_track_popularity"])
        change = f"{delta:+.1f} к предыдущей дате"

    avg_pop_val = (
        latest_tracks["spotify_popularity"].mean()
        if genre_id is not None and not latest_tracks.empty
        else latest["avg_track_popularity"]
    )
    leader_row = (
        latest_tracks.sort_values("spotify_popularity", ascending=False).iloc[0]
        if not latest_tracks.empty
        else None
    )

    cols = st.columns(4)
    with cols[0]:
        metric_card("Средняя популярность", fmt_number(avg_pop_val, 1), change, "")
    with cols[1]:
        metric_card(
            "Треков в выборке",
            fmt_number(len(latest_tracks)),
            "активный срез Spotify",
            "blue",
        )
    with cols[2]:
        leader_title = str(leader_row["track_name"])[:22] if leader_row is not None else "—"
        leader_pop = f"popularity: {fmt_number(leader_row['spotify_popularity'])}" if leader_row is not None else ""
        metric_card("Лидер среза", leader_title, leader_pop, "amber")
    with cols[3]:
        metric_card(
            "Новые релизы",
            fmt_number(latest["new_release_tracks_observed"]),
            "треков в контрольной группе",
            "coral",
        )

    st.markdown("## Ритм популярности")
    st.markdown(
        '<p class="section-note">Динамика средней и медианной популярности по дням для выбранного сегмента.</p>',
        unsafe_allow_html=True,
    )
    trend = (
        tracks_filtered.groupby("snapshot_date", as_index=False)
        .agg(
            avg_pop=("spotify_popularity", "mean"),
            med_pop=("spotify_popularity", "median"),
        )
        .sort_values("snapshot_date")
    )
    trend["snapshot_date"] = pd.to_datetime(trend["snapshot_date"])
    trend_long = trend.melt("snapshot_date", var_name="metric", value_name="value")
    trend_long["metric"] = trend_long["metric"].map({"avg_pop": "Средняя", "med_pop": "Медиана"})
    fig_trend = px.line(
        trend_long,
        x="snapshot_date",
        y="value",
        color="metric",
        markers=True,
        color_discrete_map={"Средняя": CYAN, "Медиана": BLUE},
        labels={"value": "popularity", "snapshot_date": "", "metric": "Метрика"},
    )
    fig_trend.update_yaxes(range=[0, 100], title=None)
    chart(fig_trend, 310)

    # --- РЯД 1: Топ треков Spotify и Обновленный Чарт Deezer ---
    left, right = st.columns([1, 1])
    with left:
        st.markdown("## Топ треков Spotify")
        st.markdown(
            '<p class="section-note">12 самых популярных треков на последнюю дату (№1 сверху).</p>',
            unsafe_allow_html=True,
        )
        top = latest_tracks.nlargest(12, "spotify_popularity").copy()
        if top.empty:
            empty_state("Нет треков с popularity на последнюю дату.")
        else:
            top["rank_num"] = range(1, len(top) + 1)
            top["label"] = (
                "#"
                + top["rank_num"].astype(str)
                + "  "
                + top["track_name"].fillna(top["spotify_track_id"]).str.slice(0, 28)
                + " — "
                + top["artist_name"].fillna("").str.slice(0, 16)
            )
            # Сортируем по возрастанию для px.bar, чтобы #1 отрисовался на самом верху
            top = top.sort_values("rank_num", ascending=False)
            fig_top = px.bar(
                top,
                x="spotify_popularity",
                y="label",
                orientation="h",
                text="spotify_popularity",
                color_discrete_sequence=[CYAN],
                labels={"spotify_popularity": "Spotify Popularity (0–100)", "label": ""},
            )
            fig_top.update_traces(textposition="outside", cliponaxis=False)
            fig_top.update_xaxes(range=[0, 110], title="Spotify Popularity")
            fig_top.update_yaxes(title=None)
            chart(fig_top, 440)

    with right:
        active_chart_id = genre_id if genre_id is not None else 0
        st.markdown(f"## Чарт Deezer: {genre_label(active_chart_id)}")
        st.markdown(
            '<p class="section-note">Топ-12 позиций чарта (#1 сверху, в скобках динамика за сутки). '
            'Длина полосы — популярность трека в Spotify.</p>',
            unsafe_allow_html=True,
        )
        chart_latest = charts_all[
            (pd.to_datetime(charts_all["snapshot_date"]) == latest_date)
            & (charts_all["genre_id"] == active_chart_id)
        ].copy()
        if chart_latest.empty and not charts.empty:
            chart_latest = charts[pd.to_datetime(charts["snapshot_date"]) == latest_date].copy()

        if chart_latest.empty:
            empty_state("Для выбранного жанра нет чартовых наблюдений.")
        else:
            chart_latest = (
                chart_latest.sort_values("chart_position", ascending=True)
                .drop_duplicates("deezer_track_id")
                .head(12)
                .copy()
            )
            chart_latest["delta_str"] = chart_latest["chart_position_delta_1d"].map(format_pos_delta)
            chart_latest["label"] = (
                "#"
                + chart_latest["chart_position"].astype(str)
                + " ("
                + chart_latest["delta_str"]
                + ")  "
                + chart_latest["track_name"].fillna(chart_latest["deezer_track_id"]).str.slice(0, 32)
            )
            # Если трек сопоставлен со Spotify, показываем его популярность; иначе ставим оценку по месту
            chart_latest["bar_score"] = chart_latest["spotify_popularity"].fillna(
                100 - chart_latest["chart_position"].clip(upper=95)
            )
            chart_latest["bar_text"] = np.where(
                chart_latest["spotify_popularity"].notna(),
                "pop: " + chart_latest["spotify_popularity"].fillna(0).astype(int).astype(str),
                "нет в Spotify",
            )
            chart_latest["spotify_match"] = chart_latest["is_spotify_matched"].map(
                {True: "Сопоставлен со Spotify", False: "Только в Deezer"}
            )
            # Инвертируем порядок строк (от 12 к 1), чтобы в горизонтальном баре #1 стоял на самом верху!
            chart_latest = chart_latest.sort_values("chart_position", ascending=False)

            fig_chart = px.bar(
                chart_latest,
                x="bar_score",
                y="label",
                orientation="h",
                text="bar_text",
                color="spotify_match",
                color_discrete_map={"Сопоставлен со Spotify": CYAN, "Только в Deezer": AMBER},
                labels={
                    "bar_score": "Spotify Popularity",
                    "label": "",
                    "spotify_match": "Статус",
                },
            )
            fig_chart.update_traces(textposition="outside", cliponaxis=False)
            fig_chart.update_xaxes(range=[0, 115], title="Популярность в Spotify (0–100)")
            fig_chart.update_yaxes(title=None)
            chart(fig_chart, 440)

    # --- РЯД 2: Топ популярных альбомов и Матрица хитов (Аудитория vs Популярность) ---
    row2_left, row2_right = st.columns([1, 1])
    with row2_left:
        st.markdown("## Топ популярных альбомов и синглов")
        st.markdown(
            '<p class="section-note">Рейтинг альбомов из каталога Spotify на последнюю дату по индексу популярности релиза.</p>',
            unsafe_allow_html=True,
        )
        alb_latest = (
            albums_daily[pd.to_datetime(albums_daily["snapshot_date"]) == latest_date].copy()
            if not albums_daily.empty
            else pd.DataFrame()
        )
        if genre_id is not None and not latest_tracks.empty and not alb_latest.empty:
            genre_album_ids = set(latest_tracks["spotify_album_id"].dropna().unique())
            alb_filtered = alb_latest[alb_latest["spotify_album_id"].isin(genre_album_ids)].copy()
            if not alb_filtered.empty:
                alb_latest = alb_filtered

        if alb_latest.empty or alb_latest["album_popularity"].dropna().empty:
            empty_state("Нет данных о популярности альбомов за выбранную дату.")
        else:
            top_alb = (
                alb_latest.dropna(subset=["album_popularity"])
                .sort_values("album_popularity", ascending=False)
                .drop_duplicates("spotify_album_id")
                .head(12)
                .copy()
            )
            top_alb["rank_num"] = range(1, len(top_alb) + 1)
            top_alb["artist_str"] = top_alb["artist_name"].fillna("").str.slice(0, 16)
            top_alb["label"] = (
                "#"
                + top_alb["rank_num"].astype(str)
                + "  "
                + top_alb["album_name"].fillna(top_alb["spotify_album_id"]).str.slice(0, 28)
                + np.where(top_alb["artist_str"] != "", " — " + top_alb["artist_str"], "")
            )
            top_alb["type_ru"] = top_alb["album_type"].map(
                {"album": "Альбом", "single": "Сингл / EP", "compilation": "Сборник"}
            ).fillna("Релиз")
            top_alb = top_alb.sort_values("rank_num", ascending=False)

            fig_alb = px.bar(
                top_alb,
                x="album_popularity",
                y="label",
                orientation="h",
                text="album_popularity",
                color="type_ru",
                color_discrete_map={"Альбом": BLUE, "Сингл / EP": AMBER, "Сборник": CYAN, "Релиз": CYAN},
                labels={"album_popularity": "Популярность альбома (0–100)", "label": "", "type_ru": "Тип релиза"},
            )
            fig_alb.update_traces(textposition="outside", cliponaxis=False)
            fig_alb.update_xaxes(range=[0, 110], title="Spotify Album Popularity")
            fig_alb.update_yaxes(title=None)
            chart(fig_alb, 430)

    with row2_right:
        st.markdown("## Карта хитов: Аудитория артиста vs Популярность")
        st.markdown(
            '<p class="section-note">Вирусные прорывы находятся сверху слева (высокая популярность трека при скромной базе подписчиков).</p>',
            unsafe_allow_html=True,
        )
        scatter_df = latest_tracks.dropna(subset=["spotify_popularity", "artist_followers"]).copy()
        scatter_df = scatter_df[scatter_df["artist_followers"] > 0].copy()
        if scatter_df.empty:
            empty_state("Недостаточно данных о подписчиках артистов для построения карты.")
        else:
            scatter_df["group_label"] = np.where(
                scatter_df["is_new_release_track"], "Новый релиз", "Чарт / Каталог"
            )
            scatter_df["size_bubble"] = (
                scatter_df["deezer_rank_max"].fillna(200_000).clip(lower=50_000, upper=1_000_000) / 50_000
            )
            fig_scat = px.scatter(
                scatter_df,
                x="artist_followers",
                y="spotify_popularity",
                color="group_label",
                size="size_bubble",
                hover_name="track_name",
                hover_data={
                    "artist_name": True,
                    "spotify_popularity": True,
                    "artist_followers": ":,.0f",
                    "release_age_days": True,
                    "size_bubble": False,
                    "group_label": False,
                },
                log_x=True,
                color_discrete_map={"Новый релиз": CORAL, "Чарт / Каталог": CYAN},
                labels={
                    "artist_followers": "Подписчики артиста в Spotify (лог. шкала)",
                    "spotify_popularity": "Популярность трека",
                    "group_label": "Категория",
                },
            )
            fig_scat.update_yaxes(range=[0, 104])
            chart(fig_scat, 430)

    with st.expander("Детальная таблица треков"):
        visible = tracks_filtered[
            [
                "snapshot_date",
                "track_name",
                "artist_name",
                "album_name",
                "spotify_popularity",
                "popularity_delta_1d",
                "is_new_release_track",
                "deezer_rank_max",
            ]
        ].copy()
        visible["snapshot_date"] = pd.to_datetime(visible["snapshot_date"]).dt.strftime("%Y-%m-%d")
        st.dataframe(
            visible.sort_values(["snapshot_date", "spotify_popularity"], ascending=[False, False]),
            use_container_width=True,
            hide_index=True,
        )


def render_operations_dashboard(from_date: date, to_date: date) -> None:
    ops = safe_query(
        """
        SELECT * FROM gold.mart_pipeline_daily
        WHERE snapshot_date BETWEEN %s AND %s
        ORDER BY snapshot_date
        """,
        (from_date, to_date),
    )
    dq = safe_query(
        """
        SELECT snapshot_date, rule_name, dimension, layer, severity, total_rows,
               failed_rows, fail_share, status, details
        FROM meta.dq_results
        WHERE snapshot_date BETWEEN %s AND %s
        ORDER BY snapshot_date DESC, (status = 'fail') DESC, (status = 'warn') DESC, rule_name
        """,
        (from_date, to_date),
    )
    if ops.empty:
        empty_state("Операционная витрина ещё не построена. Выполните `python src/run_gold.py`.")
        return

    latest = ops.iloc[-1]
    fail_count = int(dq.loc[dq["status"] == "fail"].shape[0]) if not dq.empty else 0
    warn_count = int(dq.loc[dq["status"] == "warn"].shape[0]) if not dq.empty else 0
    cols = st.columns(4)
    with cols[0]:
        metric_card(
            "Состояние конвейера",
            "OK" if fail_count == 0 else "Внимание",
            f"fail: {fail_count} · warn: {warn_count}",
            "" if fail_count == 0 else "coral",
        )
    with cols[1]:
        metric_card(
            "Запросов к API",
            fmt_number(latest["requests_total"]),
            f"ошибок: {fmt_number(latest['requests_failed'])}",
            "blue",
        )
    with cols[2]:
        metric_card(
            "Строк Gold",
            fmt_number(latest["gold_fact_rows"]),
            f"Silver: {fmt_number(latest['silver_rows'])}",
            "amber",
        )
    with cols[3]:
        metric_card(
            "Загрузки",
            fmt_number(latest["ingestion_runs"]),
            f"успешных: {fmt_number(latest['successful_runs'])}",
            "",
        )

    st.markdown("## Здоровье потока")
    flow = ops.copy()
    flow["snapshot_date"] = pd.to_datetime(flow["snapshot_date"])
    flow_long = flow.melt(
        id_vars="snapshot_date",
        value_vars=["bronze_rows", "silver_rows", "gold_fact_rows"],
        var_name="layer",
        value_name="rows",
    )
    flow_long["layer"] = flow_long["layer"].map(
        {"bronze_rows": "Bronze", "silver_rows": "Silver", "gold_fact_rows": "Gold"}
    )
    fig = px.bar(
        flow_long,
        x="snapshot_date",
        y="rows",
        color="layer",
        barmode="group",
        color_discrete_map={"Bronze": BLUE, "Silver": CYAN, "Gold": AMBER},
        labels={"rows": "строк", "snapshot_date": "", "layer": "слой"},
    )
    chart(fig, 350)

    left, right = st.columns([1, 1])
    with left:
        st.markdown("## Нагрузка и сбои")
        load = flow[["snapshot_date", "requests_total", "requests_failed"]].melt(
            "snapshot_date", var_name="metric", value_name="count"
        )
        load["metric"] = load["metric"].map({"requests_total": "Все запросы", "requests_failed": "Ошибки"})
        fig = px.line(
            load,
            x="snapshot_date",
            y="count",
            color="metric",
            markers=True,
            color_discrete_map={"Все запросы": BLUE, "Ошибки": CORAL},
            labels={"count": "запросы", "snapshot_date": ""},
        )
        fig.update_yaxes(title=None)
        chart(fig, 330)
    with right:
        st.markdown("## Проверки качества")
        if dq.empty:
            empty_state("DQ-результаты отсутствуют.")
        else:
            counts = (
                dq.groupby(["snapshot_date", "status"], as_index=False)
                .size()
                .rename(columns={"size": "count"})
            )
            counts["snapshot_date"] = pd.to_datetime(counts["snapshot_date"])
            fig = px.bar(
                counts,
                x="snapshot_date",
                y="count",
                color="status",
                barmode="stack",
                color_discrete_map={"pass": GREEN, "warn": AMBER, "fail": CORAL},
                labels={"count": "проверки", "snapshot_date": "", "status": "статус"},
            )
            chart(fig, 330)

    st.markdown("## Журнал качества")
    if not dq.empty:
        dq_view = dq.copy()
        dq_view["snapshot_date"] = pd.to_datetime(dq_view["snapshot_date"]).dt.strftime("%Y-%m-%d")
        dq_view["fail_share"] = dq_view["fail_share"].map(fmt_percent)
        dq_view = dq_view.rename(
            columns={
                "snapshot_date": "Дата",
                "rule_name": "Правило",
                "dimension": "Тип",
                "layer": "Слой",
                "severity": "Важность",
                "total_rows": "Проверено",
                "failed_rows": "Не прошло",
                "fail_share": "Доля",
                "status": "Статус",
                "details": "Комментарий",
            }
        )
        st.dataframe(dq_view, use_container_width=True, hide_index=True)
        st.download_button(
            "Скачать журнал DQ (CSV)",
            dq_view.to_csv(index=False).encode("utf-8-sig"),
            "dq_results.csv",
            "text/csv",
        )


def render_forecast_dashboard(from_date: date, to_date: date) -> None:
    origin_frame = safe_query("SELECT max(origin_date) AS origin_date FROM gold.fact_track_forecast")
    if origin_frame.empty or pd.isna(origin_frame.iloc[0]["origin_date"]):
        empty_state("Прогнозная витрина ещё не рассчитана. Выполните `python src/run_forecast.py`.")
        return

    origin_date = pd.Timestamp(origin_frame.iloc[0]["origin_date"]).date()
    metrics = safe_query(
        """
        SELECT horizon_days, n_samples, mae, rmse, smape
        FROM gold.forecast_model_metrics
        WHERE eval_date = %s AND model_name = 'gbm_mae'
        ORDER BY horizon_days
        """,
        (origin_date,),
    )

    horizon_focus = st.slider("Горизонт прогноза (дней вперёд)", min_value=1, max_value=7, value=4)

    forecasts = safe_query(
        """
        SELECT *
        FROM gold.fact_track_forecast
        WHERE origin_date = %s AND model_name = 'gbm_mae'
        ORDER BY horizon_days, predicted_popularity DESC
        """,
        (origin_date,),
    )
    if forecasts.empty:
        empty_state("Нет рассчитанных прогнозов GBM. Перезапустите `python src/run_forecast.py`.")
        return

    focus_slice = forecasts[forecasts["horizon_days"] == horizon_focus].copy()
    gbm_mae = float(np.average(metrics["mae"], weights=metrics["n_samples"])) if not metrics.empty else None
    gbm_rmse = float(np.average(metrics["rmse"], weights=metrics["n_samples"])) if not metrics.empty else None

    growing_count = int((focus_slice["predicted_delta"] > 0.1).sum())
    top_grower = (
        focus_slice.sort_values("predicted_delta", ascending=False).iloc[0]
        if not focus_slice.empty
        else None
    )

    cols = st.columns(4)
    with cols[0]:
        note = f"RMSE: {gbm_rmse:.2f}" if gbm_rmse is not None else "градиентный бустинг (L1)"
        metric_card("Средняя ошибка (MAE)", fmt_number(gbm_mae, 2) if gbm_mae is not None else "—", note, "")
    with cols[1]:
        metric_card("Дата среза прогноза", origin_date.strftime("%d.%m.%Y"), "горизонт до +7 дней", "blue")
    with cols[2]:
        metric_card(
            f"Треков с ростом (+{horizon_focus} дн.)",
            fmt_number(growing_count),
            f"из {fmt_number(len(focus_slice))} активных треков",
            "amber",
        )
    with cols[3]:
        has_growth = top_grower is not None and float(top_grower["predicted_delta"]) > 0
        grower_name = str(top_grower["track_name"])[:22] if has_growth else "Без роста"
        grower_delta = (
            f"{float(top_grower['predicted_delta']):+.1f} п.п. (pop: {int(top_grower['current_popularity'])})"
            if has_growth
            else "0.0 п.п."
        )
        metric_card("Лидер ожидаемого роста", grower_name, grower_delta, "coral")

    st.markdown("## Траектория трека: факт и прогноз")
    st.markdown(
        '<p class="section-note">Историческая кривая популярности из Gold и прогноз GBM на 7 дней вперёд (треки с популярностью от 10 пунктов).</p>',
        unsafe_allow_html=True,
    )
    track_options = (
        focus_slice.sort_values(["predicted_delta", "current_popularity"], ascending=[False, False])[
            ["spotify_track_id", "track_name", "artist_name", "current_popularity", "predicted_delta"]
        ]
        .drop_duplicates("spotify_track_id")
    )
    track_labels = {
        f"{row['track_name']} — {row['artist_name']} (pop: {row['current_popularity']}, Δ: {float(row['predicted_delta']):+.1f})": row["spotify_track_id"]
        for _, row in track_options.iterrows()
    }
    chosen_label = st.selectbox("Выберите трек для анализа траектории", list(track_labels.keys()))
    chosen_track_id = track_labels[chosen_label]

    hist_track = safe_query(
        """
        SELECT snapshot_date, spotify_popularity
        FROM gold.fact_track_daily
        WHERE spotify_track_id = %s
        ORDER BY snapshot_date
        """,
        (chosen_track_id,),
    )
    pred_track = forecasts[forecasts["spotify_track_id"] == chosen_track_id].sort_values("target_date").copy()

    fig_traj = go.Figure()
    if not hist_track.empty:
        fig_traj.add_trace(
            go.Scatter(
                x=pd.to_datetime(hist_track["snapshot_date"]),
                y=hist_track["spotify_popularity"],
                mode="lines+markers",
                name="Факт (Gold)",
                line={"color": CYAN, "width": 3},
            )
        )
    if not pred_track.empty:
        x_pred = [pd.Timestamp(origin_date)] + list(pd.to_datetime(pred_track["target_date"]))
        y_pred = [float(pred_track.iloc[0]["current_popularity"])] + list(
            pred_track["predicted_popularity"].astype(float)
        )
        fig_traj.add_trace(
            go.Scatter(
                x=x_pred,
                y=y_pred,
                mode="lines+markers",
                name="Прогноз (GBM)",
                line={"color": AMBER, "width": 3, "dash": "dash"},
            )
        )
    fig_traj.update_yaxes(range=[0, 102], title="popularity")
    chart(fig_traj, 360)

    movers = focus_slice[focus_slice["predicted_delta"] > 0.05].nlargest(10, "predicted_delta").copy()
    if not movers.empty:
        st.markdown(f"## Лидеры ожидаемого роста (+{horizon_focus} дн.)")
        movers["label"] = (
            movers["track_name"].fillna(movers["spotify_track_id"]).str.slice(0, 34)
            + " (pop: "
            + movers["current_popularity"].astype(str)
            + ")"
        )
        movers = movers.sort_values("predicted_delta")
        movers["release_flag"] = movers["is_new_release_track"].map(
            {True: "Новый релиз", False: "Каталог / чарт"}
        )
        fig_mov = px.bar(
            movers,
            x="predicted_delta",
            y="label",
            orientation="h",
            text="predicted_delta",
            color="release_flag",
            color_discrete_map={"Новый релиз": CORAL, "Каталог / чарт": CYAN},
            labels={
                "predicted_delta": "ожидаемый прирост popularity",
                "label": "",
                "release_flag": "Группа",
            },
        )
        fig_mov.update_traces(texttemplate="%{text:+.1f}", textposition="outside", cliponaxis=False)
        fig_mov.update_yaxes(title=None)
        chart(fig_mov, 400)

    with st.expander("Метрики по горизонтам и таблица прогнозов"):
        if not metrics.empty:
            st.markdown("#### Точность модели GBM по горизонтам (Backtest)")
            st.dataframe(metrics, use_container_width=True, hide_index=True)
        st.markdown(f"#### Детальный прогноз на +{horizon_focus} дн.")
        table_cols = [
            "target_date",
            "track_name",
            "artist_name",
            "current_popularity",
            "predicted_popularity",
            "predicted_delta",
            "is_new_release_track",
        ]
        st.dataframe(
            focus_slice[table_cols].sort_values("predicted_delta", ascending=False),
            use_container_width=True,
            hide_index=True,
        )


def main() -> None:
    render_header()
    dates = fetch_music_dates()
    from_date, to_date = render_sidebar(dates)
    if from_date is None or to_date is None:
        return

    tabs = st.tabs(["Предметная панель", "Операционная панель", "Прогнозная панель"])
    with tabs[0]:
        render_music_dashboard(from_date, to_date)
    with tabs[1]:
        render_operations_dashboard(from_date, to_date)
    with tabs[2]:
        render_forecast_dashboard(from_date, to_date)


if __name__ == "__main__":
    main()