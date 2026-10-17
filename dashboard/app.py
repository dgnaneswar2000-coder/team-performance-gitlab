"""Interactive team-performance dashboard.

    streamlit run dashboard/app.py

Reads the reporting tables built by sql/. The filters (dates, teams, work
type, weekends) apply to every chart, so the charts are computed here from
fct_tasks, using the same definitions as the SQL summary tables.
"""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import config  # noqa: E402

# One fixed colour per metric (categorical slots 1-4), used on every chart.
METRICS = {
    "Tasks completed": {"color": "#2a78d6", "unit": "tasks"},
    "Errors": {"color": "#eb6834", "unit": "errors"},
    "Quality %": {"color": "#1baf7a", "unit": "%"},
    "Hours to deliver": {"color": "#eda100", "unit": "hours (median)"},
}
MUTED, INK, INK_2, GRID = "#c3c2b7", "#0b0b0b", "#52514e", "#e6e4df"
# Readable headers for the task lists and summary tables.
TASK_COLUMNS = {
    "merged_day": "Merged on", "associate": "Associate", "team": "Team", "work_type": "Work type",
    "cycle_hours": st.column_config.NumberColumn("Hours to deliver", format="%.1f"),
    "review_comments": "Review comments",
    "was_reverted": st.column_config.CheckboxColumn("Reverted?"),
}
PEOPLE_COLUMNS = {
    "associate": "Associate", "main_team": "Main team", "tasks": "Tasks", "active_days": "Active days",
    "tasks_per_active_day": st.column_config.NumberColumn("Tasks / active day", format="%.1f"),
    "errors": "Errors",
    "quality_pct": st.column_config.NumberColumn("Quality %", format="%.1f"),
    "median_hours": st.column_config.NumberColumn("Median hours to deliver", format="%.0f"),
    "review_comments": st.column_config.NumberColumn("Review comments / task", format="%.0f"),
}

st.set_page_config(page_title="Team Performance", page_icon="📊", layout="wide")


# ------------------------------------------------------------------ data
@st.cache_data(ttl=300)
def load():
    if not config.DB_PATH.exists():
        return None
    with sqlite3.connect(config.DB_PATH) as conn:
        tasks = pd.read_sql("SELECT * FROM fct_tasks", conn)
        dq = pd.read_sql("SELECT * FROM dq_checks", conn)
        runs = pd.read_sql("SELECT * FROM ingest_runs ORDER BY run_id DESC LIMIT 5", conn)
    tasks["date"] = pd.to_datetime(tasks.merged_day)
    tasks["revert_date"] = pd.to_datetime(tasks.revert_day)
    # Today (UTC) is still in progress: its low numbers would look like a crash.
    today = pd.Timestamp(datetime.now(timezone.utc).date())
    return tasks[tasks.date < today], dq, runs


def daily_series(t: pd.DataFrame, metric: str, days: pd.DatetimeIndex) -> pd.Series:
    """One value per day for the chosen metric (same definitions as the SQL)."""
    if metric == "Tasks completed":
        s = t.groupby("date").size()
    elif metric == "Errors":  # counted on the day the error was undone
        s = t[t.was_reverted == 1].groupby("revert_date").size()
    elif metric == "Quality %":
        s = 100 - 100 * t.groupby("date").was_reverted.mean()
    else:
        s = t.groupby("date").cycle_hours.median()
    s = s.reindex(days)
    return s.fillna(0) if metric in ("Tasks completed", "Errors") else s


def summarise(t: pd.DataFrame, by: str) -> pd.DataFrame:
    """Per-team or per-associate summary (mirrors sql/03 and sql/04)."""
    g = t.groupby(by)
    out = pd.DataFrame({
        "tasks": g.size(),
        "active_days": g.merged_day.nunique(),
        "errors": g.was_reverted.sum(),
        "quality_pct": (100 - 100 * g.was_reverted.mean()).round(1),
        "pct_bug_fixes": (100 * g.work_type.apply(lambda s: (s == "bug").mean())).round(0),
        "median_hours": g.cycle_hours.median().round(1),
        "review_comments": g.review_comments.mean().round(1),
    })
    out["tasks_per_active_day"] = (out.tasks / out.active_days).round(2)
    if by == "associate":
        out["main_team"] = g.team.agg(lambda s: s.value_counts().index[0])
    return out.reset_index()


