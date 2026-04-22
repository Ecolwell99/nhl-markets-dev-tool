import math
from datetime import datetime

import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

SCOREBOARD_URL = "https://api-web.nhle.com/v1/scoreboard/now"
PBP_URL = "https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play"
REFRESH_MS = 3000

SHOT_TYPES = {"shot-on-goal", "goal"}
FACEOFF_TYPE = "faceoff"


st.set_page_config(page_title="NHL Markets Dev Tool", layout="wide")


def init_state():
    defaults = {
        "games": [],
        "selected_game_label": None,
        "selected_game_id": None,
        "tracking": False,
        "previous_faceoff_count": None,
        "warning_message": "STATUS: OK",
        "warning_type": "ok",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def fetch_json(url: str) -> dict:
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()


def load_live_games():
    data = fetch_json(SCOREBOARD_URL)
    games = []

    for day in data.get("gamesByDate", []):
        for game in day.get("games", []):
            state = game.get("gameState")
            if state in {"LIVE", "CRIT"}:
                away = game.get("awayTeam", {}).get("abbrev", "AWAY")
                home = game.get("homeTeam", {}).get("abbrev", "HOME")
                game_id = game.get("id")
                label = f"{away} @ {home} ({game_id})"
                games.append(
                    {
                        "label": label,
                        "id": game_id,
                        "away": away,
                        "home": home,
                    }
                )
    return games


def safe_team(play: dict) -> str:
    return (
        play.get("teamAbbrev", {}).get("default")
        or play.get("team", {}).get("abbrev")
        or "UNK"
    )


def parse_events(plays: list[dict]) -> list[dict]:
    events = []

    faceoff_count = 0
    sog_count = 0

    for play in plays or []:
        play_type = str(play.get("typeDescKey", "")).lower()
        period = play.get("periodDescriptor", {}).get("number")
        time_in_period = play.get("timeInPeriod", "")
        event_id = play.get("eventId")
        team = safe_team(play)

        include = False
        display_type = None

        if play_type == FACEOFF_TYPE:
            faceoff_count += 1
            include = True
            display_type = "FACEOFF"

        elif play_type in SHOT_TYPES:
            sog_count += 1
            include = True
            display_type = "SOG" if play_type == "shot-on-goal" else "GOAL"

        if include:
            events.append(
                {
                    "event_id": event_id,
                    "period": period,
                    "time_in_period": time_in_period,
                    "team": team,
                    "raw_type": play_type,
                    "display_type": display_type,
                    "faceoff_number": faceoff_count if play_type == FACEOFF_TYPE else None,
                    "sog_number": sog_count if play_type in SHOT_TYPES else None,
                }
            )

    return events


def dedupe_events(events: list[dict]) -> list[dict]:
    deduped = {}
    for event in events:
        deduped[event["event_id"]] = event
    return list(deduped.values())


def period_sort_key(event: dict):
    # NHL feed is usually already ordered, so preserve event order via event_id fallback
    return (
        event.get("period") if event.get("period") is not None else 99,
        event.get("event_id") if event.get("event_id") is not None else 999999999,
    )


def get_game_state(game_id: int) -> dict:
    data = fetch_json(PBP_URL.format(game_id=game_id))
    plays = data.get("plays", [])

    raw_events = parse_events(plays)
    events = dedupe_events(raw_events)

    faceoffs = [e for e in events if e["display_type"] == "FACEOFF"]
    sog_events = [e for e in events if e["display_type"] in {"SOG", "GOAL"}]

    by_period_faceoffs = {}
    by_period_sog = {}

    for event in faceoffs:
        p = event["period"]
        by_period_faceoffs[p] = by_period_faceoffs.get(p, 0) + 1

    for event in sog_events:
        p = event["period"]
        by_period_sog[p] = by_period_sog.get(p, 0) + 1

    return {
        "events": events,
        "faceoffs": faceoffs,
        "sog_events": sog_events,
        "faceoff_total": len(faceoffs),
        "sog_total": len(sog_events),
        "by_period_faceoffs": by_period_faceoffs,
        "by_period_sog": by_period_sog,
        "last_faceoff": faceoffs[-1] if faceoffs else None,
        "last_event": events[-1] if events else None,
        "fetched_at": datetime.now().strftime("%I:%M:%S %p"),
    }


def seconds_remaining_from_clock(clock_str: str) -> int | None:
    try:
        parts = clock_str.split(":")
        minutes = int(parts[0])
        seconds = int(parts[1])
        return minutes * 60 + seconds
    except Exception:
        return None


def bucket_label(start_sec: int, end_sec: int) -> str:
    def fmt(sec: int) -> str:
        m = sec // 60
        s = sec % 60
        return f"{m}:{s:02d}"

    return f"{fmt(start_sec)}-{fmt(end_sec)}"


def build_two_minute_buckets(period_events: list[dict]) -> list[dict]:
    buckets = [
        {"start": 1200, "end": 1081},  # 20:00-18:01
        {"start": 1080, "end": 961},   # 18:00-16:01
        {"start": 960, "end": 841},    # 16:00-14:01
        {"start": 840, "end": 721},    # 14:00-12:01
        {"start": 720, "end": 601},    # 12:00-10:01
        {"start": 600, "end": 481},    # 10:00-8:01
        {"start": 480, "end": 361},    # 8:00-6:01
        {"start": 360, "end": 241},    # 6:00-4:01
        {"start": 240, "end": 121},    # 4:00-2:01
        {"start": 120, "end": 1},      # 2:00-0:01
    ]

    sogs = [e for e in period_events if e["display_type"] in {"SOG", "GOAL"}]

    results = []
    for bucket in buckets:
        hits = []
        for event in sogs:
            secs = seconds_remaining_from_clock(event["time_in_period"])
            if secs is None:
                continue
            if bucket["end"] <= secs <= bucket["start"]:
                hits.append(event)

        results.append(
            {
                "window": bucket_label(bucket["start"], bucket["end"]),
                "result": "YES" if hits else "NO",
                "count": len(hits),
                "events": hits,
            }
        )
    return results


def warning_box(message: str, warning_type: str):
    if warning_type == "alert":
        st.markdown(
            f"""
            <div style="
                margin-top: 10px;
                margin-bottom: 18px;
                padding: 16px;
                border-radius: 10px;
                font-size: 26px;
                font-weight: 700;
                background-color: #3a1600;
                color: #ffd966;
                border: 2px solid #ff9900;
            ">
                {message}
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"""
            <div style="
                margin-top: 10px;
                margin-bottom: 18px;
                padding: 16px;
                border-radius: 10px;
                font-size: 26px;
                font-weight: 700;
                background-color: #132117;
                color: #66ff99;
                border: 2px solid #2e6b45;
            ">
                {message}
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_event_table(events: list[dict], title: str):
    st.subheader(title)

    if not events:
        st.info("No events to show.")
        return

    rows = []
    for e in events:
        rows.append(
            {
                "Period": e["period"],
                "Time": e["time_in_period"],
                "Type": e["display_type"],
                "Team": e["team"],
                "Faceoff #": e["faceoff_number"] if e["faceoff_number"] is not None else "",
                "SOG #": e["sog_number"] if e["sog_number"] is not None else "",
                "Event ID": e["event_id"],
            }
        )

    st.dataframe(rows, use_container_width=True, hide_index=True)


def preview_next_faceoff(events: list[dict], after_event_id: int | None = None):
    if after_event_id is None:
        for event in events:
            if event["display_type"] == "FACEOFF":
                return event
        return None

    found_anchor = False
    for event in events:
        if event["event_id"] == after_event_id:
            found_anchor = True
            continue
        if found_anchor and event["display_type"] == "FACEOFF":
            return event
    return None


def preview_next_sog_after_faceoff(faceoffs: list[dict], events: list[dict], faceoff_number: int):
    anchor = None
    for faceoff in faceoffs:
        if faceoff["faceoff_number"] == faceoff_number:
            anchor = faceoff
            break

    if not anchor:
        return None, f"Faceoff #{faceoff_number} not found."

    found_anchor = False
    for event in events:
        if event["event_id"] == anchor["event_id"]:
            found_anchor = True
            continue
        if found_anchor and event["display_type"] in {"SOG", "GOAL"}:
            return event, None

    return None, f"No subsequent SOG found yet after Faceoff #{faceoff_number}."


init_state()

st.title("NHL Markets Dev Tool")
st.caption("Dev / review tool only. Do not result off this tool.")

top_left, top_mid, top_right = st.columns([1.2, 2, 1.2])

with top_left:
    if st.button("Load Live Games", use_container_width=True):
        try:
            games = load_live_games()
            st.session_state.games = games

            if not games:
                st.session_state.selected_game_label = None
                st.session_state.selected_game_id = None
                st.session_state.tracking = False
                st.info("No live games found.")
            else:
                labels = [g["label"] for g in games]
                if st.session_state.selected_game_label not in labels:
                    st.session_state.selected_game_label = labels[0]
                    st.session_state.selected_game_id = games[0]["id"]
                st.success(f"Loaded {len(games)} live game(s).")
        except Exception as e:
            st.error(f"Error loading games: {e}")

with top_mid:
    game_labels = [g["label"] for g in st.session_state.games]
    selected_label = st.selectbox(
        "Live games",
        options=game_labels,
        index=game_labels.index(st.session_state.selected_game_label)
        if st.session_state.selected_game_label in game_labels
        else None,
        placeholder="Load live games first",
    )

    if selected_label:
        st.session_state.selected_game_label = selected_label
        for game in st.session_state.games:
            if game["label"] == selected_label:
                st.session_state.selected_game_id = game["id"]
                break

with top_right:
    if st.button("Track Selected Game", use_container_width=True):
        if st.session_state.selected_game_id is None:
            st.warning("Load live games and select one first.")
        else:
            st.session_state.tracking = True
            st.session_state.previous_faceoff_count = None
            st.session_state.warning_message = "STATUS: OK"
            st.session_state.warning_type = "ok"

if st.session_state.tracking:
    st_autorefresh(interval=REFRESH_MS, key="market_dev_refresh")

    try:
        state = get_game_state(st.session_state.selected_game_id)
        new_faceoff_count = state["faceoff_total"]
        previous_faceoff_count = st.session_state.previous_faceoff_count

        if previous_faceoff_count is not None:
            if new_faceoff_count < previous_faceoff_count:
                st.session_state.warning_message = (
                    f"⚠ COUNT DECREASE: {previous_faceoff_count} → {new_faceoff_count}"
                )
                st.session_state.warning_type = "alert"
            elif (new_faceoff_count - previous_faceoff_count) > 1:
                st.session_state.warning_message = (
                    f"⚠ MULTIPLE FACEOFFS ADDED: +{new_faceoff_count - previous_faceoff_count}"
                )
                st.session_state.warning_type = "alert"
            else:
                st.session_state.warning_message = "STATUS: OK"
                st.session_state.warning_type = "ok"
        else:
            st.session_state.warning_message = "STATUS: OK"
            st.session_state.warning_type = "ok"

        st.session_state.previous_faceoff_count = new_faceoff_count

        warning_box(
            st.session_state.warning_message,
            st.session_state.warning_type,
        )

        a, b, c, d = st.columns(4)
        with a:
            st.metric("Faceoffs", state["faceoff_total"])
        with b:
            st.metric("SOG Events", state["sog_total"])
        with c:
            st.metric("Game ID", st.session_state.selected_game_id)
        with d:
            st.metric("Refresh", state["fetched_at"])

        if state["last_faceoff"]:
            lf = state["last_faceoff"]
            st.markdown(
                f"**Last Faceoff:** P{lf['period']} {lf['time_in_period']} | Winner: {lf['team']} | Faceoff #{lf['faceoff_number']} | Event {lf['event_id']}"
            )
        else:
            st.markdown("**Last Faceoff:** none")

        st.divider()

        periods_present = sorted(
            [
                p for p in set(
                    [e["period"] for e in state["events"] if e["period"] is not None]
                )
            ]
        )
        if not periods_present:
            periods_present = [1]

        control_left, control_right = st.columns([1, 2])

        with control_left:
            selected_period = st.selectbox(
                "Review period",
                options=periods_present,
                index=len(periods_present) - 1,
            )

        period_events = [e for e in state["events"] if e["period"] == selected_period]
        period_faceoffs = [e for e in period_events if e["display_type"] == "FACEOFF"]
        period_sogs = [e for e in period_events if e["display_type"] in {"SOG", "GOAL"}]

        tab1, tab2, tab3, tab4 = st.tabs(
            [
                "Timeline",
                "Period Review",
                "2-Min SOG Buckets",
                "Market Preview",
            ]
        )

        with tab1:
            render_event_table(state["events"], "Full Event Timeline")

        with tab2:
            left, right = st.columns(2)
            with left:
                render_event_table(period_faceoffs, f"Period {selected_period} Faceoffs")
            with right:
                render_event_table(period_sogs, f"Period {selected_period} SOG Events")

        with tab3:
            st.subheader(f"Period {selected_period} - 2 Minute SOG Buckets")
            bucket_results = build_two_minute_buckets(period_events)

            rows = []
            for bucket in bucket_results:
                event_summary = " | ".join(
                    [
                        f"{e['time_in_period']} {e['team']} {e['display_type']}"
                        for e in bucket["events"]
                    ]
                )
                rows.append(
                    {
                        "Window": bucket["window"],
                        "Result": bucket["result"],
                        "SOG Count": bucket["count"],
                        "Events": event_summary,
                    }
                )

            st.dataframe(rows, use_container_width=True, hide_index=True)

        with tab4:
            left, right = st.columns(2)

            with left:
                st.subheader("Next Faceoff Preview")
                first_next_faceoff = preview_next_faceoff(state["events"])
                if first_next_faceoff:
                    st.markdown(
                        f"""
                        **Preview:** P{first_next_faceoff['period']} {first_next_faceoff['time_in_period']}  
                        **Winner:** {first_next_faceoff['team']}  
                        **Faceoff #:** {first_next_faceoff['faceoff_number']}  
                        **Event ID:** {first_next_faceoff['event_id']}
                        """
                    )
                else:
                    st.info("No faceoff found.")

            with right:
                st.subheader("Next SOG After Faceoff #")
                max_faceoff_num = max(
                    [f["faceoff_number"] for f in state["faceoffs"]],
                    default=1,
                )
                chosen_faceoff_num = st.number_input(
                    "Faceoff number",
                    min_value=1,
                    max_value=max_faceoff_num,
                    value=min(max_faceoff_num, 1),
                    step=1,
                )

                preview_sog, preview_error = preview_next_sog_after_faceoff(
                    state["faceoffs"],
                    state["events"],
                    chosen_faceoff_num,
                )

                if preview_error:
                    st.info(preview_error)
                else:
                    st.markdown(
                        f"""
                        **After Faceoff #{chosen_faceoff_num}:**  
                        **Next SOG Event:** P{preview_sog['period']} {preview_sog['time_in_period']}  
                        **Team:** {preview_sog['team']}  
                        **Type:** {preview_sog['display_type']}  
                        **Event ID:** {preview_sog['event_id']}
                        """
                    )

    except Exception as e:
        st.error(f"Refresh error: {e}")
else:
    warning_box("STATUS: OK", "ok")
    st.info("Load live games, select one, and click Track Selected Game.")
