"""Daily statistics-only MLB Player Props model with dynamic milestones."""

from __future__ import annotations

import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import gspread
import pandas as pd
import requests
from google.oauth2.service_account import Credentials

from player_props_math import (
    binomial_tail,
    choose_dynamic_milestone,
    clamp,
    compound_total_bases_tail,
    normalized_outcome_probabilities,
    parse_baseball_innings,
    poisson_tail,
    prop_strength_score,
    select_limited_indices,
    stats_splits,
)


MODEL_VERSION = "Player Props V1.1.1 - Empty Stats Response Guard"
MODEL_TIMEZONE = os.environ.get("MLB_SCHEDULE_TZ", "America/New_York")
DATE_OVERRIDE = os.environ.get("MLB_SCHEDULE_DATE", "").strip()
SHEET_NAME = os.environ.get("SHEET_NAME", "Daily MLB HR Picks Scorecard")
SHEET_ID = os.environ.get("SHEET_ID", "1SsjWyqMsEW9yghahhuHCjDDtqlW2-56SbUWwmMXdig8")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

HITS_GATES = {1: 0.62, 2: 0.32, 3: 0.14}
TOTAL_BASES_GATES = {2: 0.48, 3: 0.34, 4: 0.24}
RBI_GATES = {1: 0.38, 2: 0.16, 3: 0.07}
STRIKEOUT_GATES = {4: 0.78, 5: 0.68, 6: 0.58, 7: 0.48, 8: 0.38, 9: 0.29, 10: 0.21}

PARK_FACTORS = {
    "Coors Field":120, "Great American Ball Park":114, "Yankee Stadium":111,
    "Citizens Bank Park":109, "Dodger Stadium":105, "UNIQLO Field at Dodger Stadium":100,
    "Fenway Park":104, "Daikin Park":103, "Minute Maid Park":103, "Globe Life Field":103,
    "Oriole Park at Camden Yards":102, "Angel Stadium":101, "Nationals Park":101,
    "Chase Field":100, "Truist Park":100, "Wrigley Field":100, "Sutter Health Park":100,
    "Busch Stadium":99, "Comerica Park":99, "Progressive Field":99, "Rogers Centre":99,
    "Target Field":98, "American Family Field":98, "PNC Park":98, "Rate Field":98,
    "Guaranteed Rate Field":98, "Citi Field":97, "T-Mobile Park":96,
    "Tropicana Field":95, "loanDepot park":95, "Petco Park":94, "Oracle Park":94,
}

STADIUMS = {
    "Nationals Park": (38.8730,-77.0074,False,20), "Tropicana Field": (27.7682,-82.6534,True,45),
    "Target Field": (44.9817,-93.2776,False,70), "T-Mobile Park": (47.5914,-122.3325,False,45),
    "Citizens Bank Park": (39.9061,-75.1665,False,5), "Fenway Park": (42.3467,-71.0972,False,45),
    "Yankee Stadium": (40.8296,-73.9262,False,65), "Great American Ball Park": (39.0974,-84.5066,False,35),
    "Truist Park": (33.8908,-84.4678,False,25), "American Family Field": (43.0280,-87.9712,True,100),
    "Wrigley Field": (41.9484,-87.6553,False,40), "Chase Field": (33.4455,-112.0667,True,0),
    "Angel Stadium": (33.8003,-117.8827,False,55), "Daikin Park": (29.7573,-95.3555,True,350),
    "Minute Maid Park": (29.7573,-95.3555,True,350), "Dodger Stadium": (34.0739,-118.2400,False,25),
    "UNIQLO Field at Dodger Stadium": (34.0739,-118.2400,False,25), "Coors Field": (39.7559,-104.9942,False,5),
    "Petco Park": (32.7073,-117.1566,False,0), "Oracle Park": (37.7786,-122.3893,False,95),
    "PNC Park": (40.4469,-80.0057,False,30), "Progressive Field": (41.4962,-81.6852,False,350),
    "Comerica Park": (42.3390,-83.0485,False,25), "Busch Stadium": (38.6226,-90.1928,False,75),
    "Rogers Centre": (43.6414,-79.3894,True,20), "loanDepot park": (25.7781,-80.2197,True,65),
    "Globe Life Field": (32.7473,-97.0842,True,75), "Oriole Park at Camden Yards": (39.2840,-76.6217,False,45),
    "Sutter Health Park": (38.5804,-121.5139,False,40), "Citi Field": (40.7571,-73.8458,False,55),
    "Rate Field": (41.8300,-87.6338,False,35), "Guaranteed Rate Field": (41.8300,-87.6338,False,35),
}