def style(fig, height=340):
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=8, b=8), bargap=0.25,
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color=INK_2, size=13), hoverlabel=dict(bgcolor="white", font_color=INK),
        legend=dict(orientation="h", y=1.1, x=0, title=None),
    )
    fig.update_xaxes(showgrid=False, linecolor=GRID)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, rangemode="tozero")
    return fig


def fmt_hours(h):
    if pd.isna(h):
        return "–"
    return f"{int(h + 0.5)} h" if h < 48 else f"{h / 24:.1f} days"  # round half up, like the chart hovers


def kpis(t, prev):
    """Headline numbers, with change vs the previous period of the same length."""
    def delta(cur, before, fmt):
        if before is None or pd.isna(before) or pd.isna(cur):
            return None
        return fmt(cur - before)

    wd = t[t.date.dt.dayofweek < 5]
    per_day = wd.groupby("date").size().mean() if len(wd) else 0
    errors = int(t.was_reverted.sum())
    quality = 100 - 100 * t.was_reverted.mean() if len(t) else float("nan")
    hours = t.cycle_hours.median()

    p_per_day = p_err = p_q = p_h = None
    if prev is not None and len(prev):
        pwd = prev[prev.date.dt.dayofweek < 5]
        p_per_day = pwd.groupby("date").size().mean() if len(pwd) else None
        p_err, p_q, p_h = int(prev.was_reverted.sum()), 100 - 100 * prev.was_reverted.mean(), prev.cycle_hours.median()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tasks per weekday", f"{per_day:.0f}", delta(per_day, p_per_day, lambda d: f"{d:+.0f}"),
              help="Average tasks merged per Monday-Friday.")
    c2.metric("Errors (reverted tasks)", errors, delta(errors, p_err, lambda d: f"{d:+.0f}"),
              delta_color="inverse", help="Tasks that had to be undone after merging.")
    c3.metric("Quality", f"{quality:.1f}%", delta(quality, p_q, lambda d: f"{d:+.1f} pts"),
              help="Share of tasks that were not reverted.")
    c4.metric("Median time to deliver", fmt_hours(hours), delta(hours, p_h, lambda d: f"{d:+.0f} h"),
              delta_color="inverse", help="Opening a merge request to merging it, including review wait.")
    if prev is not None and len(prev):
        st.caption("Small numbers under each figure compare with the previous period of the same length.")


loaded = load()
st.title("Team performance dashboard")
if loaded is None or loaded[0].empty:
    st.warning("No data yet. Run `python run_pipeline.py` first.")
    st.stop()
tasks, dq, runs = loaded

# --------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Filters")
    lo, hi = tasks.date.min().date(), tasks.date.max().date()
    start, end = st.slider("Dates", min_value=lo, max_value=hi, value=(lo, hi), format="D MMM")
    team_options = tasks.team.value_counts().index.tolist()
    teams = st.multiselect("Teams", team_options, placeholder="All teams",
                           help="From GitLab's group:: labels, largest first.")
    work_types = st.multiselect("Work type", sorted(tasks.work_type.unique()), placeholder="All work types")
    weekends = st.toggle("Include weekends", value=True)
    st.divider()
    last = runs.iloc[0]
    st.caption(
        f"**Source:** public GitLab API, `{config.PROJECT_NAME}`.  \n"
        f"**Refreshed:** {str(last.finished_at or last.started_at)[:16].replace('T', ' ')} UTC "
        f"({last.status}).  \nPeople are anonymous IDs; bots are excluded."
    )


def apply_filters(t, s, e):
    t = t[(t.date >= pd.Timestamp(s)) & (t.date <= pd.Timestamp(e))]
    if teams:
        t = t[t.team.isin(teams)]
    if work_types:
        t = t[t.work_type.isin(work_types)]
    if not weekends:
        t = t[t.date.dt.dayofweek < 5]
    return t


