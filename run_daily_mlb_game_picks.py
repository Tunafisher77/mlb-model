import os
import time
import json
import math
import requests
import pandas as pd
import numpy as np
from datetime import date, datetime, timedelta
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
import gspread
from google.oauth2.service_account import Credentials

MODEL_VERSION = "Game Picks V2.1.1 - Schedule Integrity Fix + Eastern Slate Date"
MLB_SCHEDULE_TZ = os.environ.get("MLB_SCHEDULE_TZ", "America/New_York")
MLB_SCHEDULE_DATE_OVERRIDE = os.environ.get("MLB_SCHEDULE_DATE", "").strip()


def now_in_mlb_timezone():
    """Return current datetime in MLB slate timezone.

    GitHub Actions and Apps Script can run in UTC or Pacific time. MLB schedule dates
    should be selected using Eastern time so a late-night Pacific run does not pull
    yesterday's schedule for the next morning's report.
    """
    if ZoneInfo:
        return datetime.now(ZoneInfo(MLB_SCHEDULE_TZ))
    return datetime.utcnow()


def resolve_schedule_date():
    """Resolve the official schedule date used for this report.

    Priority:
      1. MLB_SCHEDULE_DATE env override in YYYY-MM-DD format for manual backfills/tests.
      2. Eastern-time date. If the workflow is run in the evening Eastern time, assume it
         is preparing tomorrow morning's report and use the next calendar date.

    This prevents the common failure where a 10 PM Pacific GitHub run still has a
    Pacific date of yesterday while MLB's slate date has already advanced in Eastern time.
    """
    if MLB_SCHEDULE_DATE_OVERRIDE:
        try:
            return datetime.strptime(MLB_SCHEDULE_DATE_OVERRIDE, "%Y-%m-%d").date(), "Environment override MLB_SCHEDULE_DATE"
        except Exception:
            raise RuntimeError("Invalid MLB_SCHEDULE_DATE. Use YYYY-MM-DD.")

    now_mlb = now_in_mlb_timezone()
    # Most production runs happen before the morning email. If run after 6 PM ET,
    # prepare the next day's slate rather than re-scoring games already underway/final.
    if now_mlb.hour >= 18:
        return (now_mlb.date() + timedelta(days=1)), f"{MLB_SCHEDULE_TZ} evening run; using next day's MLB slate"
    return now_mlb.date(), f"{MLB_SCHEDULE_TZ} current MLB slate date"


TODAY, SCHEDULE_DATE_REASON = resolve_schedule_date()
YEAR = TODAY.year
SHEET_NAME = os.environ.get("SHEET_NAME", "Daily MLB HR Picks Scorecard")
SHEET_ID = os.environ.get("SHEET_ID", "1SsjWyqMsEW9yghahhuHCjDDtqlW2-56SbUWwmMXdig8")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

TEAM_FIX = {
    "Arizona Diamondbacks":"ARI","Atlanta Braves":"ATL","Baltimore Orioles":"BAL","Boston Red Sox":"BOS",
    "Chicago White Sox":"CWS","Chicago Cubs":"CHC","Cincinnati Reds":"CIN","Cleveland Guardians":"CLE",
    "Colorado Rockies":"COL","Detroit Tigers":"DET","Houston Astros":"HOU","Kansas City Royals":"KC",
    "Los Angeles Angels":"LAA","Los Angeles Dodgers":"LAD","Miami Marlins":"MIA","Milwaukee Brewers":"MIL",
    "Minnesota Twins":"MIN","New York Yankees":"NYY","New York Mets":"NYM","Athletics":"ATH",
    "Philadelphia Phillies":"PHI","Pittsburgh Pirates":"PIT","San Diego Padres":"SD","San Francisco Giants":"SF",
    "Seattle Mariners":"SEA","St. Louis Cardinals":"STL","Tampa Bay Rays":"TB","Texas Rangers":"TEX",
    "Toronto Blue Jays":"TOR","Washington Nationals":"WSH"
}

PARK_FACTORS = {
    "Coors Field":120,"Great American Ball Park":114,"Yankee Stadium":111,"Citizens Bank Park":109,
    "Dodger Stadium":105,"Fenway Park":104,"Daikin Park":103,"Minute Maid Park":103,"Globe Life Field":103,
    "Oriole Park at Camden Yards":102,"Angel Stadium":101,"Nationals Park":101,"Chase Field":100,
    "Truist Park":100,"Busch Stadium":99,"Comerica Park":99,"Progressive Field":99,"Rogers Centre":99,
    "Target Field":98,"American Family Field":98,"PNC Park":98,"Wrigley Field":100,"T-Mobile Park":96,
    "Tropicana Field":95,"loanDepot park":95,"Petco Park":94,"Oracle Park":94,"Sutter Health Park":100,"UNIQLO Field at Dodger Stadium":100
}

