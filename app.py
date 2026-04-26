import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from collections import defaultdict

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


def extract_abbrev(value, fallback="UNK"):
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        if value.get("default"):
            return value["default"]
        for v in value.values():
            if isinstance(v, str) and v:
                return v
    return fallback


def load_live_games():
    data = fetch_json(SCOREBOARD_URL)
    games = []
    for day in data.get("gamesByDate", []):
        for game in day.get("games", []):
            if game.get("gameState") not in {"LIVE", "CRIT"}:
                continue
            away = extract_abbrev(game.get("awayTeam", {}).get("abbrev"), "AWAY")
            home = extract_abbrev(game.get("homeTeam", {}).get("abbrev"), "HOME")
            game_id = game.get("id")
            games.append({
                "label": f"{away} @ {home} ({game_id})",
                "id": game_id,
                "away": away,
                "home": home,
            })
    return games


def parse_clock_to_seconds(clock_str: str):
    try:
        minutes, seconds = clock_str.split(":")
        return int(minutes) * 60 + int(seconds)
    except Exception:
        return None


def seconds_to_clock(total_seconds: int) -> str:
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


def convert_to_time_remaining(clock_str: str, period: int | None, game_data=None) -> str:
    secs_elapsed = parse_clock_to_seconds(clock_str)
    if secs_elapsed is None:
        return clock_str

    period_len = 1200
    if period is not None and period > 3:
        game_type = str(game_data.get("gameType", "")).strip() if game_data else ""
        if game_type in {"2", "02"}:
            period_len = 300
        elif game_type not in {"3", "03"}:
            period_len = 1200 if secs_elapsed > 300 else 300

    return seconds_to_clock(max(0, period_len - secs_elapsed))


def build_team_lookup(game_data: dict) -> dict:
    lookup = {}
    for key, fallback in (("homeTeam", "HOME"), ("awayTeam", "AWAY")):
        team = game_data.get(key) or {}
        team_id = team.get("id")
        if team_id is not None:
            lookup[team_id] = extract_abbrev(team.get("abbrev"), fallback)
    return lookup


def get_home_away_abbrevs(game_data: dict):
    home = game_data.get("homeTeam") or {}
    away = game_data.get("awayTeam") or {}
    return (
        extract_abbrev(home.get("abbrev"), "HOME"),
        extract_abbrev(away.get("abbrev"), "AWAY"),
    )


def safe_team(play: dict, team_lookup: dict) -> str:
    details = play.get("details") or {}

    for team_id in (
        play.get("eventOwnerTeamId"),
        play.get("teamId"),
        details.get("eventOwnerTeamId"),
        details.get("teamId"),
    ):
        if team_id in team_lookup:
            return team_lookup[team_id]

    team_dict = play.get("team")
    for abbrev in (
        play.get("teamAbbrev"),
        team_dict.get("abbrev") if isinstance(team_dict, dict) else None,
        details.get("eventOwnerTeamAbbrev"),
        details.get("teamAbbrev"),
        details.get("winningTeamAbbrev"),
    ):
        parsed = extract_abbrev(abbrev, None)
        if parsed:
            return parsed

    return "UNK"


def label_home_away(team: str, home_abbrev: str, away_abbrev: str) -> str:
    if team == home_abbrev:
        return f"{team} (Home)"
    if team == away_abbrev:
        return f"{team} (Away)"
    return team


def parse_raw_events(game_data: dict) -> list[dict]:
    plays = game_data.get("plays") or []
    team_lookup = build_team_lookup(game_data)
    home_abbrev, away_abbrev = get_home_away_abbrevs(game_data)

    deduped = {}
    for play in plays:
        play_type = str(play.get("typeDescKey", "")).lower()
        if play_type not in SHOT_TYPES and play_type != FACEOFF_TYPE:
            continue

        if play_type == FACEOFF_TYPE:
            display_type = "FACEOFF"
        elif play_type == "shot-on-goal":
            display_type = "SOG"
        else:
            display_type = "GOAL"

        team = label_home_away(safe_team(play, team_lookup), home_abbrev, away_abbrev)
        period = (play.get("periodDescriptor") or {}).get("number")

        deduped[play.get("eventId")] = {
            "event_id": play.get("eventId"),
            "period": period,
            "time_in_period_raw": play.get("timeInPeriod", ""),
            "time_remaining": convert_to_time_remaining(
                play.get("timeInPeriod", ""), period, game_data
            ),
            "team": team,
            "raw_type": play_type,
            "display_type": display_type,
        }

    return list(deduped.values())


