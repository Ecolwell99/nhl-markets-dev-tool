import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

SCOREBOARD_URL = "https://api-web.nhle.com/v1/scoreboard/now"
PBP_URL = "https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play"
REFRESH_MS = 3000

FACEOFF_TYPE = "faceoff"
SHOT_TYPES = {"shot-on-goal", "goal"}

st.set_page_config(page_title="NHL Markets Dev Tool", layout="wide")


def init_state():
    defaults = {
        "games": [],
        "selected_game_label": None,
        "selected_game_id": None,
        "tracking": False,
        "previous_faceoff_count": None,
        "previous_live_period": None,
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


def parse_raw_events(plays: list[dict]) -> list[dict]:
    raw_events = []

    for play in plays or []:
        play_type = str(play.get("typeDescKey", "")).lower()

        if play_type not in SHOT_TYPES and play_type != FACEOFF_TYPE:
            continue

        display_type = "FACEOFF"
        if play_type == "shot-on-goal":
            display_type = "SOG"
        elif play_type == "goal":
            display_type = "GOAL"

        raw_events.append(
            {
                "event_id": play.get("eventId"),
                "period": play.get("periodDescriptor", {}).get("number"),
                "time_in_period": play.get("timeInPeriod", ""),
                "team": safe_team(play),
                "raw_type": play_type,
                "display_type": display_type,
            }
        )

    deduped = {}
    for event in raw_events:
        deduped[event["event_id"]] = event

    return list(deduped.values())


def add_period_local_numbers(events: list[dict]) -> list[dict]:
    faceoff_counts = {}
    sog_counts = {}
    numbered = []

    for event in events:
        period = event["period"]

        if period not in faceoff_counts:
            faceoff_counts[period] = 0
        if period not in sog_counts:
            sog_counts[period] = 0

        event_copy = dict(event)
        event_copy["faceoff_number"] = None
        event_copy["sog_number"] = None

        if event["display_type"] == "FACEOFF":
            faceoff_counts[period] += 1
            event_copy["faceoff_number"] = faceoff_counts[period]

        if event["display_type"] in {"SOG", "GOAL"}:
            sog_counts[period] += 1
            event_copy["sog_number"] = sog_counts[period]

        numbered.append(event_copy)

    return numbered


def get_game_state(game_id: int) -> dict:
    data = fetch_json(PBP_URL.format(game_id=game_id))
    plays = data.get("plays", [])

    events = parse_raw_events(plays)
    events = add_period_local_numbers(events)

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

    live_period = events[-1]["period"] if events else 1
    live_period_faceoffs = [e for e in faceoffs if e["period"] == live_period]

    return {
        "events": events,
        "faceoffs": faceoffs,
        "sog_events": sog_events,
        "by_period_faceoffs": by_period_faceoffs,
        "by_period_sog": by_period_sog,
        "faceoff_total": len(faceoffs),
        "sog_total": len(sog_events),
        "live_period": live_period,
        "live_period_faceoff_count": len(live_period_faceoffs),
        "last_faceoff": faceoffs[-1] if faceoffs else None,
    }


def seconds_remaining_from_clock(clock_str: str):
    try:
        minutes, seconds = clock_str.split(":")
        return int(minutes) * 60 + int(seconds)
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


def preview_first_faceoff_in_period(period_faceoffs: list[dict]):
    return period_faceoffs[0] if period_faceoffs else None


def preview_next_sog_after_faceoff(period_faceoffs: list[dict], period_events: list[dict], faceoff_number: int):
    anchor = None
    for faceoff in period_faceoffs:
        if faceoff["faceoff_number"] == faceoff_number:
            anchor = faceoff
            break

    if not anchor:
        return None, f"Faceoff #{faceoff_number} not found in this period."

    found_anchor = False
    for event in period_events:
        if event["event_id"] == anchor["event_id"]:
            found_anchor = True
            continue
        if found_anchor and event["display_type"] in {"SOG", "GOAL"}:
            return event, None

    return None, f"No subsequent SOG found yet after Faceoff #{faceoff_number} in this period."


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
            st.session_state.previous_live_period = None
            st.session_state.warning_message = "STATUS: OK"
            st.session_state.warning_type = "ok"

if st.session_state.tracking:
    st_autorefresh(interval=REFRESH_MS, key="market_dev_refresh")

    try:
        state = get_game_state(st.session_state.selected_game_id)

        live_period = state["live_period"]
        live_period_faceoff_count = state["live_period_faceoff_count"]
        previous_faceoff_count = st.session_state.previous_faceoff_count
        previous_live_period = st.session_state.previous_live_period

        if previous_live_period == live_period:
            if previous_faceoff_count is not None:
                if live_period_faceoff_count < previous_faceoff_count:
                    st.session_state.warning_message = (
                        f"⚠ COUNT DECREASE: {previous_faceoff_count} → {live_period_faceoff_count}"
                    )
                    st.session_state.warning_type = "alert"
                elif (live_period_faceoff_count - previous_faceoff_count) > 1:
                    st.session_state.warning_message = (
                        f"⚠ MULTIPLE FACEOFFS ADDED: +{live_period_faceoff_count - previous_faceoff_count}"
                    )
                    st.session_state.warning_type = "alert"
                else:
                    st.session_state.warning_message = "STATUS: OK"
                    st.session_state.warning_type = "ok"
            else:
                st.session_state.warning_message = "STATUS: OK"
                st.session_state.warning_type = "ok"
        else:
            st.session_state.warning_message = f"Period {live_period} started"
            st.session_state.warning_type = "ok"

        st.session_state.previous_faceoff_count = live_period_faceoff_count
        st.session_state.previous_live_period = live_period

        warning_box(
            st.session_state.warning_message,
            st.session_state.warning_type,
        )

        a, b, c, d = st.columns(4)
        with a:
            st.metric("Live Period", live_period)
        with b:
            st.metric("Faceoffs (Live Period)", live_period_faceoff_count)
        with c:
            st.metric("Faceoffs (Game)", state["faceoff_total"])
        with d:
            st.metric("SOG Events (Game)", state["sog_total"])

        if state["last_faceoff"]:
            lf = state["last_faceoff"]
            st.markdown(
                f"**Last Faceoff:** P{lf['period']} {lf['time_in_period']} | Winner: {lf['team']} | Faceoff #{lf['faceoff_number']} | Event {lf['event_id']}"
            )
        else:
            st.markdown("**Last Faceoff:** none")

        st.divider()

        periods_present = sorted(
            list(
                {
                    e["period"]
                    for e in state["events"]
                    if e["period"] is not None
                }
            )
        )
        if not periods_present:
            periods_present = [1]

        control_left, control_right = st.columns([1, 2])

        with control_left:
            selected_period = st.selectbox(
                "Review period",
                options=periods_present,
                index=periods_present.index(live_period) if live_period in periods_present else len(periods_present) - 1,
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
            st.subheader(f"Period {selected_period} Summary")
            col_a, col_b = st.columns(2)
            with col_a:
                st.metric("Faceoffs", len(period_faceoffs))
            with col_b:
                st.metric("SOG Events", len(period_sogs))

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
                first_faceoff = preview_first_faceoff_in_period(period_faceoffs)
                if first_faceoff:
                    st.markdown(
                        f"""
                        **Period {selected_period} Faceoff #{first_faceoff['faceoff_number']}**  
                        **Time:** {first_faceoff['time_in_period']}  
                        **Winner:** {first_faceoff['team']}  
                        **Event ID:** {first_faceoff['event_id']}
                        """
                    )
                else:
                    st.info("No faceoff found in this period.")

            with right:
                st.subheader("Next SOG After Faceoff #")
                max_faceoff_num = max(
                    [f["faceoff_number"] for f in period_faceoffs],
                    default=1,
                )
                chosen_faceoff_num = st.number_input(
                    "Faceoff number",
                    min_value=1,
                    max_value=max_faceoff_num,
                    value=1,
                    step=1,
                    key=f"faceoff_num_{selected_period}",
                )

                preview_sog, preview_error = preview_next_sog_after_faceoff(
                    period_faceoffs,
                    period_events,
                    chosen_faceoff_num,
                )

                if preview_error:
                    st.info(preview_error)
                else:
                    st.markdown(
                        f"""
                        **Period {selected_period} | After Faceoff #{chosen_faceoff_num}:**  
                        **Next SOG Event:** P{preview_sog['period']} {preview_sog['time_in_period']}  
                        **Team:** {preview_sog['team']}  
                        **Type:** {preview_sog['display_type']}  
                        **SOG #:** {preview_sog['sog_number']}  
                        **Event ID:** {preview_sog['event_id']}
                        """
                    )

    except Exception as e:
        st.error(f"Refresh error: {e}")
else:
    warning_box("STATUS: OK", "ok")
    st.info("Load live games, select one, and click Track Selected Game.")