STADIUMS = {
    "Nationals Park": {"lat":38.8730,"lon":-77.0074,"dome":False,"cf":20},
    "Tropicana Field": {"lat":27.7682,"lon":-82.6534,"dome":True,"cf":45},
    "Target Field": {"lat":44.9817,"lon":-93.2776,"dome":False,"cf":70},
    "T-Mobile Park": {"lat":47.5914,"lon":-122.3325,"dome":False,"cf":45},
    "Citizens Bank Park": {"lat":39.9061,"lon":-75.1665,"dome":False,"cf":5},
    "Fenway Park": {"lat":42.3467,"lon":-71.0972,"dome":False,"cf":45},
    "Yankee Stadium": {"lat":40.8296,"lon":-73.9262,"dome":False,"cf":65},
    "Great American Ball Park": {"lat":39.0974,"lon":-84.5066,"dome":False,"cf":35},
    "Truist Park": {"lat":33.8908,"lon":-84.4678,"dome":False,"cf":25},
    "American Family Field": {"lat":43.0280,"lon":-87.9712,"dome":True,"cf":100},
    "Wrigley Field": {"lat":41.9484,"lon":-87.6553,"dome":False,"cf":40},
    "Chase Field": {"lat":33.4455,"lon":-112.0667,"dome":True,"cf":0},
    "Angel Stadium": {"lat":33.8003,"lon":-117.8827,"dome":False,"cf":55},
    "Daikin Park": {"lat":29.7573,"lon":-95.3555,"dome":True,"cf":350},
    "Minute Maid Park": {"lat":29.7573,"lon":-95.3555,"dome":True,"cf":350},
    "Dodger Stadium": {"lat":34.0739,"lon":-118.2400,"dome":False,"cf":25},
    "UNIQLO Field at Dodger Stadium": {"lat":34.0739,"lon":-118.2400,"dome":False,"cf":25},
    "Coors Field": {"lat":39.7559,"lon":-104.9942,"dome":False,"cf":5},
    "Petco Park": {"lat":32.7073,"lon":-117.1566,"dome":False,"cf":0},
    "Oracle Park": {"lat":37.7786,"lon":-122.3893,"dome":False,"cf":95},
    "PNC Park": {"lat":40.4469,"lon":-80.0057,"dome":False,"cf":30},
    "Progressive Field": {"lat":41.4962,"lon":-81.6852,"dome":False,"cf":350},
    "Comerica Park": {"lat":42.3390,"lon":-83.0485,"dome":False,"cf":25},
    "Busch Stadium": {"lat":38.6226,"lon":-90.1928,"dome":False,"cf":75},
    "Rogers Centre": {"lat":43.6414,"lon":-79.3894,"dome":True,"cf":20},
    "loanDepot park": {"lat":25.7781,"lon":-80.2197,"dome":True,"cf":65},
    "Globe Life Field": {"lat":32.7473,"lon":-97.0842,"dome":True,"cf":75},
    "Oriole Park at Camden Yards": {"lat":39.2840,"lon":-76.6217,"dome":False,"cf":45},
    "Sutter Health Park": {"lat":38.5804,"lon":-121.5139,"dome":False,"cf":40},
}


def auth_google():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw and os.path.exists("service_account.json"):
        raw = open("service_account.json", "r").read()
    if not raw:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON secret.")
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def clean_cell(x):
    try:
        if x is None:
            return ""
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return ""
        if pd.isna(x):
            return ""
        return x
    except Exception:
        return x


def clean_rows(rows):
    return [[clean_cell(c) for c in row] for row in rows]


def safe_float(x, default=0.0):
    try:
        if x in [None, "", "-", "--", "---"]:
            return default
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def team_abbrev(team_obj):
    if not team_obj:
        return ""
    return team_obj.get("abbreviation") or team_obj.get("fileCode") or TEAM_FIX.get(team_obj.get("name", ""), team_obj.get("name", ""))


def get_or_create_ws(spreadsheet, title, rows=100, cols=30):
    try:
        return spreadsheet.worksheet(title)
    except Exception:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def angle_diff(a, b):
    return abs((float(a) - float(b) + 180) % 360 - 180)


def wind_impact(wind_from_deg, wind_mph, center_field_deg, dome):
    if dome:
        return ("Dome", 0)
    if wind_from_deg in [None, ""] or wind_mph in [None, ""]:
        return ("Unknown", 0)
    wind_from = safe_float(wind_from_deg, default=0)
    mph = safe_float(wind_mph, default=0)
    blowing_to = (wind_from + 180) % 360
    diff_to_cf = angle_diff(blowing_to, center_field_deg)
    if mph < 5:
        return ("Calm", 0)
    if diff_to_cf <= 35:
        return ("Out", min(16, 4 + mph * 0.8))
    if diff_to_cf <= 70:
        return ("Cross/Out", min(8, 2 + mph * 0.35))
    if diff_to_cf >= 145:
        return ("In", -min(14, 3 + mph * 0.7))
    if diff_to_cf >= 110:
        return ("Cross/In", -min(7, 1 + mph * 0.35))
    return ("Cross", 0)