def add_period_local_numbers(events: list[dict]) -> list[dict]:
    faceoff_counts: dict[int, int] = defaultdict(int)
    sog_counts: dict[int, int] = defaultdict(int)
    numbered = []

    for event in events:
        period = event["period"]
        event_copy = {**event, "faceoff_number": None, "sog_number": None}

        if event["display_type"] == "FACEOFF":
            faceoff_counts[period] += 1
            event_copy["faceoff_number"] = faceoff_counts[period]
        elif event["display_type"] in {"SOG", "GOAL"}:
            sog_counts[period] += 1
            event_copy["sog_number"] = sog_counts[period]

        numbered.append(event_copy)

    return numbered


def get_game_state(game_id: int) -> dict:
    data = fetch_json(PBP_URL.format(game_id=game_id))
    events = add_period_local_numbers(parse_raw_events(data))

    faceoffs = [e for e in events if e["display_type"] == "FACEOFF"]
    sog_events = [e for e in events if e["display_type"] in {"SOG", "GOAL"}]

    by_period_faceoffs: dict[int, int] = defaultdict(int)
    by_period_sog: dict[int, int] = defaultdict(int)
    for e in faceoffs:
        by_period_faceoffs[e["period"]] += 1
    for e in sog_events:
        by_period_sog[e["period"]] += 1

    live_period = events[-1]["period"] if events else 1
    live_period_faceoffs = [e for e in faceoffs if e["period"] == live_period]
    home_abbrev, away_abbrev = get_home_away_abbrevs(data)

    return {
        "events": events,
        "faceoffs": faceoffs,
        "sog_events": sog_events,
        "by_period_faceoffs": dict(by_period_faceoffs),
        "by_period_sog": dict(by_period_sog),
        "faceoff_total": len(faceoffs),
        "sog_total": len(sog_events),
        "live_period": live_period,
        "live_period_faceoff_count": len(live_period_faceoffs),
        "last_faceoff": faceoffs[-1] if faceoffs else None,
        "home_abbrev": home_abbrev,
        "away_abbrev": away_abbrev,
        "home_label": f"{home_abbrev} (Home)",
        "away_label": f"{away_abbrev} (Away)",
    }


def bucket_label(start_sec: int, end_sec: int) -> str:
    return f"{seconds_to_clock(start_sec)}-{seconds_to_clock(end_sec)}"


def build_two_minute_buckets(period_events: list[dict], home_label: str, away_label: str) -> list[dict]:
    buckets = [
        (1200, 1081), (1080, 961), (960, 841), (840, 721), (720, 601),
        (600, 481), (480, 361), (360, 241), (240, 121), (120, 1),
    ]
    sogs = [e for e in period_events if e["display_type"] in {"SOG", "GOAL"}]

    results = []
    for start, end in buckets:
        hits = [
            e for e in sogs
            if (secs := parse_clock_to_seconds(e["time_remaining"])) is not None
            and end <= secs <= start
        ]
        results.append({
            "window": bucket_label(start, end),
            "home_result": "YES" if any(e["team"] == home_label for e in hits) else "NO",
            "away_result": "YES" if any(e["team"] == away_label for e in hits) else "NO",
        })
    return results


def build_first_sog_after_faceoff(period_faceoffs: list[dict], period_events: list[dict]) -> list[dict]:
    results = []
    for faceoff in period_faceoffs:
        first_sog = None
        found_anchor = False
        for event in period_events:
            if event["event_id"] == faceoff["event_id"]:
                found_anchor = True
                continue
            if found_anchor and event["display_type"] in {"SOG", "GOAL"}:
                first_sog = event
                break

        results.append({
            "Faceoff #": faceoff["faceoff_number"],
            "Faceoff Time": faceoff["time_remaining"],
            "Faceoff Team": faceoff["team"],
            "First Shot": f"{first_sog['time_remaining']} {first_sog['team']}" if first_sog else "NO",
        })
    return results


_WARNING_STYLES = {
    "alert": ("background-color:#3a1600", "color:#ffd966", "border:2px solid #ff9900"),
    "ok": ("background-color:#132117", "color:#66ff99", "border:2px solid #2e6b45"),
}