f = apply_filters(tasks, start, end)
span = (end - start).days + 1
prev_start = start - timedelta(days=span)
prev = apply_filters(tasks, prev_start, start - timedelta(days=1)) if prev_start >= lo else None
days = pd.date_range(start, end)
if not weekends:
    days = days[days.dayofweek < 5]

if f.empty:
    st.info("No tasks match these filters. Widen the date range or clear a filter.")
    st.stop()

scope = ", ".join(teams) if teams else "all teams"
st.caption(f"Showing **{len(f):,} tasks** by **{f.associate.nunique()} associates** in {scope}, "
           f"{start:%d %b} – {end:%d %b %Y}.")

tab_overview, tab_teams, tab_people, tab_about = st.tabs(
    ["📈 Overview", "👥 Teams", "🧑 Associates", "ℹ️ Data & definitions"])

# ============================================================== overview
with tab_overview:
    kpis(f, prev)
    metric = st.segmented_control("Show per day", list(METRICS), default="Tasks completed", key="ov_metric") \
        or "Tasks completed"
    m = METRICS[metric]
    series = daily_series(f, metric, days)
    weekend = series.index.dayofweek >= 5

    avg_text = {"Tasks completed": f"about {series[~weekend].mean():.0f} on a typical weekday",
                "Errors": f"{int(series.sum())} in total over {len(series)} days",
                "Quality %": f"{100 - 100 * f.was_reverted.mean():.1f}% of tasks never needed undoing",
                "Hours to deliver": f"half of all tasks merged within {fmt_hours(f.cycle_hours.median())}"}[metric]
    st.subheader(f"{metric} per day: {avg_text}")
    st.markdown("**Click a bar** to see that day's tasks below. Grey bars are weekends.")

    fig = go.Figure()
    fig.add_bar(x=series.index, y=series.values, name=metric,
                marker_color=[MUTED if w else m["color"] for w in weekend],
                hovertemplate="%{x|%a %d %b}: <b>%{y:" + (".0f" if metric in ("Tasks completed", "Errors")
                                                          else ".1f") + "} " + m["unit"] + "</b><extra></extra>")
    if len(series) >= 7 and metric in ("Tasks completed", "Errors"):
        fig.add_scatter(x=series.index, y=series.rolling(7, min_periods=7).mean(), mode="lines",
                        name="7-day average", line=dict(color=INK_2, width=2),
                        hovertemplate="7-day average: %{y:.1f}<extra></extra>")
    if metric == "Quality %":
        fig.update_yaxes(range=[max(0, series.min() - 2), 100.5])
    fig.update_yaxes(title=m["unit"])
    event = st.plotly_chart(style(fig), width="stretch", on_select="rerun", selection_mode="points",
                            key="ov_chart")

    picked = [pd.Timestamp(p["x"]).normalize() for p in event.selection.points] if event else []
    if picked:
        day = picked[0]
        if metric == "Errors":
            d = f[f.revert_date == day]
            st.markdown(f"**Tasks undone on {day:%A %d %B}** ({len(d)})")
        else:
            d = f[f.date == day]
            st.markdown(f"**Tasks merged on {day:%A %d %B}** ({len(d)}, by {d.associate.nunique()} associates)")
        st.dataframe(
            d[["associate", "team", "work_type", "cycle_hours", "review_comments", "was_reverted"]]
            .sort_values("cycle_hours"),
            hide_index=True, width="stretch", height=300, column_config=TASK_COLUMNS)
    else:
        st.caption("No day selected.")

    left, right = st.columns(2)
    with left:
        mix = f.work_type.value_counts()
        st.subheader(f"What the work was: {100 * mix.get('bug', 0) / len(f):.0f}% bug fixes")
        fig = go.Figure(go.Bar(
            y=mix.index, x=mix.values, orientation="h", marker_color=METRICS["Tasks completed"]["color"],
            text=[f"{v:,} ({100 * v / len(f):.0f}%)" for v in mix.values], textposition="outside",
            cliponaxis=False, hovertemplate="%{y}: %{x:,} tasks<extra></extra>"))
        fig.update_yaxes(autorange="reversed")
        fig.update_xaxes(range=[0, mix.max() * 1.3], title="Tasks")
        st.plotly_chart(style(fig, 260), width="stretch")
    with right:
        bins = [0, 4, 24, 72, 168, float("inf")]
        labels = ["Under 4 h", "4-24 h", "1-3 days", "3-7 days", "Over a week"]
        b = pd.cut(f.cycle_hours, bins=bins, labels=labels, right=False).value_counts().reindex(labels)
        share = 100 * b / b.sum()
        st.subheader(f"Time to deliver: {share.iloc[:2].sum():.0f}% merged within a day")
        fig = go.Figure(go.Bar(
            x=labels, y=share.values, marker_color=METRICS["Hours to deliver"]["color"],
            text=[f"{s:.0f}%" for s in share.values], textposition="outside", cliponaxis=False,
            customdata=b.values, hovertemplate="%{x}: %{customdata:,} tasks (%{y:.0f}%)<extra></extra>"))
        fig.update_yaxes(title="Share of tasks (%)", range=[0, share.max() * 1.3])
        st.plotly_chart(style(fig, 260), width="stretch")
        st.caption("Elapsed time, including waiting for review. GitLab's time-tracking field is empty "
                   "for almost every task, so hands-on effort can't be measured.")