def weather_score_from_values(temp_f, humidity, wind_boost, dome):
    if dome:
        return 50
    score = 50
    if temp_f >= 90:
        score += 15
    elif temp_f >= 80:
        score += 10
    elif temp_f >= 70:
        score += 5
    elif temp_f < 55:
        score -= 10
    if humidity >= 65:
        score += 3
    score += wind_boost
    return max(0, min(100, round(score, 1)))


def get_weather_for_venue(venue):
    st = STADIUMS.get(venue)
    if not st:
        return {"TempF":None,"Humidity":None,"WindMPH":None,"WindFromDir":None,"WindImpact":"Unknown","WindBoost":0,"Dome":False,"WeatherScore":50}
    if st["dome"]:
        return {"TempF":72,"Humidity":50,"WindMPH":0,"WindFromDir":"","WindImpact":"Dome","WindBoost":0,"Dome":True,"WeatherScore":50}
    try:
        w = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude":st["lat"], "longitude":st["lon"],
                "current":"temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m",
                "temperature_unit":"fahrenheit", "wind_speed_unit":"mph",
            },
            timeout=20
        ).json()
        cur = w.get("current", {})
        temp = safe_float(cur.get("temperature_2m"), default=70)
        hum = safe_float(cur.get("relative_humidity_2m"), default=50)
        wind = safe_float(cur.get("wind_speed_10m"), default=0)
        wd_from = safe_float(cur.get("wind_direction_10m"), default=0)
        impact, boost = wind_impact(wd_from, wind, st["cf"], False)
        return {"TempF":temp,"Humidity":hum,"WindMPH":wind,"WindFromDir":wd_from,"WindImpact":impact,"WindBoost":round(boost,1),"Dome":False,"WeatherScore":weather_score_from_values(temp, hum, boost, False)}
    except Exception:
        return {"TempF":None,"Humidity":None,"WindMPH":None,"WindFromDir":None,"WindImpact":"Weather API Error","WindBoost":0,"Dome":False,"WeatherScore":50}


def park_factor(venue):
    return PARK_FACTORS.get(venue, 100)


def get_pitcher_stats(pid):
    if not pid:
        return {"ERA":4.50,"WHIP":1.30,"K9":8.0,"PitcherQuality":50}
    try:
        data = requests.get(
            f"https://statsapi.mlb.com/api/v1/people/{int(pid)}/stats",
            params={"stats":"season","group":"pitching","season":YEAR},
            timeout=20
        ).json()
        splits = data.get("stats", [{}])[0].get("splits", [])
        st = splits[0]["stat"] if splits else {}
        era = safe_float(st.get("era"), 4.50)
        whip = safe_float(st.get("whip"), 1.30)
        k9 = safe_float(st.get("strikeoutsPer9Inn"), 8.0)
        quality = 50
        quality += max(-24, min(24, (4.50 - era) * 8))
        quality += max(-22, min(22, (1.30 - whip) * 38))
        quality += max(-12, min(12, (k9 - 8.0) * 3))
        return {"ERA":era,"WHIP":whip,"K9":k9,"PitcherQuality":round(max(0,min(100,quality)),1)}
    except Exception:
        return {"ERA":4.50,"WHIP":1.30,"K9":8.0,"PitcherQuality":50}


def get_team_stats():
    teams = {}
    try:
        standings = requests.get(
            "https://statsapi.mlb.com/api/v1/standings",
            params={"leagueId":"103,104","season":YEAR,"standingsTypes":"regularSeason"},
            timeout=30
        ).json()
        for rec in standings.get("records", []):
            for tr in rec.get("teamRecords", []):
                team = tr.get("team", {})
                tid = team.get("id")
                if tid:
                    teams[int(tid)] = {
                        "TeamName": team.get("name",""), "Wins": int(tr.get("wins",0)),
                        "Losses": int(tr.get("losses",0)), "WinPct": safe_float(tr.get("winningPercentage"), 0.500),
                        "Streak": tr.get("streak",{}).get("streakCode","")
                    }
    except Exception as e:
        print(f"Standings fetch failed: {e}")
    try:
        hitting = requests.get("https://statsapi.mlb.com/api/v1/teams/stats", params={"group":"hitting","stats":"season","season":YEAR,"sportIds":1}, timeout=30).json()
        for s in hitting.get("stats", [{}])[0].get("splits", []):
            tid = int(s.get("team",{}).get("id",0) or 0)
            stat = s.get("stat",{})
            if tid:
                teams.setdefault(tid, {})
                games = safe_float(stat.get("gamesPlayed"), 1)
                runs = safe_float(stat.get("runs"), 0)
                teams[tid].update({"RunsPerGame": round(runs / games, 2) if games else 0, "TeamHR": int(safe_float(stat.get("homeRuns"),0)), "OPS": safe_float(stat.get("ops"),0)})
    except Exception as e:
        print(f"Team hitting fetch failed: {e}")
    try:
        pitching = requests.get("https://statsapi.mlb.com/api/v1/teams/stats", params={"group":"pitching","stats":"season","season":YEAR,"sportIds":1}, timeout=30).json()
        for s in pitching.get("stats", [{}])[0].get("splits", []):
            tid = int(s.get("team",{}).get("id",0) or 0)
            stat = s.get("stat",{})
            if tid:
                teams.setdefault(tid, {})
                teams[tid].update({"TeamERA": safe_float(stat.get("era"), 4.50), "TeamWHIP": safe_float(stat.get("whip"), 1.30)})
    except Exception as e:
        print(f"Team pitching fetch failed: {e}")
    return teams