def resolve_date():
    if DATE_OVERRIDE:
        return datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d").date(), "Environment override MLB_SCHEDULE_DATE"
    now = datetime.now(ZoneInfo(MODEL_TIMEZONE))
    if now.hour >= 18:
        return now.date() + timedelta(days=1), f"{MODEL_TIMEZONE} evening run; using next MLB slate"
    return now.date(), f"Official MLB slate date from {MODEL_TIMEZONE}"


TODAY, DATE_LOGIC = resolve_date()
YEAR = TODAY.year
RUN_UTC = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
RUN_LOCAL = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")


def safe_float(value, default=0.0):
    try:
        result = float(value)
        return default if math.isnan(result) or math.isinf(result) else result
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def team_abbrev(team):
    return str(team.get("abbreviation") or team.get("fileCode") or team.get("name", ""))


def auth_google():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw and os.path.exists("service_account.json"):
        with open("service_account.json", "r", encoding="utf-8") as stream:
            raw = stream.read()
    if not raw:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON secret.")
    credentials = Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    return gspread.authorize(credentials)


def request_json(url, params=None, attempts=3):
    last_error = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, params=params, timeout=35)
            response.raise_for_status()
            return response.json()
        except Exception as error:
            last_error = error
            if attempt + 1 < attempts:
                import time
                time.sleep(1 + attempt)
    raise RuntimeError(f"Request failed: {url}: {last_error}")


def is_playable(status):
    text = str(status or "").strip().lower()
    return text in {"scheduled", "pre-game", "warmup"}


def weather_score(temp_f, humidity, wind_boost, dome):
    if dome:
        return 50.0
    score = 50.0
    if temp_f >= 90: score += 15
    elif temp_f >= 80: score += 10
    elif temp_f >= 70: score += 5
    elif temp_f < 55: score -= 10
    if humidity >= 65: score += 3
    return round(clamp(score + wind_boost, 0, 100), 1)


def angle_diff(left, right):
    return abs((float(left) - float(right) + 180) % 360 - 180)


def wind_effect(wind_from, mph, center_field, dome):
    if dome: return "Dome", 0.0
    if mph < 5: return "Calm", 0.0
    blowing_to = (wind_from + 180) % 360
    difference = angle_diff(blowing_to, center_field)
    if difference <= 35: return "Out", min(16, 4 + mph * 0.8)
    if difference <= 70: return "Cross/Out", min(8, 2 + mph * 0.35)
    if difference >= 145: return "In", -min(14, 3 + mph * 0.7)
    if difference >= 110: return "Cross/In", -min(7, 1 + mph * 0.35)
    return "Cross", 0.0


def get_weather(venue, game_date_utc):
    stadium = STADIUMS.get(venue)
    if not stadium:
        return {"TempF": 70, "Humidity": 50, "WindMPH": 0, "WindImpact": "Unknown", "WeatherScore": 50, "WeatherVerified": False}
    lat, lon, dome, center_field = stadium
    if dome:
        return {"TempF": 72, "Humidity": 50, "WindMPH": 0, "WindImpact": "Dome", "WeatherScore": 50, "WeatherVerified": True}
    try:
        payload = request_json(
            "https://api.open-meteo.com/v1/forecast",
            {
                "latitude": lat, "longitude": lon,
                "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m",
                "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                "timezone": "UTC", "forecast_days": 2,
            },
        )
        target = datetime.fromisoformat(str(game_date_utc).replace("Z", "+00:00"))
        times = [datetime.fromisoformat(value).replace(tzinfo=timezone.utc) for value in payload.get("hourly", {}).get("time", [])]
        index = min(range(len(times)), key=lambda i: abs((times[i] - target).total_seconds()))
        hourly = payload["hourly"]
        temp = safe_float(hourly["temperature_2m"][index], 70)
        humidity = safe_float(hourly["relative_humidity_2m"][index], 50)
        mph = safe_float(hourly["wind_speed_10m"][index], 0)
        direction = safe_float(hourly["wind_direction_10m"][index], 0)
        impact, boost = wind_effect(direction, mph, center_field, False)
        return {"TempF": round(temp,1), "Humidity": round(humidity,1), "WindMPH": round(mph,1), "WindImpact": impact, "WeatherScore": weather_score(temp, humidity, boost, False), "WeatherVerified": True}
    except Exception as error:
        print(f"Weather fallback for {venue}: {error}")
        return {"TempF": 70, "Humidity": 50, "WindMPH": 0, "WindImpact": "Weather unavailable", "WeatherScore": 50, "WeatherVerified": False}