# ================================================================= teams
with tab_teams:
    team_sum = summarise(f, "team").sort_values("tasks", ascending=False)
    c1, c2 = st.columns([2, 1])
    t_metric = c1.segmented_control(
        "Compare teams by", ["Tasks completed", "Errors", "Quality %", "Hours to deliver"],
        default="Tasks completed", key="team_metric") or "Tasks completed"
    top_n = c2.slider("Teams shown", 5, max(5, len(team_sum)), min(15, len(team_sum)))
    hide_unlabelled = c2.checkbox("Hide tasks with no team label", value=True)
    col = {"Tasks completed": "tasks", "Errors": "errors", "Quality %": "quality_pct",
           "Hours to deliver": "median_hours"}[t_metric]
    # Quality and speed are only meaningful with enough tasks behind them.
    pool = team_sum if col in ("tasks", "errors") else team_sum[team_sum.tasks >= 20]
    if hide_unlabelled:
        pool = pool[pool.team != "(no team label)"]
    best_first = col not in ("errors", "median_hours")
    shown = pool.nlargest(top_n, "tasks").sort_values(col, ascending=not best_first)

    st.subheader(f"{t_metric} by team")
    st.markdown("**Click a team** to see its daily trend and associates."
                + ("" if col in ("tasks", "errors") else " Teams with fewer than 20 tasks are hidden."))
    fig = go.Figure(go.Bar(
        y=shown.team, x=shown[col], orientation="h", marker_color=METRICS[t_metric]["color"],
        text=shown[col].map(lambda v: f"{v:,.1f}" if col != "tasks" and col != "errors" else f"{v:,.0f}"),
        textposition="outside", cliponaxis=False,
        customdata=shown[["tasks"]],
        hovertemplate="%{y}: <b>%{x}</b><br>%{customdata[0]:,} tasks<extra></extra>"))
    fig.update_yaxes(autorange="reversed", title=None)
    fig.update_xaxes(title=METRICS[t_metric]["unit"],
                     range=[0, (shown[col].max() or 1) * 1.2] if col != "quality_pct" else [
                         max(0, shown[col].min() - 2), 100.5])
    ev = st.plotly_chart(style(fig, max(260, 28 * len(shown))), width="stretch",
                         on_select="rerun", selection_mode="points", key="team_chart")

    focus = ev.selection.points[0]["y"] if ev and ev.selection.points else None
    if focus:
        ft = f[f.team == focus]
        st.markdown(f"### {focus}")
        a, b_, c, d = st.columns(4)
        a.metric("Tasks", f"{len(ft):,}")
        b_.metric("Associates", ft.associate.nunique())
        c.metric("Errors", int(ft.was_reverted.sum()))
        d.metric("Median time to deliver", fmt_hours(ft.cycle_hours.median()))
        s = daily_series(ft, "Tasks completed", days)
        fig = go.Figure(go.Bar(x=s.index, y=s.values, marker_color=[
            MUTED if w else METRICS["Tasks completed"]["color"] for w in s.index.dayofweek >= 5],
            hovertemplate="%{x|%a %d %b}: %{y:.0f} tasks<extra></extra>"))
        fig.update_yaxes(title="Tasks per day")
        st.plotly_chart(style(fig, 240), width="stretch")
        st.dataframe(summarise(ft, "associate").sort_values("tasks", ascending=False)
                     [["associate", "tasks", "tasks_per_active_day", "errors", "quality_pct", "median_hours"]],
                     hide_index=True, width="stretch", height=260, column_config=PEOPLE_COLUMNS)
    else:
        st.caption("No team selected.")

    with st.expander("All teams as a table"):
        st.dataframe(team_sum[["team", "tasks", "active_days", "errors", "quality_pct", "pct_bug_fixes",
                               "median_hours", "review_comments"]],
                     hide_index=True, width="stretch",
                     column_config={**PEOPLE_COLUMNS, "team": "Team",
                                    "pct_bug_fixes": st.column_config.NumberColumn("Bug-fix share %",
                                                                                   format="%.0f")})
        st.download_button("Download teams (CSV)", team_sum.to_csv(index=False), "teams.csv", "text/csv")