def team_score(team_id, team_stats, pitcher_quality, is_home, env_boost):
    st = team_stats.get(int(team_id), {})
    win_pct = safe_float(st.get("WinPct"), 0.500)
    rpg = safe_float(st.get("RunsPerGame"), 4.3)
    ops = safe_float(st.get("OPS"), 0.700)
    team_era = safe_float(st.get("TeamERA"), 4.50)
    team_whip = safe_float(st.get("TeamWHIP"), 1.30)
    score = 50
    score += (win_pct - 0.500) * 55
    score += (rpg - 4.3) * 5.5
    score += (ops - 0.700) * 70
    score += (4.50 - team_era) * 2.8
    score += (1.30 - team_whip) * 9
    score += (pitcher_quality - 50) * 0.60
    if is_home:
        score += 3.5
    score += env_boost
    return round(max(0, min(100, score)), 1)


def win_probability_from_edge(edge):
    # Conservative logistic curve: edge 0 = 50%, edge 20 ~= 73%, edge 40 ~= 88%.
    e = max(-50, min(50, float(edge)))
    prob = 1 / (1 + math.exp(-e / 16.0))
    return round(prob * 100, 1)


def confidence_from_prob(prob):
    p = float(prob)
    if p >= 78:
        return "★★★★★ Elite Pick"
    if p >= 68:
        return "★★★★ Strong Pick"
    if p >= 60:
        return "★★★ Solid Pick"
    if p >= 55:
        return "★★ Lean"
    return "★ Pass"


def margin_from_edge(edge):
    e = abs(float(edge))
    if e >= 20:
        return "Projected 2+ run edge"
    if e >= 12:
        return "Projected 1-2 run edge"
    if e >= 6:
        return "Small projected edge"
    return "No clear run-margin edge"


def projected_team_runs(team_model_score, opponent_pitcher_quality, weather_score, park):
    # Stats-only run estimate. Not meant as exact betting total; used for relative game context.
    runs = 4.35
    runs += (team_model_score - 50) * 0.055
    runs += (50 - opponent_pitcher_quality) * 0.035
    runs += (safe_float(weather_score, 50) - 50) * 0.025
    runs += (safe_float(park, 100) - 100) * 0.035
    return round(max(1.5, min(9.5, runs)), 1)


def is_playable_game_status(status):
    """Return True only for official, playable games that can be evaluated pregame/in-game/final.

    The official MLB schedule is the only source of truth. We explicitly reject postponed,
    cancelled, suspended, delayed-start placeholders, and other non-playable records.
    """
    if not status:
        return False
    st = str(status).strip().lower()
    blocked_terms = [
        "postponed", "cancelled", "canceled", "suspended", "delayed",
        "completion early", "game over, suspended", "forfeit"
    ]
    if any(term in st for term in blocked_terms):
        return False
    allowed = {
        "scheduled", "pre-game", "warmup"
    }
    return st in allowed


def verification_failure_reason(game_record):
    reasons = []
    if not game_record.get("GamePk"):
        reasons.append("Missing gamePk")
    if not game_record.get("AwayTeamID"):
        reasons.append("Missing away team ID")
    if not game_record.get("HomeTeamID"):
        reasons.append("Missing home team ID")
    if not game_record.get("Venue"):
        reasons.append("Missing venue")
    status = game_record.get("GameStatus")
    if not is_playable_game_status(status):
        reasons.append(f"Non-playable status: {status or 'Unknown'}")
    if not game_record.get("AwayPitcherID") or game_record.get("AwayPitcher") in [None, "", "Unknown"]:
        reasons.append("Missing away probable pitcher")
    if not game_record.get("HomePitcherID") or game_record.get("HomePitcher") in [None, "", "Unknown"]:
        reasons.append("Missing home probable pitcher")
    if not game_record.get("WeatherSourceStatus") or str(game_record.get("WeatherSourceStatus", "")).startswith("Missing"):
        reasons.append("Weather not tied to verified venue")
    return "; ".join(reasons)