def fetch_schedule():
    payload = request_json(
        "https://statsapi.mlb.com/api/v1/schedule",
        {"sportId": 1, "date": TODAY.isoformat(), "hydrate": "probablePitcher,team,venue"},
    )
    games, integrity = [], []
    team_game_counts = Counter()
    raw_games = [game for date_block in payload.get("dates", []) for game in date_block.get("games", [])]
    for game in raw_games:
        for side in ("away", "home"):
            team_id = game.get("teams", {}).get(side, {}).get("team", {}).get("id")
            if team_id: team_game_counts[int(team_id)] += 1
    for game in raw_games:
        away_block = game.get("teams", {}).get("away", {}) or {}
        home_block = game.get("teams", {}).get("home", {}) or {}
        away_team, home_team = away_block.get("team", {}) or {}, home_block.get("team", {}) or {}
        away_pitcher, home_pitcher = away_block.get("probablePitcher", {}) or {}, home_block.get("probablePitcher", {}) or {}
        venue = (game.get("venue", {}) or {}).get("name", "")
        status = (game.get("status", {}) or {}).get("detailedState", "")
        reasons = []
        if not game.get("gamePk"): reasons.append("Missing gamePk")
        if not is_playable(status): reasons.append(f"Non-playable status: {status}")
        if not away_team.get("id") or not home_team.get("id"): reasons.append("Missing team identity")
        if not away_pitcher.get("id") or not home_pitcher.get("id"): reasons.append("Missing probable pitcher")
        if not venue: reasons.append("Missing venue")
        if away_team.get("id") and team_game_counts[int(away_team["id"])] > 1: reasons.append("Doubleheader requires game-specific lineup")
        if home_team.get("id") and team_game_counts[int(home_team["id"])] > 1: reasons.append("Doubleheader requires game-specific lineup")
        weather = get_weather(venue, game.get("gameDate", "")) if not reasons else {"WeatherVerified": False}
        if not reasons and not weather.get("WeatherVerified"): reasons.append("Weather not tied to venue")
        record = {
            "Date": TODAY.isoformat(), "GamePk": game.get("gamePk", ""), "GameDateUTC": game.get("gameDate", ""),
            "Status": status, "Venue": venue, "ParkFactor": PARK_FACTORS.get(venue, 100),
            "AwayTeamID": away_team.get("id", ""), "AwayTeam": team_abbrev(away_team),
            "HomeTeamID": home_team.get("id", ""), "HomeTeam": team_abbrev(home_team),
            "AwayPitcherID": away_pitcher.get("id", ""), "AwayPitcher": away_pitcher.get("fullName", "Unknown"),
            "HomePitcherID": home_pitcher.get("id", ""), "HomePitcher": home_pitcher.get("fullName", "Unknown"),
            "Verified": not reasons, "VerificationNotes": "Verified" if not reasons else "; ".join(reasons),
            **weather,
        }
        integrity.append(record)
        if not reasons: games.append(record)
    if not integrity:
        raise RuntimeError(f"Official MLB schedule returned no games for {TODAY.isoformat()}.")
    return games, integrity