# ============================================================= associates
with tab_people:
    people = summarise(f, "associate")
    c1, c2 = st.columns([1, 2])
    min_tasks = c1.slider("Minimum tasks", 1, int(max(2, people.tasks.max())), 5,
                          help="People with only a few tasks have very noisy percentages.")
    search = c2.text_input("Find an associate", placeholder="e.g. Associate 3FA2C1")
    p = people[people.tasks >= min_tasks]
    if search:
        p = p[p.associate.str.contains(search.strip(), case=False)]

    st.subheader("Output vs speed: each dot is one associate")
    st.markdown("Right = more tasks per active day. Lower = faster delivery. Bigger dot = more tasks in total. "
                "**Orange** dots had at least one task reverted. **Click a dot** to open that associate.")
    clean, err = p[p.errors == 0], p[p.errors > 0]
    fig = go.Figure()
    for d, name, color in [(clean, "No errors", METRICS["Tasks completed"]["color"]),
                           (err, "Had an error", METRICS["Errors"]["color"])]:
        fig.add_scatter(
            x=d.tasks_per_active_day, y=d.median_hours, mode="markers", name=name,
            marker=dict(color=color, size=(d.tasks ** 0.5) * 3 + 6, opacity=0.75,
                        line=dict(color="white", width=1)),
            customdata=d[["associate", "main_team", "tasks", "errors", "quality_pct"]],
            hovertemplate="<b>%{customdata[0]}</b> (%{customdata[1]})<br>%{customdata[2]} tasks, "
                          "%{x:.1f} per active day<br>median %{y:.0f} h to deliver<br>"
                          "errors: %{customdata[3]} · quality %{customdata[4]:.1f}%<extra></extra>")
    fig.update_xaxes(title="Tasks per active day")
    ticks = {1: "1 h", 4: "4 h", 24: "1 day", 72: "3 days", 168: "1 week", 720: "1 month"}
    fig.update_yaxes(title="Median time to deliver (log scale)", type="log",
                     tickvals=list(ticks), ticktext=list(ticks.values()))
    ev = st.plotly_chart(style(fig, 440), width="stretch", on_select="rerun",
                         selection_mode="points", key="people_chart")

    chosen = ev.selection.points[0]["customdata"][0] if ev and ev.selection.points else None
    if not chosen and search and len(p) == 1:
        chosen = p.associate.iloc[0]
    if chosen:
        pt = f[f.associate == chosen]
        row = people.set_index("associate").loc[chosen]
        st.markdown(f"### {chosen} · {row.main_team}")
        a, b_, c, d, e = st.columns(5)
        a.metric("Tasks", int(row.tasks))
        b_.metric("Tasks per active day", f"{row.tasks_per_active_day:.1f}")
        c.metric("Errors", int(row.errors))
        d.metric("Quality", f"{row.quality_pct:.1f}%")
        e.metric("Median time to deliver", fmt_hours(row.median_hours))
        s = daily_series(pt, "Tasks completed", days)
        fig = go.Figure(go.Bar(x=s.index, y=s.values, marker_color=METRICS["Tasks completed"]["color"],
                               hovertemplate="%{x|%a %d %b}: %{y:.0f} tasks<extra></extra>"))
        fig.update_yaxes(title="Tasks per day", dtick=1)
        st.plotly_chart(style(fig, 220), width="stretch")
        st.markdown("Their tasks, reverted ones first:")
        st.dataframe(pt[["merged_day", "team", "work_type", "cycle_hours", "review_comments", "was_reverted"]]
                     .sort_values(["was_reverted", "merged_day"], ascending=False),
                     hide_index=True, width="stretch", height=240, column_config=TASK_COLUMNS)

    st.subheader("All associates")
    st.dataframe(
        p.sort_values("tasks", ascending=False)[
            ["associate", "main_team", "tasks", "active_days", "tasks_per_active_day", "errors",
             "quality_pct", "median_hours", "review_comments"]],
        hide_index=True, width="stretch", height=380,
        column_config={
            "associate": "Associate", "main_team": "Main team",
            "tasks": st.column_config.ProgressColumn("Tasks", format="%d", min_value=0,
                                                     max_value=int(max(1, people.tasks.max()))),
            "active_days": "Active days",
            "tasks_per_active_day": st.column_config.NumberColumn("Tasks / active day", format="%.1f"),
            "errors": "Errors",
            "quality_pct": st.column_config.NumberColumn("Quality %", format="%.1f"),
            "median_hours": st.column_config.NumberColumn("Median hours to deliver", format="%.0f"),
            "review_comments": st.column_config.NumberColumn("Review comments / task", format="%.0f"),
        })
    st.caption(f"{len(p)} of {len(people)} associates shown.")
    st.download_button("Download associates (CSV)", p.to_csv(index=False), "associates.csv", "text/csv")