def get_weather_for_verified_venue(venue):
    weather = get_weather_for_venue(venue)
    if not venue:
        weather["WeatherSourceStatus"] = "Missing venue"
    elif venue not in STADIUMS:
        # Keep neutral weather for unknown stadiums, but do not treat it as verified.
        weather["WeatherSourceStatus"] = "Missing stadium coordinates"
    elif weather.get("WindImpact") == "Weather API Error":
        # Weather failed but the record is still tied to the official venue; use neutral score.
        weather["WeatherSourceStatus"] = "Venue verified; weather API fallback"
    elif weather.get("Dome"):
        weather["WeatherSourceStatus"] = "Venue verified; dome conditions"
    else:
        weather["WeatherSourceStatus"] = "Venue verified; live weather"
    return weather


def build_schedule_games():
    """Build one record per official MLB game for TODAY.

    Only verified games are returned for scoring. All official schedule rows, including
    excluded rows, are returned in the integrity DataFrame. The model never creates
    matchups from standings, prior sheets, cached rows, or guessed opponents.
    """
    schedule_date = TODAY.isoformat()
    url = "https://statsapi.mlb.com/api/v1/schedule"
    sched = requests.get(
        url,
        params={
            "sportId": 1,
            "date": schedule_date,
            "hydrate": "probablePitcher,team,venue",
        },
        timeout=30,
    ).json()

    verified_games = []
    integrity_rows = []
    seen_gamepks = set()

    for day in sched.get("dates", []):
        for g in day.get("games", []):
            game_pk = g.get("gamePk", "")
            if game_pk in seen_gamepks:
                continue
            seen_gamepks.add(game_pk)

            away_block = g.get("teams", {}).get("away", {}) or {}
            home_block = g.get("teams", {}).get("home", {}) or {}
            away_team = away_block.get("team", {}) or {}
            home_team = home_block.get("team", {}) or {}
            away_p = away_block.get("probablePitcher", {}) or {}
            home_p = home_block.get("probablePitcher", {}) or {}
            venue_obj = g.get("venue", {}) or {}
            venue = venue_obj.get("name", "")
            status = (g.get("status", {}) or {}).get("detailedState", "")
            weather = get_weather_for_verified_venue(venue)

            record = {
                "ScheduleDateUsed": schedule_date,
                "ScheduleDateReason": SCHEDULE_DATE_REASON,
                "GamePk": game_pk,
                "GameDateUTC": g.get("gameDate", ""),
                "GameStatus": status,
                "Venue": venue,
                "VenueID": venue_obj.get("id", ""),
                "ParkFactor": park_factor(venue),
                "AwayTeam": team_abbrev(away_team),
                "AwayTeamName": away_team.get("name", ""),
                "AwayTeamID": away_team.get("id"),
                "HomeTeam": team_abbrev(home_team),
                "HomeTeamName": home_team.get("name", ""),
                "HomeTeamID": home_team.get("id"),
                "AwayPitcher": away_p.get("fullName", "Unknown"),
                "AwayPitcherID": away_p.get("id"),
                "HomePitcher": home_p.get("fullName", "Unknown"),
                "HomePitcherID": home_p.get("id"),
                **weather,
            }

            fail_reason = verification_failure_reason(record)
            verified = fail_reason == ""
            integrity = {
                "Date Used": schedule_date,
                "Date Logic": SCHEDULE_DATE_REASON,
                "gamePk": game_pk,
                "Away Team": record["AwayTeam"],
                "Away Team Name": record["AwayTeamName"],
                "Away Team ID": record["AwayTeamID"],
                "Home Team": record["HomeTeam"],
                "Home Team Name": record["HomeTeamName"],
                "Home Team ID": record["HomeTeamID"],
                "Venue": venue,
                "Venue ID": record["VenueID"],
                "Game Status": status,
                "Away Probable Pitcher": record["AwayPitcher"],
                "Away Pitcher ID": record["AwayPitcherID"],
                "Home Probable Pitcher": record["HomePitcher"],
                "Home Pitcher ID": record["HomePitcherID"],
                "Verified": "Yes" if verified else "No",
                "Verification Failure Reason": fail_reason,
                "Weather Source/Status": record.get("WeatherSourceStatus", ""),
                "WeatherScore": record.get("WeatherScore", ""),
                "WindImpact": record.get("WindImpact", ""),
                "TempF": record.get("TempF", ""),
                "ParkFactor": record.get("ParkFactor", ""),
            }
            integrity_rows.append(integrity)

            if verified:
                record["Verified"] = "Yes"
                record["VerificationFailureReason"] = ""
                verified_games.append(record)

    integrity_df = pd.DataFrame(integrity_rows)
    games_df = pd.DataFrame(verified_games)

    if integrity_df.empty:
        raise RuntimeError(f"Official MLB schedule returned no games for {schedule_date}.")
    if games_df.empty:
        print(f"No fully verified MLB games found for {schedule_date}. See Game Schedule Integrity tab.")

    return games_df, integrity_df