def warning_box(message: str, warning_type: str):
    style = "; ".join(_WARNING_STYLES.get(warning_type, _WARNING_STYLES["ok"]))
    st.markdown(
        f'<div style="margin-top:10px; margin-bottom:18px; padding:16px; border-radius:10px;'
        f' font-size:26px; font-weight:700; {style}">{message}</div>',
        unsafe_allow_html=True,
    )


def render_event_table(events: list[dict], title: str):
    st.subheader(title)
    if not events:
        st.info("No events to show.")
        return

    rows = [
        {
            "Period": e["period"],
            "Clock": e["time_remaining"],
            "Type": e["display_type"],
            "Team": e["team"],
            "Faceoff #": e["faceoff_number"] if e["faceoff_number"] is not None else "",
            "SOG #": e["sog_number"] if e["sog_number"] is not None else "",
        }
        for e in events
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def preview_next_sog_after_faceoff(period_faceoffs: list[dict], period_events: list[dict], faceoff_number: int):
    anchor = next((f for f in period_faceoffs if f["faceoff_number"] == faceoff_number), None)
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


# --- App ---

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
                delta = live_period_faceoff_count - previous_faceoff_count
                if delta < 0:
                    st.session_state.warning_message = (
                        f"⚠ COUNT DECREASE: {previous_faceoff_count} → {live_period_faceoff_count}"
                    )
                    st.session_state.warning_type = "alert"
                elif delta > 1:
                    st.session_state.warning_message = f"⚠ MULTIPLE FACEOFFS ADDED: +{delta}"
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

        warning_box(st.session_state.warning_message, st.session_state.warning_type)

        a, b, c, d = st.columns(4)
        with a:
            st.metric("Live Period", live_period)
        with b:
            st.metric("Faceoffs (Live Period)", live_period_faceoff_count)
        with c:
            st.metric("Faceoffs (Game)", state["faceoff_total"])
        with d:
            st.metric("SOG Events (Game)", state["sog_total"])

        if lf := state["last_faceoff"]:
            st.markdown(
                f"**Last Faceoff:** P{lf['period']} {lf['time_remaining']} | {lf['team']} | Faceoff #{lf['faceoff_number']}"
            )
        else:
            st.markdown("**Last Faceoff:** none")

        st.divider()

        periods_present = sorted({e["period"] for e in state["events"] if e["period"] is not None}) or [1]

        with st.columns([1, 2])[0]:
            selected_period = st.selectbox(
                "Review period",
                options=periods_present,
                index=periods_present.index(live_period) if live_period in periods_present else len(periods_present) - 1,
            )

        period_events = [e for e in state["events"] if e["period"] == selected_period]
        period_faceoffs = [e for e in period_events if e["display_type"] == "FACEOFF"]
        period_sogs = [e for e in period_events if e["display_type"] in {"SOG", "GOAL"}]

        tab1, tab2, tab3, tab4 = st.tabs(["Timeline", "Period Review", "2-Min SOG Buckets", "Market Preview"])

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
            bucket_results = build_two_minute_buckets(period_events, state["home_label"], state["away_label"])
            rows = [
                {
                    "Window": b["window"],
                    state["home_label"]: b["home_result"],
                    state["away_label"]: b["away_result"],
                }
                for b in bucket_results
            ]
            st.dataframe(rows, use_container_width=True, hide_index=True)

        with tab4:
            left, right = st.columns(2)

            with left:
                st.subheader("Next Faceoff Preview")
                first_faceoff = period_faceoffs[0] if period_faceoffs else None
                if first_faceoff:
                    st.markdown(
                        f"**Period {selected_period} Faceoff #{first_faceoff['faceoff_number']}**  \n"
                        f"**Time:** {first_faceoff['time_remaining']}  \n"
                        f"**Team:** {first_faceoff['team']}"
                    )
                else:
                    st.info("No faceoff found in this period.")

            with right:
                st.subheader("First Shot After Each Faceoff")
                rows = build_first_sog_after_faceoff(period_faceoffs, period_events)
                if rows:
                    st.dataframe(rows, use_container_width=True, hide_index=True)
                else:
                    st.info("No faceoffs found in this period.")

    except Exception as e:
        st.error(f"Refresh error: {e}")
else:
    warning_box("STATUS: OK", "ok")
    st.info("Load live games, select one, and click Track Selected Game.")