# ================================================================== about
with tab_about:
    st.markdown(f"""
#### How each metric is defined

| Metric | Definition | What it can't tell you |
|---|---|---|
| **Task** | A merge request merged into `{config.PROJECT_NAME}` by a person (bots excluded). | Size varies: a one-line fix and a week's work both count as one. |
| **Errors** | A task that was later *reverted* (undone), counted on the day it was undone. | Only catches mistakes serious enough to undo; smaller ones are fixed forward. |
| **Quality %** | Share of tasks that were *not* reverted. | Reverts are rare, so most people score near 100%. Read it alongside errors and review comments. |
| **Hours to deliver** | Time from opening the merge request to merging it (median). | Elapsed time including waiting for review, not hands-on effort. GitLab's time-tracking field is almost never filled in. |
| **Tasks per active day** | Tasks ÷ days the person merged anything. | Days off aren't counted against anyone. |
| **Bug fixes** | Tasks labelled `type::bug`. | Shows defects being fixed, not who caused them. |

These numbers describe output and outcomes, not effort or value. Use them to start a conversation,
not to rank people.
""")
    st.markdown("#### Data quality")
    st.dataframe(dq, hide_index=True, width="stretch",
                 column_config={"check_name": "Check", "n": "Records", "pct": "% of tasks"})
    st.markdown("#### Recent pipeline runs")
    st.dataframe(runs, hide_index=True, width="stretch")
    st.download_button("Download filtered tasks (CSV)",
                       f.drop(columns=["date", "revert_date", "web_url"]).to_csv(index=False),
                       "tasks.csv", "text/csv")
