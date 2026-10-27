"""Интерактивные предметный и операционный дашборды Music Analytics.

Запуск:
    streamlit run dashboard/app.py

Данные читаются только из Gold-витрин PostgreSQL; приложение не изменяет БД.
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any

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

# Музыкальная палитра: midnight — фон, cyan — поток популярности,
# amber — релизы и coral — сбои. Цвета не кодируют декоративные категории.
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

# Названия жанров Deezer для ID, которые используются в DEEZER_GENRE_IDS.
# ID 0 — общий чарт, а не отдельный музыкальный жанр.
DEEZER_GENRE_NAMES = {
    0: "Общий чарт",
    106: "Электро",
    116: "Рэп / хип-хоп",
    132: "Поп",
    152: "Хаус",
    165: "Альтернатива",
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
        margin={"l": 18, "r": 18, "t": 50, "b": 18},
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
      .status-good {{ color: {GREEN}; font-weight: 700; }}
      .status-warn {{ color: {AMBER}; font-weight: 700; }}
      .status-fail {{ color: {CORAL}; font-weight: 700; }}
      div[data-testid="stDataFrame"] {{ border: 1px solid {GRID}; }}
      .stTabs [data-baseweb="tab-list"] {{ gap: 1.3rem; border-bottom: 1px solid {GRID}; }}
      .stTabs [data-baseweb="tab"] {{ color: {MUTED}; padding: .7rem .15rem; }}
      .stTabs [aria-selected="true"] {{ color: {TEXT}; border-bottom-color: {CYAN}; }}
      .stButton button, .stDownloadButton button {{ border-color: {GRID}; background: {PANEL}; color: {TEXT}; }}
      .stButton button:hover, .stDownloadButton button:hover {{ border-color: {CYAN}; color: {CYAN}; }}
      [data-testid="stMetricValue"] {{ color: {TEXT}; }}
      [data-testid="stMetricLabel"] {{ color: {MUTED}; }}
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
    return psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", "15432")),
        user=_env("PG_USER"),
        password=_env("PG_PASSWORD"),
        dbname=_env("PG_DB"),
    )


@st.cache_data(ttl=60, show_spinner=False)
def query(sql: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    connection = get_connection()
    return pd.read_sql_query(sql, connection, params=params)


def safe_query(sql: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    try:
        return query(sql, params)
    except Exception as exc:  # dashboard должен объяснить ошибку, а не падать трассировкой
        st.error(f"Не удалось прочитать Gold: {exc}")
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
    frame = safe_query(
        """
        SELECT snapshot_date FROM gold.mart_music_daily
        ORDER BY snapshot_date
        """
    )
    if frame.empty:
        return []
    return [pd.Timestamp(v).date() for v in frame["snapshot_date"]]


def render_header() -> None:
    st.markdown('<div class="eyebrow">music analytics / signal room</div>', unsafe_allow_html=True)
    st.title("Что сейчас звучит в данных?")
    st.markdown(
        '<p class="lede">Ежедневный срез популярности, чартов и новых релизов — '
        'от сырого API-ответа до решения музыкального аналитика.</p>',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="rule"></div>', unsafe_allow_html=True)


def render_sidebar(dates: list[date]) -> tuple[date | None, date | None, int | None]:
    st.sidebar.markdown('<div class="eyebrow">контекст среза</div>', unsafe_allow_html=True)
    st.sidebar.markdown("### Фильтры")
    if not dates:
        st.sidebar.warning("В mart_music_daily пока нет данных.")
        return None, None, None

    min_date, max_date = min(dates), max(dates)
    selected_range = st.sidebar.date_input(
        "Период",
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
        # fetch_music_dates не кэшируется, поэтому очищать нужно только query.
        query.clear()
        st.rerun()

    genre_frame = safe_query(
        """
        SELECT DISTINCT genre_id
        FROM gold.fact_chart_position_daily
        WHERE snapshot_date BETWEEN %s AND %s
        ORDER BY genre_id
        """,
        (from_date, to_date),
    )
    genre_options = [int(v) for v in genre_frame["genre_id"].dropna()] if not genre_frame.empty else []
    genre_labels = {genre_label(value): value for value in genre_options}
    genre = st.sidebar.selectbox("Жанр чарта", ["Все жанры"] + list(genre_labels))
    genre_id = None if genre == "Все жанры" else genre_labels[genre]

    st.sidebar.markdown("---")
    st.sidebar.caption("Gold обновляется после `collect.py` → `run_silver.py` → `run_gold.py`.")
    return from_date, to_date, genre_id


def render_music_dashboard(from_date: date, to_date: date, genre_id: int | None) -> None:
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
    charts = safe_query(
        """
        SELECT * FROM gold.fact_chart_position_daily
        WHERE snapshot_date BETWEEN %s AND %s
        """ + (" AND genre_id = %s" if genre_id is not None else ""),
        params + ((genre_id,) if genre_id is not None else ()),
    )
    if kpi.empty or tracks.empty:
        empty_state("За выбранный период нет Gold-данных. Сначала выполните `python src/run_gold.py`.")
        return

    # Запрос отсортирован от последней даты к первой.
    latest = kpi.iloc[0]
    previous = kpi.iloc[1] if len(kpi) > 1 else None
    change = "нет предыдущего среза"
    if previous is not None and not pd.isna(previous["avg_track_popularity"]):
        delta = float(latest["avg_track_popularity"]) - float(previous["avg_track_popularity"])
        change = f"{delta:+.1f} к предыдущей дате"

    cols = st.columns(4)
    with cols[0]:
        metric_card("Средняя популярность", fmt_number(latest["avg_track_popularity"], 1), change, "")
    with cols[1]:
        metric_card("Треков в срезе", fmt_number(latest["spotify_tracks_observed"]), "уникальные Spotify ID", "blue")
    with cols[2]:
        metric_card("Чартовое покрытие", fmt_percent(latest["chart_spotify_match_share"]), "Deezer → Spotify", "amber")
    with cols[3]:
        metric_card("Новые релизы", fmt_number(latest["new_release_tracks_observed"]), "треков в контрольной группе", "coral")

    st.markdown("## Ритм популярности")
    st.markdown('<p class="section-note">Среднее и медиана popularity по дням. Это основной сигнал для будущего прогноза.</p>', unsafe_allow_html=True)
    trend = kpi[["snapshot_date", "avg_track_popularity", "median_track_popularity"]].copy()
    trend["snapshot_date"] = pd.to_datetime(trend["snapshot_date"])
    trend = trend.melt("snapshot_date", var_name="metric", value_name="value")
    trend["metric"] = trend["metric"].map({"avg_track_popularity": "Средняя", "median_track_popularity": "Медиана"})
    fig = px.line(trend, x="snapshot_date", y="value", color="metric", markers=True,
                  color_discrete_map={"Средняя": CYAN, "Медиана": BLUE}, labels={"value": "popularity", "snapshot_date": ""})
    fig.update_yaxes(range=[0, 100], title=None)
    chart(fig, 330)

    left, right = st.columns([1.12, .88])
    with left:
        st.markdown("## Кто наверху")
        latest_date = pd.to_datetime(tracks["snapshot_date"]).max()
        top = tracks[pd.to_datetime(tracks["snapshot_date"]) == latest_date].nlargest(10, "spotify_popularity").copy()
        if top.empty:
            empty_state("Нет треков с popularity на последнюю дату.")
        else:
            top["label"] = top["track_name"].fillna(top["spotify_track_id"]).str.slice(0, 42)
            top = top.sort_values("spotify_popularity")
            fig = px.bar(top, x="spotify_popularity", y="label", orientation="h", text="spotify_popularity",
                         color_discrete_sequence=[CYAN], labels={"spotify_popularity": "popularity", "label": ""})
            fig.update_traces(textposition="outside", cliponaxis=False)
            fig.update_xaxes(range=[0, 108], title=None)
            fig.update_yaxes(title=None)
            chart(fig, 430)
    with right:
        st.markdown("## Лидер дня")
        leader = latest.get("top_spotify_track_name") or "Нет данных"
        st.markdown(f"### {leader}")
        st.markdown(f"**{fmt_number(latest.get('top_spotify_track_popularity'))}** popularity", unsafe_allow_html=True)
        st.markdown(f"Срез: **{pd.Timestamp(latest['snapshot_date']).strftime('%d.%m.%Y')}**")
        st.markdown("---")
        st.markdown("### Чартовый сигнал")
        st.markdown(
            '<p class="section-note">12 лучших позиций Deezer-чарта на последнюю дату. '
            '№1 — первое место; цвет показывает, удалось ли сопоставить трек со Spotify.</p>',
            unsafe_allow_html=True,
        )
        chart_latest = charts[pd.to_datetime(charts["snapshot_date"]) == latest_date] if not charts.empty else charts
        if chart_latest.empty:
            empty_state("Для выбранного жанра нет чартовых наблюдений.")
        else:
            chart_latest = chart_latest.nsmallest(12, "chart_position").copy()
            chart_latest["label"] = chart_latest["track_name"].fillna(chart_latest["deezer_track_id"]).str.slice(0, 34)
            chart_latest["spotify_match"] = chart_latest["is_spotify_matched"].map(
                {True: "Сопоставлен", False: "Не сопоставлен"}
            ).fillna("Не сопоставлен")
            fig = px.bar(
                chart_latest.sort_values("chart_position", ascending=True),
                x="chart_position",
                y="label",
                orientation="h",
                color="spotify_match",
                color_discrete_map={"Сопоставлен": CYAN, "Не сопоставлен": AMBER},
                labels={
                    "chart_position": "позиция в чарте (1 — первое место)",
                    "label": "",
                    "spotify_match": "Spotify",
                },
            )
            fig.update_xaxes(autorange="reversed", title=None)
            fig.update_yaxes(title=None)
            chart(fig, 400)

    with st.expander("Детальная таблица треков"):
        visible = tracks[["snapshot_date", "track_name", "artist_name", "spotify_popularity", "popularity_delta_1d", "is_new_release_track", "deezer_rank_max"]].copy()
        visible["snapshot_date"] = pd.to_datetime(visible["snapshot_date"]).dt.strftime("%Y-%m-%d")
        st.dataframe(visible.sort_values(["snapshot_date", "spotify_popularity"], ascending=[False, False]), use_container_width=True, hide_index=True)


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
        metric_card("Состояние конвейера", "OK" if fail_count == 0 else "Внимание", "fail: %d · warn: %d" % (fail_count, warn_count), "" if fail_count == 0 else "coral")
    with cols[1]:
        metric_card("Запросов к API", fmt_number(latest["requests_total"]), f"ошибок: {fmt_number(latest['requests_failed'])}", "blue")
    with cols[2]:
        metric_card("Строк Gold", fmt_number(latest["gold_fact_rows"]), f"Silver: {fmt_number(latest['silver_rows'])}", "amber")
    with cols[3]:
        metric_card("Загрузки", fmt_number(latest["ingestion_runs"]), f"успешных: {fmt_number(latest['successful_runs'])}", "")

    st.markdown("## Здоровье потока")
    st.markdown('<p class="section-note">Одна строка — одна календарная дата наблюдения. Пропуски не маскируются.</p>', unsafe_allow_html=True)
    flow = ops.copy()
    flow["snapshot_date"] = pd.to_datetime(flow["snapshot_date"])
    flow_long = flow.melt(id_vars="snapshot_date", value_vars=["bronze_rows", "silver_rows", "gold_fact_rows"], var_name="layer", value_name="rows")
    flow_long["layer"] = flow_long["layer"].map({"bronze_rows": "Bronze", "silver_rows": "Silver", "gold_fact_rows": "Gold"})
    fig = px.bar(flow_long, x="snapshot_date", y="rows", color="layer", barmode="group",
                 color_discrete_map={"Bronze": BLUE, "Silver": CYAN, "Gold": AMBER}, labels={"rows": "строк", "snapshot_date": "", "layer": "слой"})
    chart(fig, 350)

    left, right = st.columns([1, 1])
    with left:
        st.markdown("## Нагрузка и сбои")
        load = flow[["snapshot_date", "requests_total", "requests_failed"]].melt("snapshot_date", var_name="metric", value_name="count")
        load["metric"] = load["metric"].map({"requests_total": "Все запросы", "requests_failed": "Ошибки"})
        fig = px.line(load, x="snapshot_date", y="count", color="metric", markers=True,
                      color_discrete_map={"Все запросы": BLUE, "Ошибки": CORAL}, labels={"count": "запросы", "snapshot_date": ""})
        fig.update_yaxes(title=None)
        chart(fig, 330)
    with right:
        st.markdown("## Проверки качества")
        if dq.empty:
            empty_state("DQ-результаты отсутствуют.")
        else:
            counts = dq.groupby(["snapshot_date", "status"], as_index=False).size().rename(columns={"size": "count"})
            counts["snapshot_date"] = pd.to_datetime(counts["snapshot_date"])
            fig = px.bar(counts, x="snapshot_date", y="count", color="status", barmode="stack",
                         color_discrete_map={"pass": GREEN, "warn": AMBER, "fail": CORAL},
                         labels={"count": "проверки", "snapshot_date": "", "status": "статус"})
            chart(fig, 330)

    st.markdown("## Журнал качества")
    if dq.empty:
        empty_state("За выбранный период нет результатов DQ.")
    else:
        dq_view = dq.copy()
        dq_view["snapshot_date"] = pd.to_datetime(dq_view["snapshot_date"]).dt.strftime("%Y-%m-%d")
        dq_view["fail_share"] = dq_view["fail_share"].map(lambda x: fmt_percent(x))
        dq_view = dq_view.rename(columns={
            "snapshot_date": "Дата", "rule_name": "Правило", "dimension": "Тип", "layer": "Слой",
            "severity": "Важность", "total_rows": "Проверено", "failed_rows": "Не прошло",
            "fail_share": "Доля", "status": "Статус", "details": "Комментарий",
        })
        st.dataframe(dq_view, use_container_width=True, hide_index=True)
        st.download_button("Скачать журнал DQ (CSV)", dq_view.to_csv(index=False).encode("utf-8-sig"), "dq_results.csv", "text/csv")


def main() -> None:
    render_header()
    dates = fetch_music_dates()
    from_date, to_date, genre_id = render_sidebar(dates)
    if from_date is None or to_date is None:
        return

    tabs = st.tabs(["Предметная панель", "Операционная панель"])
    with tabs[0]:
        render_music_dashboard(from_date, to_date, genre_id)
    with tabs[1]:
        render_operations_dashboard(from_date, to_date)


if __name__ == "__main__":
    main()