def environment_edge(row):
    wx = safe_float(row.get("WeatherScore"), 50)
    park = safe_float(row.get("ParkFactor"), 100)
    wind = str(row.get("WindImpact", ""))
    boost = 0
    if wx >= 65: boost += 1.5
    if wx <= 45: boost -= 1.0
    if park >= 105: boost += 1.0
    if park <= 95: boost -= 1.0
    if wind in ["Out","Cross/Out"]: boost += 1.0
    if wind in ["In","Cross/In"]: boost -= 1.0
    return boost


def build_why(row):
    pieces = []
    pick = row.get("Projected Winner Name", row.get("Projected Winner", ""))
    opp = row.get("Opponent", "")
    pitcher_edge = safe_float(row.get("Pitcher Edge"), 0)
    if pitcher_edge >= 12:
        pieces.append(f"{pick} owns a major starting pitching advantage.")
    elif pitcher_edge >= 6:
        pieces.append(f"{pick} has the better starting pitcher profile today.")
    elif pitcher_edge <= -6:
        pieces.append(f"{pick} is supported more by team strength than the starting pitching matchup.")
    else:
        pieces.append("The starting pitching matchup is relatively balanced.")
    if safe_float(row.get("Win Probability", 50), 50) >= 70:
        pieces.append("The overall model profile creates one of the stronger projected winner spots on the slate.")
    elif safe_float(row.get("Win Probability", 50), 50) >= 60:
        pieces.append("The model sees a solid statistical edge, but not a runaway advantage.")
    else:
        pieces.append("This projects closer to a lean than a high-confidence selection.")
    wind = str(row.get("WindImpact", ""))
    venue = row.get("Venue", "")
    temp = safe_float(row.get("TempF"), 0)
    if wind in ["Out", "Cross/Out"]:
        pieces.append(f"Run environment gets a boost with wind {wind.lower()} at {venue}.")
    elif wind in ["In", "Cross/In"]:
        pieces.append(f"Weather may suppress scoring with wind {wind.lower()} at {venue}.")
    elif wind == "Dome":
        pieces.append(f"{venue} provides controlled dome conditions, reducing weather volatility.")
    else:
        pieces.append(f"Weather at {venue} grades close to neutral.")
    return " ".join(pieces)


def build_game_model():
    games, integrity = build_schedule_games()
    team_stats = get_team_stats()
    rows, debug = [], []

    if games.empty:
        return pd.DataFrame(rows), pd.DataFrame(debug), integrity

    for _, g in games.iterrows():
        away_pitch = get_pitcher_stats(g["AwayPitcherID"])
        home_pitch = get_pitcher_stats(g["HomePitcherID"])
        env = environment_edge(g)
        away_score = team_score(g["AwayTeamID"], team_stats, away_pitch["PitcherQuality"], False, env)
        home_score = team_score(g["HomeTeamID"], team_stats, home_pitch["PitcherQuality"], True, env)
        away_runs = projected_team_runs(away_score, home_pitch["PitcherQuality"], g["WeatherScore"], g["ParkFactor"])
        home_runs = projected_team_runs(home_score, away_pitch["PitcherQuality"], g["WeatherScore"], g["ParkFactor"])
        if home_score >= away_score:
            pick_team, pick_name, fade_team = g["HomeTeam"], g["HomeTeamName"], g["AwayTeam"]
            edge = round(home_score - away_score, 1)
            pitcher_edge = round(home_pitch["PitcherQuality"] - away_pitch["PitcherQuality"], 1)
            expected_margin = round(home_runs - away_runs, 1)
        else:
            pick_team, pick_name, fade_team = g["AwayTeam"], g["AwayTeamName"], g["HomeTeam"]
            edge = round(away_score - home_score, 1)
            pitcher_edge = round(away_pitch["PitcherQuality"] - home_pitch["PitcherQuality"], 1)
            expected_margin = round(away_runs - home_runs, 1)
        win_prob = win_probability_from_edge(edge)
        row = {
            "Date": TODAY.isoformat(),
            "Model Version": MODEL_VERSION,
            "GamePk": g["GamePk"],
            "Game": f"{g['AwayTeam']} @ {g['HomeTeam']}",
            "Venue": g["Venue"],
            "Projected Winner": pick_team,
            "Projected Winner Name": pick_name,
            "Opponent": fade_team,
            "Confidence": confidence_from_prob(win_prob),
            "Win Probability": win_prob,
            "Model Edge": edge,
            "Run Margin Lean": margin_from_edge(edge),
            "Expected Margin": expected_margin,
            "Away Projected Runs": away_runs,
            "Home Projected Runs": home_runs,
            "Away Score": away_score,
            "Home Score": home_score,
            "Away Team": g["AwayTeam"],
            "Home Team": g["HomeTeam"],
            "Away Pitcher": g["AwayPitcher"],
            "Home Pitcher": g["HomePitcher"],
            "Away Pitcher Quality": away_pitch["PitcherQuality"],
            "Home Pitcher Quality": home_pitch["PitcherQuality"],
            "Pitcher Edge": pitcher_edge,
            "WeatherScore": g["WeatherScore"],
            "WindImpact": g["WindImpact"],
            "TempF": g["TempF"],
            "ParkFactor": g["ParkFactor"],
            "Weather Source/Status": g.get("WeatherSourceStatus", ""),
            "Verified": "Yes",
        }
        row["Why"] = build_why(row)
        rows.append(row)
        debug.append({
            **g.to_dict(),
            "ScheduleSource": "MLB Stats API official schedule",
            "ScheduleDateUsed": TODAY.isoformat(),
            "AwayERA": away_pitch["ERA"],
            "AwayWHIP": away_pitch["WHIP"],
            "AwayK9": away_pitch["K9"],
            "HomeERA": home_pitch["ERA"],
            "HomeWHIP": home_pitch["WHIP"],
            "HomeK9": home_pitch["K9"],
            "AwayPitcherQuality": away_pitch["PitcherQuality"],
            "HomePitcherQuality": home_pitch["PitcherQuality"],
            "AwayModelScore": away_score,
            "HomeModelScore": home_score,
            "AwayProjectedRuns": away_runs,
            "HomeProjectedRuns": home_runs,
        })
    picks = pd.DataFrame(rows).sort_values("Win Probability", ascending=False).reset_index(drop=True)
    if not picks.empty:
        picks["Rank"] = picks.index + 1
    return picks, pd.DataFrame(debug), integrity