def fetch_active_players(team_ids):
    players = {}
    for team_id in sorted(set(int(value) for value in team_ids)):
        payload = request_json(f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster", {"rosterType": "active", "season": YEAR})
        for item in payload.get("roster", []):
            person = item.get("person", {}) or {}
            if person.get("id"):
                players[int(person["id"])] = {"Player": person.get("fullName", ""), "TeamID": team_id}
    return players


def fetch_hitter_stats(active_players):
    payload = request_json(
        "https://statsapi.mlb.com/api/v1/stats",
        {"stats": "season", "group": "hitting", "playerPool": "ALL", "season": YEAR, "sportIds": 1, "limit": 2000},
    )
    rows = []
    for split in stats_splits(payload):
        player = split.get("player", {}) or {}
        player_id = safe_int(player.get("id"))
        if player_id not in active_players: continue
        stat = split.get("stat", {}) or {}
        games = safe_int(stat.get("gamesPlayed"))
        plate_appearances = safe_int(stat.get("plateAppearances"))
        at_bats = safe_int(stat.get("atBats"))
        if games < 10 or plate_appearances / max(1, games) < 2.5: continue
        rows.append({
            "PlayerID": player_id, "Player": active_players[player_id]["Player"] or player.get("fullName", ""),
            "TeamID": active_players[player_id]["TeamID"], "Games": games, "PA": plate_appearances,
            "AB": at_bats, "Hits": safe_int(stat.get("hits")), "Doubles": safe_int(stat.get("doubles")),
            "Triples": safe_int(stat.get("triples")), "HR": safe_int(stat.get("homeRuns")),
            "RBI": safe_int(stat.get("rbi")), "AVG": safe_float(stat.get("avg")),
        })
    return rows


def fetch_team_hitting():
    payload = request_json(
        "https://statsapi.mlb.com/api/v1/teams/stats",
        {"group": "hitting", "stats": "season", "season": YEAR, "sportIds": 1},
    )
    result = {}
    for split in stats_splits(payload):
        team_id = safe_int((split.get("team", {}) or {}).get("id"))
        stat = split.get("stat", {}) or {}
        games = max(1, safe_int(stat.get("gamesPlayed"), 1))
        pa = max(1, safe_int(stat.get("plateAppearances"), 1))
        result[team_id] = {
            "RunsPerGame": safe_int(stat.get("runs")) / games,
            "StrikeoutRate": safe_int(stat.get("strikeOuts")) / pa,
        }
    return result


def fetch_pitcher_stats(player_id):
    payload = request_json(
        f"https://statsapi.mlb.com/api/v1/people/{int(player_id)}/stats",
        {"stats": "season", "group": "pitching", "season": YEAR},
    )
    splits = stats_splits(payload)
    stat = splits[0].get("stat", {}) if splits else {}
    starts = safe_int(stat.get("gamesStarted"))
    innings = parse_baseball_innings(stat.get("inningsPitched"))
    return {
        "ERA": safe_float(stat.get("era"), 4.50), "WHIP": safe_float(stat.get("whip"), 1.30),
        "K9": safe_float(stat.get("strikeoutsPer9Inn"), 8.0), "H9": safe_float(stat.get("hitsPer9Inn"), 8.5),
        "HR9": safe_float(stat.get("homeRunsPer9"), 1.15), "Starts": starts,
        "IPPerStart": innings / starts if starts else 0.0,
    }


def matchup_adjusted_outcomes(hitter, pitcher, park, weather):
    singles = max(0, hitter["Hits"] - hitter["Doubles"] - hitter["Triples"] - hitter["HR"])
    outs = max(0, hitter["AB"] - hitter["Hits"])
    probabilities = normalized_outcome_probabilities(
        [outs, singles, hitter["Doubles"], hitter["Triples"], hitter["HR"]],
        [33.0, 7.5, 2.0, 0.2, 1.1],
    )
    hit_factor = clamp(1.0 + (pitcher["WHIP"] - 1.30) * 0.10 + (pitcher["H9"] - 8.5) * 0.015, 0.86, 1.16)
    extra_factor = clamp(1.0 + (park - 100) * 0.006 + (weather - 50) * 0.003, 0.82, 1.22)
    hr_factor = clamp(extra_factor * (1.0 + (pitcher["HR9"] - 1.15) * 0.12), 0.78, 1.30)
    adjusted = [probabilities[0], probabilities[1] * hit_factor, probabilities[2] * hit_factor * extra_factor, probabilities[3] * hit_factor * extra_factor, probabilities[4] * hit_factor * hr_factor]
    total = sum(adjusted)
    return [value / total for value in adjusted]


def confidence_label(score):
    if score >= 76: return "★★★★★ Elite Prop"
    if score >= 70: return "★★★★ Strong Prop"
    if score >= 65: return "★★★ Solid Prop"
    return "★★ Watchlist"


def make_prop(base, prop_type, threshold, probability, gate, score, projection, reason):
    player = base["Player"]
    display = f"{player} {threshold}+ {prop_type}"
    prediction_id = "|".join([TODAY.isoformat(), str(base["GamePk"]), str(base["PlayerID"]), prop_type, str(threshold), MODEL_VERSION])
    return {
        "Prediction ID": prediction_id, "Date": TODAY.isoformat(), "Model Version": MODEL_VERSION,
        "Player Type": base["PlayerType"], "Player ID": base["PlayerID"], "Player": player,
        "Team": base["Team"], "Opponent": base["Opponent"], "GamePk": base["GamePk"],
        "Game": base["Game"], "HomeAway": base["HomeAway"], "Venue": base["Venue"],
        "Prop Type": prop_type, "Threshold": threshold, "Recommended Prop": display,
        "Projected Probability": round(probability * 100, 1), "Probability Gate": round(gate * 100, 1),
        "Projected Mean": round(projection, 2), "Prop Score": score, "Confidence": confidence_label(score),
        "Opposing Pitcher": base.get("OpposingPitcher", ""), "Projected PA/IP": round(base.get("UsageProjection", 0), 2),
        "ParkFactor": base.get("ParkFactor", 100), "WeatherScore": base.get("WeatherScore", 50),
        "Reason": reason, "Roster Verified": "Yes", "Schedule Verified": "Yes", "Result": "",
    }


def hitter_props(hitter, game, pitcher, team_stats, team_side):
    team_id = hitter["TeamID"]
    opponent_id = game["HomeTeamID"] if team_side == "Away" else game["AwayTeamID"]
    team = game["AwayTeam"] if team_side == "Away" else game["HomeTeam"]
    opponent = game["HomeTeam"] if team_side == "Away" else game["AwayTeam"]
    expected_ab = int(round(clamp(hitter["AB"] / max(1, hitter["Games"]), 3.0, 5.0)))
    expected_pa = clamp(hitter["PA"] / max(1, hitter["Games"]), 3.2, 5.1)
    outcomes = matchup_adjusted_outcomes(hitter, pitcher, game["ParkFactor"], game["WeatherScore"])
    hit_probability = sum(outcomes[1:])
    base = {
        "PlayerType": "Hitter", "PlayerID": hitter["PlayerID"], "Player": hitter["Player"],
        "Team": team, "Opponent": opponent, "GamePk": game["GamePk"], "Game": f"{game['AwayTeam']} @ {game['HomeTeam']}",
        "HomeAway": team_side, "Venue": game["Venue"], "OpposingPitcher": game["HomePitcher"] if team_side == "Away" else game["AwayPitcher"],
        "UsageProjection": expected_pa, "ParkFactor": game["ParkFactor"], "WeatherScore": game["WeatherScore"],
    }
    props = []
    hit_probs = {threshold: binomial_tail(expected_ab, hit_probability, threshold) for threshold in HITS_GATES}
    selected = choose_dynamic_milestone(hit_probs, HITS_GATES)
    if selected:
        threshold, probability, gate = selected
        score = prop_strength_score(probability, gate, threshold, 4.0)
        reason = f"Season AVG {hitter['AVG']:.3f}; {expected_ab} projected at-bats; opposing starter WHIP {pitcher['WHIP']:.2f} and H/9 {pitcher['H9']:.1f}."
        props.append(make_prop(base, "Hits", threshold, probability, gate, score, expected_ab * hit_probability, reason))
    tb_probs = {threshold: compound_total_bases_tail(expected_ab, outcomes[1], outcomes[2], outcomes[3], outcomes[4], threshold) for threshold in TOTAL_BASES_GATES}
    selected = choose_dynamic_milestone(tb_probs, TOTAL_BASES_GATES)
    if selected:
        threshold, probability, gate = selected
        score = prop_strength_score(probability, gate, threshold, 3.0)
        xbh_rate = (hitter["Doubles"] + hitter["Triples"] + hitter["HR"]) / max(1, hitter["AB"])
        reason = f"Extra-base-hit rate {xbh_rate:.1%}; park factor {game['ParkFactor']}; weather score {game['WeatherScore']:.1f}; opposing starter HR/9 {pitcher['HR9']:.2f}."
        props.append(make_prop(base, "Total Bases", threshold, probability, gate, score, sum(index * probability_value for index, probability_value in enumerate(outcomes)) * expected_ab, reason))
    team_rpg = team_stats.get(team_id, {}).get("RunsPerGame", 4.3)
    rbi_per_game = (hitter["RBI"] + 15 * 0.55) / (hitter["Games"] + 15)
    rbi_mean = rbi_per_game * clamp(team_rpg / 4.3, 0.80, 1.25) * clamp(1 + (pitcher["ERA"] - 4.30) * 0.06, 0.78, 1.25) * clamp(1 + (game["ParkFactor"] - 100) * 0.005, 0.88, 1.15)
    rbi_probs = {threshold: poisson_tail(rbi_mean, threshold) for threshold in RBI_GATES}
    selected = choose_dynamic_milestone(rbi_probs, RBI_GATES)
    if selected:
        threshold, probability, gate = selected
        score = prop_strength_score(probability, gate, threshold, 3.0)
        reason = f"Projected RBI mean {rbi_mean:.2f}; team scoring {team_rpg:.2f} runs/game; opposing starter ERA {pitcher['ERA']:.2f}."
        props.append(make_prop(base, "RBIs", threshold, probability, gate, score, rbi_mean, reason))
    return props


def pitcher_prop(game, side, pitcher, opponent_stats):
    if pitcher["Starts"] < 3 or pitcher["IPPerStart"] < 3.5: return []
    player_id = game["AwayPitcherID"] if side == "Away" else game["HomePitcherID"]
    player = game["AwayPitcher"] if side == "Away" else game["HomePitcher"]
    team = game["AwayTeam"] if side == "Away" else game["HomeTeam"]
    opponent = game["HomeTeam"] if side == "Away" else game["AwayTeam"]
    opponent_id = game["HomeTeamID"] if side == "Away" else game["AwayTeamID"]
    opponent_k_rate = opponent_stats.get(opponent_id, {}).get("StrikeoutRate", 0.225)
    projected_ip = clamp(pitcher["IPPerStart"], 4.0, 7.2)
    strikeout_mean = pitcher["K9"] * projected_ip / 9.0 * clamp(opponent_k_rate / 0.225, 0.78, 1.28)
    probabilities = {threshold: poisson_tail(strikeout_mean, threshold) for threshold in STRIKEOUT_GATES}
    selected = choose_dynamic_milestone(probabilities, STRIKEOUT_GATES)
    if not selected: return []
    threshold, probability, gate = selected
    score = prop_strength_score(probability, gate, threshold, 1.6)
    base = {
        "PlayerType": "Pitcher", "PlayerID": player_id, "Player": player, "Team": team,
        "Opponent": opponent, "GamePk": game["GamePk"], "Game": f"{game['AwayTeam']} @ {game['HomeTeam']}",
        "HomeAway": side, "Venue": game["Venue"], "OpposingPitcher": "",
        "UsageProjection": projected_ip, "ParkFactor": game["ParkFactor"], "WeatherScore": game["WeatherScore"],
    }
    reason = f"K/9 {pitcher['K9']:.1f}; {projected_ip:.1f} projected innings; opponent strikeout rate {opponent_k_rate:.1%}; projected strikeouts {strikeout_mean:.2f}."
    return [make_prop(base, "Strikeouts", threshold, probability, gate, score, strikeout_mean, reason)]


def build_model():
    games, integrity = fetch_schedule()
    if not games:
        raise RuntimeError("No fully verified games available for Player Props.")
    team_ids = [game[key] for game in games for key in ("AwayTeamID", "HomeTeamID")]
    active_players = fetch_active_players(team_ids)
    hitters = fetch_hitter_stats(active_players)
    team_stats = fetch_team_hitting()
    hitters_by_team = defaultdict(list)
    for hitter in hitters: hitters_by_team[hitter["TeamID"]].append(hitter)
    pitcher_cache = {}
    props = []
    for game in games:
        for pitcher_id in (game["AwayPitcherID"], game["HomePitcherID"]):
            pitcher_cache[int(pitcher_id)] = fetch_pitcher_stats(pitcher_id)
        away_pitcher = pitcher_cache[int(game["AwayPitcherID"])]
        home_pitcher = pitcher_cache[int(game["HomePitcherID"])]
        for hitter in sorted(hitters_by_team[game["AwayTeamID"]], key=lambda row: row["PA"] / row["Games"], reverse=True)[:9]:
            props.extend(hitter_props(hitter, game, home_pitcher, team_stats, "Away"))
        for hitter in sorted(hitters_by_team[game["HomeTeamID"]], key=lambda row: row["PA"] / row["Games"], reverse=True)[:9]:
            props.extend(hitter_props(hitter, game, away_pitcher, team_stats, "Home"))
        props.extend(pitcher_prop(game, "Away", away_pitcher, team_stats))
        props.extend(pitcher_prop(game, "Home", home_pitcher, team_stats))
    frame = pd.DataFrame(props)
    if frame.empty: raise RuntimeError("No Player Props cleared the dynamic statistical gates.")
    frame = frame.sort_values(["Prop Score", "Projected Probability"], ascending=False).reset_index(drop=True)
    frame["Overall Rank"] = frame.index + 1
    return frame, pd.DataFrame(integrity)


def select_report_card(model):
    selected_indices = select_limited_indices(model.to_dict("records"))
    card = model.loc[selected_indices].copy().reset_index(drop=True)
    card["Report Rank"] = card.index + 1
    card["Report Section"] = card["Report Rank"].apply(lambda rank: "Top Props" if rank <= 12 else "Watchlist")
    return card


def get_or_create_sheet(workbook, title, rows=1000, cols=40):
    try: return workbook.worksheet(title)
    except gspread.WorksheetNotFound: return workbook.add_worksheet(title=title, rows=rows, cols=cols)


def column_letter(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def ensure_header(worksheet, headers):
    existing = worksheet.row_values(1)
    if existing and existing[:len(headers)] != headers:
        raise RuntimeError(f"Header mismatch on {worksheet.title}; refusing to shift historical data.")
    if not existing:
        worksheet.update(values=[headers], range_name=f"A1:{column_letter(len(headers))}1")


def append_unique(worksheet, headers, rows):
    ensure_header(worksheet, headers)
    existing = set(worksheet.col_values(1)[1:])
    new_rows = [[row.get(header, "") for header in headers] for row in rows if row.get("Prediction ID") not in existing]
    if new_rows: worksheet.append_rows(new_rows, value_input_option="USER_ENTERED")


def build_email_rows(card, model):
    rows = [
        ["Daily MLB Player Props - Dynamic Statistical Milestones"], ["Last Updated", RUN_LOCAL],
        ["Model Version", MODEL_VERSION], ["Schedule Date Used", TODAY.isoformat()], ["Schedule Date Logic", DATE_LOGIC], [],
        ["Daily Outlook"], ["Verified Props Evaluated", len(model)],
        ["Top Overall Prop", card.iloc[0]["Recommended Prop"] + " - " + card.iloc[0]["Confidence"]],
        ["Selection Method", "Highest statistically supported milestone; no sportsbook or market information."],
    ]
    rows.extend([[], ["Category Leaders"]])
    for prop_type in ["Hits", "Total Bases", "RBIs", "Strikeouts"]:
        subset = model[model["Prop Type"] == prop_type]
        if subset.empty:
            rows.append([f"Best {prop_type}", "No candidate cleared the statistical reliability gate."])
        else:
            leader = subset.iloc[0]
            rows.append([
                f"Best {prop_type}",
                f"{leader['Recommended Prop']} | {leader['Projected Probability']:.1f}% | {leader['Confidence']}",
            ])
    for section in ["Top Props", "Watchlist"]:
        rows.extend([[], [section]])
        for _, prop in card[card["Report Section"] == section].iterrows():
            rows.extend([
                [f"{int(prop['Report Rank'])}. {prop['Confidence']}", f"{prop['Recommended Prop']} | {prop['Game']} | {prop['Venue']}"],
                ["Projected Probability", f"{prop['Projected Probability']:.1f}%"],
                ["Projected Mean", f"{prop['Projected Mean']:.2f}"],
                ["Why Today", prop["Reason"]],
                ["Verification", "Official schedule verified | Active roster verified | Probable pitcher verified | Venue/weather verified"],
            ])
    rows.extend([[], ["Model Notes"],
        ["Threshold Logic", "The model evaluates multiple milestones and selects the highest threshold clearing its category-specific reliability gate."],
        ["Ranking Basis", "Season production, projected opportunities, opponent profile, team context, park, and game-time weather."],
        ["Lineup Status", "Morning model uses active-roster and playing-time verification; confirmed starting lineups may not yet be available."],
        ["Data Constraint", "Statistics only. No sportsbook odds, lines, implied probability, or market influence."],
        ["Results Tracking", "Published props will be archived and graded separately after games become final."],
    ])
    return rows


def write_to_sheet(model, card, integrity):
    client = auth_google()
    workbook = client.open_by_key(SHEET_ID)
    current_ws = get_or_create_sheet(workbook, "Player Props", 100, 40)
    history_ws = get_or_create_sheet(workbook, "Player Props Model Results", 5000, 40)
    email_ws = get_or_create_sheet(workbook, "Player Props Email Summary", 250, 10)
    integrity_ws = get_or_create_sheet(workbook, "Player Props Integrity Log", 1000, 35)
    run_ws = get_or_create_sheet(workbook, "Player Props Run Log", 100, 10)
    headers = [
        "Prediction ID", "Date", "Model Version", "Overall Rank", "Report Rank", "Report Section",
        "Player Type", "Player ID", "Player", "Team", "Opponent", "GamePk", "Game", "HomeAway", "Venue",
        "Prop Type", "Threshold", "Recommended Prop", "Projected Probability", "Probability Gate", "Projected Mean",
        "Prop Score", "Confidence", "Opposing Pitcher", "Projected PA/IP", "ParkFactor", "WeatherScore", "Reason",
        "Roster Verified", "Schedule Verified", "Result",
    ]
    current_rows = card.to_dict("records")
    current_ws.clear()
    current_ws.update(values=[headers] + [[row.get(header, "") for header in headers] for row in current_rows], range_name=f"A1:{column_letter(len(headers))}{len(current_rows)+1}")
    append_unique(history_ws, headers, model.to_dict("records"))
    email_rows = build_email_rows(card, model)
    email_ws.clear(); email_ws.update(values=email_rows, range_name=f"A1:B{len(email_rows)}")
    integrity_headers = list(integrity.columns)
    integrity_ws.clear(); integrity_ws.update(values=[integrity_headers] + integrity.fillna("").values.tolist(), range_name=f"A1:{column_letter(len(integrity_headers))}{len(integrity)+1}")
    run_rows = [
        ["Run Timestamp UTC", RUN_UTC], ["Run Timestamp Pacific", RUN_LOCAL], ["Schedule Date Used", TODAY.isoformat()],
        ["Schedule Date Logic", DATE_LOGIC], ["Model Version", MODEL_VERSION], ["Verified Games", int(integrity["Verified"].sum())],
        ["Eligible Props", len(model)], ["Published Props", len(card)], ["Top Props", len(card[card["Report Section"] == "Top Props"])],
        ["Watchlist", len(card[card["Report Section"] == "Watchlist"])], ["Status", "Completed Successfully"],
    ]
    run_ws.clear(); run_ws.update(values=run_rows, range_name=f"A1:B{len(run_rows)}")
    print(f"Updated {SHEET_NAME}: {len(card)} published Player Props")


def main():
    print(f"Starting {MODEL_VERSION} for {TODAY.isoformat()}")
    model, integrity = build_model()
    card = select_report_card(model)
    write_to_sheet(model, card, integrity)
    for _, row in card.head(12).iterrows():
        print(f"{int(row['Report Rank'])}. {row['Recommended Prop']} | {row['Projected Probability']:.1f}% | {row['Confidence']}")


if __name__ == "__main__":
    main()