def build_email_rows(picks):
    if picks.empty:
        return clean_rows([
            ["Daily MLB Game Picks - Stats Only"],
            ["Last Updated", now_in_mlb_timezone().strftime("%Y-%m-%d %H:%M:%S %Z")],
            ["Model Version", MODEL_VERSION],
            [],
            ["Daily Outlook"],
            ["Verified Games Evaluated", 0],
            ["Status", "No fully verified playable games with confirmed probable pitchers were available from today's official MLB schedule."],
            ["Note", "Stats-only model. No sportsbook odds, betting edge, or lines used."],
        ])

    top = picks.iloc[0]
    high_score = picks.copy()
    high_score["Total Runs"] = high_score["Away Projected Runs"] + high_score["Home Projected Runs"]
    highest_total = high_score.sort_values("Total Runs", ascending=False).iloc[0]
    rows = [
        ["Daily MLB Game Picks - Stats Only"],
        ["Last Updated", now_in_mlb_timezone().strftime("%Y-%m-%d %H:%M:%S %Z")],
        ["Model Version", MODEL_VERSION],
        [],
        ["Daily Outlook"],
        ["Verified Games Evaluated", len(picks)],
        ["Schedule Source", "MLB Stats API official schedule only"],
        ["Schedule Date Used", TODAY.isoformat()],
        ["Schedule Date Logic", SCHEDULE_DATE_REASON],
        ["Best Overall Pick", f"{top['Projected Winner']} over {top['Opponent']} - {top['Confidence']} ({top['Win Probability']}%)"],
        ["Best Run Margin Lean", f"{top['Projected Winner']} - {top['Run Margin Lean']} | Expected Margin {top['Expected Margin']}"],
        ["Highest Projected Scoring Game", f"{highest_total['Game']} - {highest_total['Total Runs']:.1f} projected runs"],
        ["Note", "Stats-only model. No sportsbook odds, betting edge, or lines used."],
        [],
        ["Top Game Picks"]
    ]
    for _, r in picks.head(7).iterrows():
        rows.append([f"{int(r['Rank'])}. {r['Confidence']}", f"{r['Projected Winner']} over {r['Opponent']} | {r['Game']} | {r['Venue']}"])
        rows.append(["Win Probability", f"{r['Win Probability']}%"] )
        rows.append(["Projected Score", f"{r['Away Team']} {r['Away Projected Runs']} - {r['Home Team']} {r['Home Projected Runs']}"])
        rows.append(["Expected Margin", f"{r['Expected Margin']} runs"])
        rows.append(["Run Margin Lean", r["Run Margin Lean"]])
        rows.append(["Why Today", r["Why"]])
        rows.append(["Pitchers", f"{r['Away Team']}: {r['Away Pitcher']} | {r['Home Team']}: {r['Home Pitcher']}"])
        rows.append(["Environment", f"{safe_float(r['TempF'],0):.1f}°F | Wind {r['WindImpact']} | WeatherScore {r['WeatherScore']} | ParkFactor {r['ParkFactor']}"])
        rows.append(["Verification", "✓ Official Schedule Verified | ✓ gamePk Verified | ✓ Teams Verified | ✓ Pitchers Verified | ✓ Venue Verified | ✓ Weather Tied to Venue"])
        rows.append([])
    rows.extend([
        ["Model Notes"],
        ["Ranking Basis", "Starting pitcher quality, team offense, team pitching profile, win profile, home field, park and weather."],
        ["Schedule Integrity", "Only games from today's official MLB schedule with verified teams, venue, status, probable pitchers, and venue-tied weather are scored."],
        ["Odds/Betting", "Removed. This model uses stats only."],
        ["Results Tracking", "Game Result can be entered manually after games finish."],
    ])
    return clean_rows(rows)

def write_to_sheet(picks, debug, integrity):
    gc = auth_google()
    sh = gc.open_by_key(SHEET_ID)
    game_ws = get_or_create_ws(sh, "Game Picks", 100, 45)
    debug_ws = get_or_create_ws(sh, "Game Model Debug", 1000, 75)
    integrity_ws = get_or_create_ws(sh, "Game Schedule Integrity", 1000, 35)
    results_ws = get_or_create_ws(sh, "Game Results", 1000, 20)
    email_ws = get_or_create_ws(sh, "Game Email Summary", 150, 10)

    game_cols = [
        "Date","Model Version","Rank","GamePk","Game","Venue","Projected Winner","Projected Winner Name",
        "Opponent","Confidence","Win Probability","Model Edge","Run Margin Lean","Expected Margin",
        "Away Projected Runs","Home Projected Runs","Away Score","Home Score","Away Team","Home Team",
        "Away Pitcher","Home Pitcher","Away Pitcher Quality","Home Pitcher Quality","Pitcher Edge",
        "WeatherScore","WindImpact","TempF","ParkFactor","Weather Source/Status","Why","Verified","Game Result"
    ]
    game_rows = []
    if not picks.empty:
        for _, r in picks.iterrows():
            game_rows.append([r.get(c,"") for c in game_cols[:-1]] + [""])
    game_ws.clear()
    game_ws.update(values=clean_rows([game_cols] + game_rows), range_name=f"A1:AG{len(game_rows)+1}")

    debug_ws.clear()
    if debug is not None and not debug.empty:
        debug_cols = list(debug.columns)
        debug_rows = debug[debug_cols].fillna("").values.tolist()
        debug_ws.update(values=clean_rows([debug_cols] + debug_rows), range_name=f"A1:BZ{len(debug_rows)+1}")
    else:
        debug_ws.update(values=[["No verified games scored", TODAY.isoformat(), MODEL_VERSION]], range_name="A1:C1")

    integrity_ws.clear()
    if integrity is not None and not integrity.empty:
        integrity_cols = list(integrity.columns)
        integrity_rows = integrity[integrity_cols].fillna("").values.tolist()
        integrity_ws.update(values=clean_rows([integrity_cols] + integrity_rows), range_name=f"A1:AI{len(integrity_rows)+1}")
    else:
        integrity_ws.update(values=[["No official schedule rows returned", TODAY.isoformat(), MODEL_VERSION]], range_name="A1:C1")

    results_headers = ["Date","Game","Projected Winner","Confidence","Win Probability","Expected Margin","Actual Winner","Correct?","Final Score","Notes"]
    if not results_ws.get_all_values():
        results_ws.update(values=[results_headers], range_name="A1:J1")

    email_rows = build_email_rows(picks)
    email_ws.clear()
    email_ws.update(values=email_rows, range_name=f"A1:B{len(email_rows)}")
    print(f"Updated Google Sheet: {SHEET_NAME}")
    print(f"Verified Game Picks written: {len(picks)}")
    print(f"Official schedule rows logged: {0 if integrity is None else len(integrity)}")

def main():
    print(f"Starting {MODEL_VERSION}")
    picks, debug, integrity = build_game_model()
    for attempt in range(3):
        try:
            write_to_sheet(picks, debug, integrity)
            break
        except gspread.exceptions.APIError as exc:
            if "429" not in str(exc) or attempt == 2:
                raise
            wait_seconds = 65 * (attempt + 1)
            print(f"Google Sheets write quota reached; retrying in {wait_seconds} seconds.")
            time.sleep(wait_seconds)
    print("Top Game Picks")
    if picks.empty:
        print("- No fully verified games available for scoring.")
    else:
        for _, r in picks.head(7).iterrows():
            print(f"- {r['Projected Winner']} over {r['Opponent']} | {r['Confidence']} | Win Prob {r['Win Probability']}% | {r['Run Margin Lean']}")


if __name__ == "__main__":
    main()

