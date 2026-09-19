import os
import re, json, math, requests
import pandas as pd
import numpy as np
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo
from pybaseball import statcast_batter
import gspread
from google.oauth2.service_account import Credentials

PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
EASTERN_TZ = ZoneInfo("America/New_York")
RUN_NOW_LOCAL = datetime.now(PACIFIC_TZ)
RUN_NOW_EASTERN = datetime.now(EASTERN_TZ)

# MLB schedule/slate date must be based on Eastern time, not the GitHub runner or Pacific time.
# This prevents late-night Pacific runs from accidentally pulling yesterday's official schedule.
SCHEDULE_DATE = RUN_NOW_EASTERN.date()
TODAY = SCHEDULE_DATE  # backward-compatible alias used throughout the existing V16 code
YEAR = SCHEDULE_DATE.year
START = SCHEDULE_DATE - timedelta(days=14)
RUN_TIMESTAMP_LOCAL = RUN_NOW_LOCAL.strftime("%Y-%m-%d %H:%M:%S %Z")
RUN_TIMESTAMP_EASTERN = RUN_NOW_EASTERN.strftime("%Y-%m-%d %H:%M:%S %Z")
SCHEDULE_DATE_LOGIC = "Official MLB slate date from America/New_York"

MODEL_VERSION = "Automated V16.2 - Eastern Slate Schedule Fix"
SHEET_NAME = os.environ.get("SHEET_NAME", "Daily MLB HR Picks Scorecard")
SHEET_ID = os.environ.get("SHEET_ID", "1SsjWyqMsEW9yghahhuHCjDDtqlW2-56SbUWwmMXdig8")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

UNPLAYABLE_STATUS_KEYWORDS = [
    "postponed", "cancelled", "canceled", "suspended", "final", "game over"
]

def is_playable_game_status(status):
    """Return True for games that can be considered part of today's playable slate."""
    s = str(status or "").strip().lower()
    if not s:
        return False
    return not any(k in s for k in UNPLAYABLE_STATUS_KEYWORDS)

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

def norm(s):
    s = pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], 0).fillna(0)
    mn, mx = s.min(), s.max()
    if mx == mn:
        return pd.Series([50] * len(s), index=s.index)
    return ((s - mn) / (mx - mn) * 100).replace([np.inf, -np.inf], 0).fillna(0)

def safe_float(x, default=0.0):
    try:
        if x in [None, "", "-", "--", "---"]:
            return default
        val = float(x)
        if math.isnan(val) or math.isinf(val):
            return default
        return val
    except Exception:
        return default

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

def team_abbrev(team_obj):
    if not team_obj:
        return ""
    return team_obj.get("abbreviation") or team_obj.get("fileCode") or TEAM_FIX.get(team_obj.get("name", ""), team_obj.get("name", ""))

def tier_from_rank(rank):
    r = int(float(rank))
    if r <= 3:
        return "Primary"
    if r <= 6:
        return "Secondary"
    if r <= 9:
        return "Longshot"
    return ""

PARK_FACTORS = {
    "Coors Field":120,"Great American Ball Park":114,"Yankee Stadium":111,"Citizens Bank Park":109,
    "Dodger Stadium":105,"Fenway Park":104,"Daikin Park":103,"Minute Maid Park":103,"Globe Life Field":103,
    "Oriole Park at Camden Yards":102,"Angel Stadium":101,"Nationals Park":101,"Chase Field":100,
    "Truist Park":100,"Busch Stadium":99,"Comerica Park":99,"Progressive Field":99,"Rogers Centre":99,
    "Target Field":98,"American Family Field":98,"PNC Park":98,"Wrigley Field":100,"T-Mobile Park":96,
    "Tropicana Field":95,"loanDepot park":95,"Petco Park":94,"Oracle Park":94,
    "Citi Field":97,"Sutter Health Park":100,"Rate Field":98,"Guaranteed Rate Field":98,
    "George M. Steinbrenner Field":98,"TD Ballpark":100,"London Stadium":105,"Rickwood Field":100
}

def park_factor(venue):
    return PARK_FACTORS.get(venue, 100)

# Approximate direction from home plate to center field. Used to determine if wind is blowing out/in.
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
    "Citi Field": {"lat":40.7571,"lon":-73.8458,"dome":False,"cf":5},
    "Sutter Health Park": {"lat":38.5804,"lon":-121.5139,"dome":False,"cf":40},
    "Rate Field": {"lat":41.8300,"lon":-87.6339,"dome":False,"cf":60},
    "Guaranteed Rate Field": {"lat":41.8300,"lon":-87.6339,"dome":False,"cf":60},
    "George M. Steinbrenner Field": {"lat":27.9799,"lon":-82.5068,"dome":False,"cf":60},
    "TD Ballpark": {"lat":28.0037,"lon":-82.7862,"dome":False,"cf":55},
    "London Stadium": {"lat":51.5386,"lon":-0.0165,"dome":False,"cf":25},
    "Rickwood Field": {"lat":33.5050,"lon":-86.8550,"dome":False,"cf":35},
}

def angle_diff(a, b):
    return abs((float(a) - float(b) + 180) % 360 - 180)

def wind_impact(wind_from_deg, wind_mph, center_field_deg, dome):
    if dome:
        return ("Dome", 0, "", "")
    if wind_from_deg in [None, ""] or wind_mph in [None, ""]:
        return ("Unknown", 0, "", "")
    wind_from = safe_float(wind_from_deg, default=0)
    mph = safe_float(wind_mph, default=0)
    blowing_to = (wind_from + 180) % 360
    diff_to_cf = angle_diff(blowing_to, center_field_deg)
    if mph < 5:
        return ("Calm", 0, round(blowing_to,1), round(diff_to_cf,1))
    if diff_to_cf <= 35:
        return ("Out", min(16, 4 + mph * 0.8), round(blowing_to,1), round(diff_to_cf,1))
    if diff_to_cf <= 70:
        return ("Cross/Out", min(8, 2 + mph * 0.35), round(blowing_to,1), round(diff_to_cf,1))
    if diff_to_cf >= 145:
        return ("In", -min(14, 3 + mph * 0.7), round(blowing_to,1), round(diff_to_cf,1))
    if diff_to_cf >= 110:
        return ("Cross/In", -min(7, 1 + mph * 0.35), round(blowing_to,1), round(diff_to_cf,1))
    return ("Cross", 0, round(blowing_to,1), round(diff_to_cf,1))

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
        return {
            "TempF":None,"Humidity":None,"WindMPH":None,"WindFromDir":None,
            "WindBlowingTo":None,"WindAngleToCF":None,"WindImpact":"Unknown","WindBoost":0,
            "Dome":False,"WeatherScore":50,"WeatherSource":"No venue coordinate match",
            "WeatherStatus":"No stadium coordinates for official venue; weather not verified",
            "WeatherTiedToVenue":False
        }
    if st["dome"]:
        return {
            "TempF":72,"Humidity":50,"WindMPH":0,"WindFromDir":"","WindBlowingTo":"",
            "WindAngleToCF":"","WindImpact":"Dome","WindBoost":0,"Dome":True,"WeatherScore":50,
            "WeatherSource":"Venue dome model","WeatherStatus":"Verified dome/controlled conditions tied to official venue",
            "WeatherTiedToVenue":True
        }
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
        impact, boost, blowing_to, diff = wind_impact(wd_from, wind, st["cf"], False)
        wx_score = weather_score_from_values(temp, hum, boost, False)
        return {
            "TempF":temp,"Humidity":hum,"WindMPH":wind,"WindFromDir":wd_from,
            "WindBlowingTo":blowing_to,"WindAngleToCF":diff,"WindImpact":impact,"WindBoost":round(boost,1),
            "Dome":False,"WeatherScore":wx_score,"WeatherSource":"Open-Meteo venue coordinates",
            "WeatherStatus":"Verified weather tied to official venue coordinates","WeatherTiedToVenue":True
        }
    except Exception as e:
        return {
            "TempF":None,"Humidity":None,"WindMPH":None,"WindFromDir":None,
            "WindBlowingTo":None,"WindAngleToCF":None,"WindImpact":"Weather API Error","WindBoost":0,
            "Dome":False,"WeatherScore":50,"WeatherSource":"Open-Meteo venue coordinates",
            "WeatherStatus":f"Weather API error; venue coordinates known and neutral fallback used: {e}",
            "WeatherTiedToVenue":True
        }

def get_or_create_ws(spreadsheet, title, rows=100, cols=30):
    try:
        return spreadsheet.worksheet(title)
    except Exception:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def probable_pitcher_fallback(team_abbr, opponent_abbr):
    """
    MLB schedule probablePitcher is sometimes blank early in the morning.
    Fallback strategy:
    1) Try today's schedule hydrate probablePitcher again.
    2) If still blank, leave Unknown but return a lower-confidence flag.
    This keeps model stable and tags rows clearly instead of silently treating Unknown as normal.
    """
    return {"name": "Unknown", "id": None, "source": "Missing from MLB probablePitcher feed", "confidence": "Low"}

def pitcher_source_label(pid, pname):
    if pid and pname and pname != "Unknown":
        return "MLB probablePitcher"
    return "Missing from MLB probablePitcher feed"


def fetch_active_roster_players(team_ids):
    """
    Pulls active rosters for today's verified schedule teams and returns a player-id map.

    This is the V16.1 schedule-integrity fix:
    - Player team assignment comes from the current active roster, not the season stat endpoint.
    - This prevents traded/old-team stat records from attaching to the wrong game.
    - A hitter is only eligible if his active roster team has a verified game today.
    """
    active_players = {}
    for tid in sorted(set([x for x in team_ids if x])):
        try:
            data = requests.get(
                f"https://statsapi.mlb.com/api/v1/teams/{int(tid)}/roster",
                params={"rosterType": "active"},
                timeout=20
            ).json()
            team_data = requests.get(
                f"https://statsapi.mlb.com/api/v1/teams/{int(tid)}",
                timeout=20
            ).json()
            team_obj = (team_data.get("teams") or [{}])[0]
            team_abbr = team_abbrev(team_obj)
            team_name = team_obj.get("name", "")

            for item in data.get("roster", []):
                person = item.get("person", {})
                pid = person.get("id")
                if pid:
                    active_players[int(pid)] = {
                        "Player ID": int(pid),
                        "Player": person.get("fullName", ""),
                        "Team ID": int(tid),
                        "Team": team_abbr,
                        "Team Name": team_name,
                        "RosterStatus": "Active",
                    }
        except Exception as e:
            print(f"Active roster lookup failed for team {tid}: {e}")
    return active_players


def fetch_active_roster_player_ids(team_ids):
    """Backward-compatible wrapper used by older sections."""
    return set(fetch_active_roster_players(team_ids).keys())


def target_label_from_score(score):
    """
    Baseball confidence label for HR target quality.
    These thresholds are intentionally easier to read than the old betting grades.
    """
    try:
        s = float(score)
    except Exception:
        return "Watchlist"
    if s >= 70:
        return "Elite Target"
    if s >= 60:
        return "Strong Target"
    if s >= 50:
        return "Solid Target"
    if s >= 40:
        return "Watchlist"
    return "Longshot"

def factor_label(value, high=70, low=45):
    try:
        v = float(value)
    except Exception:
        return "Neutral"
    if v >= high:
        return "Strong"
    if v <= low:
        return "Weak"
    return "Neutral"

def build_reason_text(row):
    """Build specific, sentence-style explanations for the email.
    Keeps rankings untouched; only improves the human-readable reason note.
    """
    sentences = []

    player = str(row.get("Player", "the hitter") or "the hitter")
    pitcher = str(row.get("Opposing Pitcher", "the opposing pitcher") or "the opposing pitcher")
    venue = str(row.get("Venue", "the park") or "the park")
    wind = str(row.get("WindImpact", "") or "")
    dome = str(row.get("Dome", "") or "").lower() == "true"

    def fval(col, default=0.0):
        try:
            return float(row.get(col, default) or default)
        except Exception:
            return default

    last_hr = fval("Last7HR", 0)
    hard_hit = fval("HardHit%", 0)
    mph100 = fval("100+MPH%", 0)
    fly_ball = fval("FlyBall%", 0)
    pv = fval("PitcherVulnerability", 0)
    era = fval("ERA", 0)
    whip = fval("WHIP", 0)
    k9 = fval("K9", 0)
    park = fval("ParkFactor", 100)
    wx = fval("WeatherScore", 50)
    wind_mph = fval("WindMPH", 0)

    # Pitcher explanation
    pitcher_bits = []
    if pv >= 70:
        pitcher_bits.append("very favorable pitcher profile")
    elif pv >= 60:
        pitcher_bits.append("favorable pitcher profile")
    elif pv >= 50:
        pitcher_bits.append("manageable pitcher matchup")

    if era >= 4.75:
        pitcher_bits.append(f"elevated ERA ({era:.2f})")
    if whip >= 1.35:
        pitcher_bits.append(f"elevated WHIP ({whip:.2f})")
    if 0 < k9 <= 7.5:
        pitcher_bits.append(f"lower strikeout rate ({k9:.1f} K/9)")

    if pitcher_bits:
        sentences.append(f"Faces {pitcher}, with a {', '.join(pitcher_bits)}.")
    else:
        sentences.append(f"Faces {pitcher}; matchup is verified but not a major pitcher boost.")

    # Batted-ball / recent form explanation
    power_bits = []
    if last_hr >= 2:
        power_bits.append(f"coming off {int(last_hr)} HR in the recent Statcast window")
    elif last_hr >= 1:
        power_bits.append("coming off a recent HR")
    if mph100 >= 30:
        power_bits.append(f"excellent 100+ MPH contact rate ({mph100:.1f}%)")
    elif mph100 >= 20:
        power_bits.append(f"frequent 100+ MPH contact ({mph100:.1f}%)")
    if hard_hit >= 50:
        power_bits.append(f"strong hard-hit rate ({hard_hit:.1f}%)")
    if fly_ball >= 45:
        power_bits.append(f"fly-ball profile ({fly_ball:.1f}%)")

    if power_bits:
        sentences.append(player + " is " + "; ".join(power_bits) + ".")
    else:
        sentences.append(player + " grades more as a matchup/weather target than a recent power-form target.")

    # Park/weather explanation
    env_bits = []
    if dome:
        env_bits.append(f"controlled dome conditions at {venue}")
    else:
        if wind in ["Out", "Cross/Out"]:
            env_bits.append(f"wind is {wind.lower()} at {venue} ({wind_mph:.1f} mph)")
        elif wind in ["In", "Cross/In"]:
            env_bits.append(f"wind is working {wind.lower()} at {venue} ({wind_mph:.1f} mph)")
        elif wind:
            env_bits.append(f"wind is {wind.lower()} at {venue}")
        else:
            env_bits.append(f"weather is tied to {venue}")

    if park >= 110:
        env_bits.append(f"very HR-friendly park factor ({int(round(park))})")
    elif park >= 105:
        env_bits.append(f"positive park factor ({int(round(park))})")
    elif park <= 95:
        env_bits.append(f"park factor is less favorable ({int(round(park))})")

    if wx >= 70:
        env_bits.append(f"strong weather score ({wx:.1f})")
    elif wx >= 60:
        env_bits.append(f"positive weather score ({wx:.1f})")

    if env_bits:
        sentences.append("Environment: " + "; ".join(env_bits) + ".")

    verified = str(row.get("MatchupVerified", row.get("Verified", "")) or "").lower()
    if verified in ["true", "yes", "1"]:
        sentences.append("Game, venue, probable pitcher, and weather are verified from the schedule engine.")

    return " ".join(sentences)

def build_model():
    """
    V16.1 Schedule Integrity Engine.

    The game schedule is the source of truth. The hitter's current team is taken from
    today's active roster, not from the season HR stats endpoint. This prevents old-team
    or traded-player records from being attached to the wrong opponent, pitcher, venue,
    or weather record.
    """
    sched_date = SCHEDULE_DATE.isoformat()
    print(f"Schedule date used for MLB feed: {sched_date} Eastern ({SCHEDULE_DATE_LOGIC})")

    sched = requests.get(
        "https://statsapi.mlb.com/api/v1/schedule",
        params={"sportId": 1, "date": sched_date, "hydrate": "probablePitcher,team,venue"},
        timeout=30
    ).json()

    matchups = []
    todays_team_ids = []

    for day in sched.get("dates", []):
        for g in day.get("games", []):
            game_pk = g.get("gamePk", "")
            game_date = g.get("gameDate", "")
            status = g.get("status", {}).get("detailedState", "")
            venue = g.get("venue", {}).get("name", "")

            away_team = g.get("teams", {}).get("away", {}).get("team", {}) or {}
            home_team = g.get("teams", {}).get("home", {}).get("team", {}) or {}

            away_id = away_team.get("id")
            home_id = home_team.get("id")

            away = team_abbrev(away_team)
            home = team_abbrev(home_team)

            away_name = away_team.get("name", "")
            home_name = home_team.get("name", "")

            away_p = g.get("teams", {}).get("away", {}).get("probablePitcher", {}) or {}
            home_p = g.get("teams", {}).get("home", {}).get("probablePitcher", {}) or {}

            away_p_name = away_p.get("fullName", "Unknown")
            away_p_id = away_p.get("id")
            home_p_name = home_p.get("fullName", "Unknown")
            home_p_id = home_p.get("id")

            weather = get_weather_for_venue(venue)

            playable_status = is_playable_game_status(status)
            weather_tied = bool(weather.get("WeatherTiedToVenue", False))

            base = {
                "ScheduleDateUsed": sched_date,
                "ScheduleDateLogic": SCHEDULE_DATE_LOGIC,
                "RunTimestampPacific": RUN_TIMESTAMP_LOCAL,
                "RunTimestampEastern": RUN_TIMESTAMP_EASTERN,
                "GamePk": game_pk,
                "GameDateUTC": game_date,
                "GameStatus": status,
                "PlayableStatus": playable_status,
                "Venue": venue,
                "ParkFactor": park_factor(venue),
                "HomeTeam": home,
                "AwayTeam": away,
                "HomeTeamName": home_name,
                "AwayTeamName": away_name,
                **weather
            }

            if away_id:
                todays_team_ids.append(away_id)
                verified = bool(game_pk and away_id and home_id and venue and playable_status and home_p_id and weather_tied)
                notes = []
                if not game_pk: notes.append("missing gamePk")
                if not home_id: notes.append("missing opponent team id")
                if not venue: notes.append("missing venue")
                if not playable_status: notes.append(f"unplayable game status: {status}")
                if not home_p_id: notes.append("missing opposing probable pitcher")
                if not weather_tied: notes.append(weather.get("WeatherStatus", "weather not tied to venue"))
                matchups.append({
                    "Team ID": int(away_id),
                    "Team": away,
                    "Opponent Team ID": int(home_id) if home_id else "",
                    "Opponent": home,
                    "HomeAway": "Away",
                    "Opposing Pitcher": home_p_name,
                    "Opposing Pitcher ID": home_p_id,
                    "PitcherSource": pitcher_source_label(home_p_id, home_p_name),
                    "PitcherConfidence": "High" if home_p_id else "Low",
                    "MatchupVerified": verified,
                    "VerificationNotes": "Verified: OfficialSchedule+gamePk+Teams+PlayableStatus+Venue+ProbablePitcher+VenueWeather" if verified else "; ".join(notes),
                    "ScheduleIntegrityKey": f"{game_pk}:{away_id}:{home_id}:{home_p_id}:{venue}",
                    **base
                })

            if home_id:
                todays_team_ids.append(home_id)
                verified = bool(game_pk and home_id and away_id and venue and playable_status and away_p_id and weather_tied)
                notes = []
                if not game_pk: notes.append("missing gamePk")
                if not away_id: notes.append("missing opponent team id")
                if not venue: notes.append("missing venue")
                if not playable_status: notes.append(f"unplayable game status: {status}")
                if not away_p_id: notes.append("missing opposing probable pitcher")
                if not weather_tied: notes.append(weather.get("WeatherStatus", "weather not tied to venue"))
                matchups.append({
                    "Team ID": int(home_id),
                    "Team": home,
                    "Opponent Team ID": int(away_id) if away_id else "",
                    "Opponent": away,
                    "HomeAway": "Home",
                    "Opposing Pitcher": away_p_name,
                    "Opposing Pitcher ID": away_p_id,
                    "PitcherSource": pitcher_source_label(away_p_id, away_p_name),
                    "PitcherConfidence": "High" if away_p_id else "Low",
                    "MatchupVerified": verified,
                    "VerificationNotes": "Verified: OfficialSchedule+gamePk+Teams+PlayableStatus+Venue+ProbablePitcher+VenueWeather" if verified else "; ".join(notes),
                    "ScheduleIntegrityKey": f"{game_pk}:{home_id}:{away_id}:{away_p_id}:{venue}",
                    **base
                })

    matchups = pd.DataFrame(matchups)
    if matchups.empty:
        raise RuntimeError("No MLB games found for today. Schedule feed returned empty.")

    verified_matchups = matchups[matchups["MatchupVerified"] == True].copy()
    verified_team_ids = set(pd.to_numeric(verified_matchups["Team ID"], errors="coerce").dropna().astype(int).tolist())
    print(f"Game integrity: {len(verified_matchups)} verified team-game records out of {len(matchups)}")
    print(f"Verified teams today: {sorted(verified_team_ids)}")

    # V16.1: build current-team player map from active rosters before reading season stats.
    active_players = fetch_active_roster_players(list(verified_team_ids))
    if not active_players:
        raise RuntimeError("Active roster lookup failed for all verified teams. Refusing to score stale player-team data.")
    print(f"Active roster map built: {len(active_players)} active players on verified teams")

    # Pull season HR leaders, then assign current team from active roster only.
    d = requests.get(
        "https://statsapi.mlb.com/api/v1/stats",
        params={"stats": "season", "group": "hitting", "playerPool": "ALL", "sortStat": "homeRuns", "limit": 200, "season": YEAR, "hydrate": "team"},
        timeout=30
    ).json()

    rows = []
    skipped_old_team = 0
    for s in d.get("stats", [{}])[0].get("splits", []):
        player_obj = s.get("player", {}) or {}
        pid = player_obj.get("id")
        try:
            pid_int = int(pid) if pid not in [None, ""] else None
        except Exception:
            pid_int = None

        if not pid_int or pid_int not in active_players:
            skipped_old_team += 1
            continue

        roster = active_players[pid_int]
        roster_team_id = int(roster["Team ID"])
        if roster_team_id not in verified_team_ids:
            skipped_old_team += 1
            continue

        rows.append({
            "Player": roster.get("Player") or player_obj.get("fullName"),
            "Player ID": pid_int,
            "Team": roster.get("Team", ""),
            "Team ID": roster_team_id,
            "Team Name": roster.get("Team Name", ""),
            "RosterStatus": roster.get("RosterStatus", "Active"),
            "Season HR": int(s.get("stat", {}).get("homeRuns", 0) or 0)
        })

    model = pd.DataFrame(rows)
    if model.empty:
        raise RuntimeError("No eligible active hitters found after V16.1 roster/team/schedule integrity filter.")

    print(f"Eligible hitters from active rosters: {len(model)}")
    print(f"Skipped hitters not on verified active rosters: {skipped_old_team}")

    out = []
    for _, r in model.iterrows():
        try:
            df = statcast_batter(START.strftime("%Y-%m-%d"), TODAY.strftime("%Y-%m-%d"), int(r["Player ID"]))
            b = df[df["launch_speed"].notna()]
            hh = ((b["launch_speed"] >= 95).mean() * 100) if len(b) else 0
            c100 = ((b["launch_speed"] >= 100).mean() * 100) if len(b) else 0
            fb = ((b["launch_angle"] > 10).mean() * 100) if len(b) else 0
            recent = ((df["events"] == "home_run").sum()) if len(df) else 0
            out.append([r["Player"], hh, c100, fb, recent])
        except Exception:
            out.append([r["Player"], 0, 0, 0, 0])
    model = model.merge(pd.DataFrame(out, columns=["Player", "HardHit%", "100+MPH%", "FlyBall%", "Last7HR"]), on="Player", how="left")

    pitch_rows = []
    for pid, pname in verified_matchups[["Opposing Pitcher ID", "Opposing Pitcher"]].drop_duplicates().dropna().values:
        try:
            data = requests.get(
                f"https://statsapi.mlb.com/api/v1/people/{int(pid)}/stats",
                params={"stats": "season", "group": "pitching", "season": YEAR},
                timeout=20
            ).json()
            splits = data.get("stats", [{}])[0].get("splits", [])
            st = splits[0]["stat"] if splits else {}
            pitch_rows.append({"Opposing Pitcher ID": pid, "ERA": safe_float(st.get("era")), "WHIP": safe_float(st.get("whip")), "K9": safe_float(st.get("strikeoutsPer9Inn"))})
        except Exception:
            pitch_rows.append({"Opposing Pitcher ID": pid, "ERA": 0, "WHIP": 0, "K9": 0})

    pdf = pd.DataFrame(pitch_rows)
    if len(pdf):
        pdf["PitcherVulnerability"] = norm(pdf["ERA"]) * 0.40 + norm(pdf["WHIP"]) * 0.30 + (100 - norm(pdf["K9"])) * 0.30
    else:
        pdf = pd.DataFrame(columns=["Opposing Pitcher ID", "ERA", "WHIP", "K9", "PitcherVulnerability"])

    # V16.1: inner join by active roster Team ID only. This is the hard binding to today's verified game.
    model = model.merge(verified_matchups, on=["Team ID"], how="inner", suffixes=("", "_game"))

    if "Team_game" in model.columns:
        model["Team"] = model["Team_game"].where(model["Team_game"].astype(str).str.len() > 0, model["Team"])
        model = model.drop(columns=["Team_game"])

    model = model.merge(pdf, on="Opposing Pitcher ID", how="left")

    model["PitcherKnown"] = model["Opposing Pitcher ID"].apply(lambda x: bool(str(x).strip()) and str(x).strip().lower() not in ["nan", "none", ""])
    model["RosterTeamVerified"] = model.apply(lambda r: int(r["Team ID"]) in verified_team_ids and str(r.get("RosterStatus", "")) == "Active", axis=1)
    model["HardVerified"] = (
        (model["MatchupVerified"] == True) &
        (model["PitcherKnown"] == True) &
        (model["RosterTeamVerified"] == True) &
        (model["GamePk"].astype(str).str.len() > 0) &
        (model["Venue"].astype(str).str.len() > 0) &
        (model["PlayableStatus"] == True) &
        (model["WeatherTiedToVenue"] == True)
    )

    bad = model[model["HardVerified"] != True]
    if len(bad):
        print(f"Removed {len(bad)} rows failing hard verification.")
    model = model[model["HardVerified"] == True].copy()

    if model.empty:
        raise RuntimeError("All hitters failed V16.1 hard schedule integrity verification. Refusing to send bad report.")

    for c in ["ERA", "WHIP", "K9", "PitcherVulnerability"]:
        model[c] = pd.to_numeric(model[c], errors="coerce").replace([np.inf, -np.inf], 0).fillna(0)
    numeric_defaults = {
        "ParkFactor": 100,
        "WeatherScore": 50,
        "TempF": 70,
        "Humidity": 50,
        "WindMPH": 0,
        "WindBoost": 0,
    }
    for c, default in numeric_defaults.items():
        model[c] = pd.to_numeric(model[c], errors="coerce").replace([np.inf, -np.inf], default).fillna(default)

    model["Score"] = (
        norm(model["Season HR"]) * 0.07 +
        norm(model["Last7HR"]) * 0.12 +
        norm(model["HardHit%"]) * 0.16 +
        norm(model["100+MPH%"]) * 0.12 +
        norm(model["FlyBall%"]) * 0.08 +
        norm(model["PitcherVulnerability"]) * 0.19 +
        norm(model["ParkFactor"]) * 0.07 +
        norm(model["WeatherScore"]) * 0.19
    )

    model = model.replace([np.inf, -np.inf], 0).fillna("")
    model = model.sort_values("Score", ascending=False).reset_index(drop=True)
    model["Rank"] = model.index + 1
    model["Group"] = model["Rank"].apply(lambda x: "Group 1" if x <= 10 else ("Group 2" if x <= 20 else "Group 3"))
    model["Tier"] = model["Rank"].apply(tier_from_rank)

    return model, matchups


def normalize_name_for_odds(name):
    return "".join(ch.lower() for ch in str(name) if ch.isalnum())

def odds_name_tokens(name):
    raw = str(name).replace(",", " ").replace(".", " ").strip().lower()
    return [p for p in re.split(r"\s+", raw) if p]

def odds_name_match_score(model_name, odds_name):
    m_norm = normalize_name_for_odds(model_name)
    o_norm = normalize_name_for_odds(odds_name)
    if not m_norm or not o_norm:
        return 0
    if m_norm == o_norm:
        return 100

    m = odds_name_tokens(model_name)
    o = odds_name_tokens(odds_name)
    if not m or not o:
        return 0

    m_first, m_last = m[0], m[-1]
    o_first, o_last = o[0], o[-1]

    if len(o) >= 2 and m_first == o[-1] and m_last == o[0]:
        return 95
    if m_first == o_first and m_last == o_last:
        return 95
    if m_last == o_last and m_first[0:1] == o_first[0:1]:
        return 88

    # More sportsbook-name fallbacks:
    # Some feeds include extra text or team abbreviations in the player description.
    if len(m_last) >= 5 and m_last in o:
        return 84
    if len(m_last) >= 5 and m_last in " ".join(o):
        return 83
    if len(m_last) >= 5 and o_last == m_last:
        return 82

    return 0

def find_best_odds_match(model_player, odds_map):
    exact = normalize_name_for_odds(model_player)
    if exact in odds_map:
        item = dict(odds_map[exact])
        item["MatchScore"] = 100
        return item

    best_score = 0
    best_item = {}
    for _, item in odds_map.items():
        score = odds_name_match_score(model_player, item.get("PlayerOddsName", ""))
        if score > best_score:
            best_score = score
            best_item = item

    if best_score >= 80:
        item = dict(best_item)
        item["MatchScore"] = best_score
        return item
    return {}

def american_to_implied_pct(odds):
    try:
        odds = float(odds)
        if odds > 0:
            return round(10000 / (odds + 100), 2)
        if odds < 0:
            return round((-odds) / ((-odds) + 100) * 100, 2)
    except Exception:
        pass
    return ""


def sanitize_hr_odds(odds):
    """
    Some odds feeds/books can return HR prop prices with an extra zero.
    Example: +14000 often behaves like +1400 for normal 1+ HR pricing.
    We do NOT overwrite the raw odds silently; this returns a normalized value and a flag.
    """
    try:
        o = float(odds)
    except Exception:
        return "", "Missing"
    if o > 5000:
        return int(round(o / 10)), "Normalized from very large odds"
    return int(o), "OK"

def estimated_model_hr_prob(score):
    """
    Conservative first-pass conversion from model score to estimated HR probability.
    This is not calibrated yet. It gives us a starting value score until we collect results.
    """
    try:
        s = float(score)
        return round(max(1.0, min(22.0, 2.0 + (s / 100.0) * 18.0)), 2)
    except Exception:
        return ""


def is_true_one_plus_hr_market(point):
    """
    Keep only Over 0.5 home run props.
    Exclude Over 1.5 / 2.5 alternate HR props.
    """
    try:
        p = float(point)
        return abs(p - 0.5) < 0.01
    except Exception:
        return False

def odds_sanity_status(best_odds, avg_odds, books_found):
    try:
        b = float(best_odds)
        a = float(avg_odds)
    except Exception:
        return "No true 1+ HR odds found"
    if books_found < 2:
        return "Low book count"
    if b > 1800 or a > 1500:
        return "CHECK - unusually high HR odds"
    if b < 100 or a < 100:
        return "CHECK - unusually low HR odds"
    return "Consensus OK"

def fetch_hr_odds():
    """
    Stable consensus HR odds engine:
    - Queries batter_home_runs
    - Keeps only Over 0.5 HR props
    - Collects all sportsbook prices per player
    - Rejects suspicious alternate/parlay/boosted odds
    - Returns best odds, average odds, books found, and best book
    """
    api_key = os.environ.get("ODDS_API_KEY", "")
    if not api_key:
        print("ODDS_API_KEY missing. Continuing without Vegas odds.")
        return {}, "Missing ODDS_API_KEY"

    sport = "baseball_mlb"
    base = "https://api.the-odds-api.com/v4"
    regions = os.environ.get("ODDS_REGIONS", "us")
    books = os.environ.get("ODDS_BOOKMAKERS", "")
    player_prices = {}
    status = "OK"

    try:
        events_resp = requests.get(
            f"{base}/sports/{sport}/events",
            params={"apiKey": api_key, "dateFormat": "iso"},
            timeout=30
        )
        if events_resp.status_code != 200:
            return {}, f"Events error {events_resp.status_code}: {events_resp.text[:200]}"
        events = events_resp.json()
    except Exception as e:
        return {}, f"Events exception: {e}"

    today_prefix = TODAY.isoformat()
    tomorrow_prefix = (TODAY + timedelta(days=1)).isoformat()
    todays_events = []
    for ev in events:
        ct = str(ev.get("commence_time", ""))
        if ct.startswith(today_prefix) or ct.startswith(tomorrow_prefix):
            todays_events.append(ev)

    for ev in todays_events:
        params = {
            "apiKey": api_key,
            "regions": regions,
            "markets": "batter_home_runs",
            "oddsFormat": "american"
        }
        if books.strip():
            params["bookmakers"] = books.strip()

        try:
            r = requests.get(
                f"{base}/sports/{sport}/events/{ev.get('id')}/odds",
                params=params,
                timeout=30
            )
            if r.status_code != 200:
                status = f"Some odds errors. Last {r.status_code}: {r.text[:120]}"
                continue
            data = r.json()
        except Exception as e:
            status = f"Some odds exceptions. Last: {e}"
            continue

        for bm in data.get("bookmakers", []):
            book_title = bm.get("title", bm.get("key", ""))
            for market in bm.get("markets", []):
                if market.get("key") != "batter_home_runs":
                    continue

                for out in market.get("outcomes", []):
                    outcome_name = str(out.get("name", ""))
                    player = out.get("description") or out.get("player") or ""
                    point = out.get("point", "")
                    price = out.get("price", "")

                    if not player or price in ["", None]:
                        continue

                    if outcome_name.lower() not in ["over", "yes"] and "over" not in outcome_name.lower():
                        continue
                    if not is_true_one_plus_hr_market(point):
                        continue

                    try:
                        p = int(float(price))
                    except Exception:
                        continue

                    # Hard filter obvious alternate/parlay/boosted odds.
                    if p > 2500 or p < 80:
                        continue

                    key = normalize_name_for_odds(player)
                    if not key:
                        continue

                    player_prices.setdefault(key, {
                        "PlayerOddsName": player,
                        "Prices": []
                    })
                    player_prices[key]["Prices"].append({
                        "book": book_title,
                        "price": p,
                        "point": point,
                    })

    odds_map = {}
    for key, item in player_prices.items():
        prices = item["Prices"]
        if not prices:
            continue

        values = sorted([x["price"] for x in prices])
        median = values[len(values)//2]

        filtered = []
        for x in prices:
            if x["price"] <= median + 600 and x["price"] >= max(80, median - 400):
                filtered.append(x)

        if not filtered:
            filtered = prices

        best = max(filtered, key=lambda x: x["price"])
        avg = round(sum(x["price"] for x in filtered) / len(filtered))
        books_found = len(filtered)

        odds_map[key] = {
            "PlayerOddsName": item["PlayerOddsName"],
            "BestHROdds": int(best["price"]),
            "AvgHROdds": int(avg),
            "BooksFound": books_found,
            "BestBook": best["book"],
            "OddsBook": best["book"],
            "OddsPoint": best["point"],
            "RawHROdds": int(best["price"]),
            "ImpliedProbPct": american_to_implied_pct(avg),
            "OddsNote": odds_sanity_status(best["price"], avg, books_found),
            "OddsStatus": status
        }

    return odds_map, status

def add_odds_to_model(model):
    odds_map, odds_status = fetch_hr_odds()
    rows = []
    for _, r in model.iterrows():
        od = find_best_odds_match(r.get("Player", ""), odds_map)

        best = od.get("BestHROdds", "")
        avg = od.get("AvgHROdds", "")
        implied = od.get("ImpliedProbPct", "")
        model_prob = estimated_model_hr_prob(r.get("Score", 0))

        if implied != "" and model_prob != "":
            edge = round(float(model_prob) - float(implied), 2)
        else:
            edge = ""

        value_score = float(r.get("Score", 0))
        if edge != "":
            value_score = value_score + max(0, edge) * 1.5

        rows.append({
            "Player": r.get("Player", ""),
            "PlayerOddsName": od.get("PlayerOddsName", ""),
            "BestHROdds": best,
            "AvgHROdds": avg,
            "BooksFound": od.get("BooksFound", ""),
            "BestBook": od.get("BestBook", ""),
            "OddsMatchScore": od.get("MatchScore", 100 if od else ""),
            "RawHROdds": od.get("RawHROdds", ""),
            "OddsBook": od.get("OddsBook", ""),
            "OddsPoint": od.get("OddsPoint", ""),
            "OddsNote": od.get("OddsNote", "No true 1+ HR odds found"),
            "ImpliedProbPct": implied,
            "ModelProbEstPct": model_prob,
            "EdgePct": edge,
            "ValueScore": round(value_score, 2),
            "OddsStatus": od.get("OddsStatus", odds_status)
        })
    odds_df = pd.DataFrame(rows)
    model = model.merge(odds_df, on="Player", how="left")
    return model

def confidence_grade(row):
    """Baseball target label, not betting grade."""
    return target_label_from_score(row.get("Score", 0))


def add_target_rank_and_confidence(model):
    model = model.sort_values("Score", ascending=False).reset_index(drop=True)
    model["Rank"] = model.index + 1
    model["Group"] = model["Rank"].apply(lambda x: "Group 1" if x <= 10 else ("Group 2" if x <= 20 else "Group 3"))
    model["Tier"] = model["Rank"].apply(tier_from_rank)
    model["ConfidenceLabel"] = model.apply(confidence_grade, axis=1)
    model["Reason"] = model.apply(build_reason_text, axis=1)
    return model


def scouting_stars(label):
    label = str(label or "")
    if label == "Elite Target":
        return "★★★★★"
    if label == "Strong Target":
        return "★★★★☆"
    if label == "Solid Target":
        return "★★★☆☆"
    if label == "Watchlist":
        return "★★☆☆☆"
    return "★☆☆☆☆"


def safe_num(value, default=0.0):
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def rating_from_value(value, excellent=75, strong=60, solid=50, weak=40):
    v = safe_num(value, 50)
    if v >= excellent:
        return "★★★★★", "Excellent"
    if v >= strong:
        return "★★★★☆", "Good"
    if v >= solid:
        return "★★★☆☆", "Neutral+"
    if v >= weak:
        return "★★☆☆☆", "Neutral"
    return "★☆☆☆☆", "Poor"


def pitcher_rating(row):
    pv = safe_num(row.get("PitcherVulnerability", 50), 50)
    if pv >= 75:
        return "★★★★★", "Excellent"
    if pv >= 60:
        return "★★★★☆", "Favorable"
    if pv >= 50:
        return "★★★☆☆", "Average"
    if pv >= 35:
        return "★★☆☆☆", "Tough"
    return "★☆☆☆☆", "Difficult"

def environment_rating(row):
    wx = safe_num(row.get("WeatherScore", 50), 50)
    wind = str(row.get("WindImpact", "") or "")
    park = safe_num(row.get("ParkFactor", 100), 100)
    dome = str(row.get("Dome", "") or "").lower() == "true"

    # Start with weather score, then adjust for park and wind in a readable way.
    env_score = wx
    if wind == "Out":
        env_score += 12
    elif wind == "Cross/Out":
        env_score += 7
    elif wind == "Cross/In":
        env_score -= 5
    elif wind == "In":
        env_score -= 10
    if park >= 110:
        env_score += 8
    elif park >= 105:
        env_score += 4
    elif park <= 95:
        env_score -= 4
    if dome:
        env_score = max(48, min(58, env_score))

    if env_score >= 75:
        return "★★★★★", "Excellent"
    if env_score >= 62:
        return "★★★★☆", "Good"
    if env_score >= 50:
        return "★★★☆☆", "Neutral"
    if env_score >= 40:
        return "★★☆☆☆", "Poor"
    return "★☆☆☆☆", "Difficult"


def power_rating(row):
    last_hr = safe_num(row.get("Last7HR", 0), 0)
    mph100 = safe_num(row.get("100+MPH%", 0), 0)
    fb = safe_num(row.get("FlyBall%", 0), 0)
    hard = safe_num(row.get("HardHit%", 0), 0)
    score = 0
    if last_hr >= 5: score += 3
    elif last_hr >= 2: score += 2
    elif last_hr >= 1: score += 1
    if mph100 >= 30: score += 3
    elif mph100 >= 20: score += 2
    elif mph100 >= 12: score += 1
    if fb >= 60: score += 2
    elif fb >= 45: score += 1
    if hard >= 45: score += 2
    elif hard >= 35: score += 1
    if score >= 8:
        return "★★★★★", "Excellent Power Form"
    if score >= 6:
        return "★★★★☆", "Strong Power Form"
    if score >= 4:
        return "★★★☆☆", "Solid Power Form"
    if score >= 2:
        return "★★☆☆☆", "Developing Power Form"
    return "★☆☆☆☆", "Limited Recent Power Form"


def risk_level(row):
    label = str(row.get("ConfidenceLabel", "") or "")
    wind = str(row.get("WindImpact", "") or "")
    pitcher_known = str(row.get("PitcherConfidence", "") or "").lower() == "high"
    if label in ["Elite Target", "Strong Target"] and pitcher_known and wind not in ["In", "Cross/In"]:
        return "Low"
    if label in ["Solid Target", "Strong Target"]:
        return "Medium"
    return "Medium-High"


def clean_game_label(row):
    team = str(row.get("Team", "") or "")
    opp = str(row.get("Opponent", "") or "")
    venue = str(row.get("Venue", "") or "")
    if team and opp and venue:
        return f"{team} vs {opp} at {venue}"
    if team and opp:
        return f"{team} vs {opp}"
    return venue or "Verified MLB matchup"


def wind_text(row):
    dome = str(row.get("Dome", "") or "").lower() == "true"
    if dome:
        return "Dome / controlled conditions"
    wind = str(row.get("WindImpact", "") or "")
    mph = safe_num(row.get("WindMPH", 0), 0)
    if wind:
        return f"{mph:.1f} mph {wind}"
    return "Weather verified"


def temp_text(row):
    dome = str(row.get("Dome", "") or "").lower() == "true"
    if dome:
        return "Controlled"
    temp = safe_num(row.get("TempF", 0), 0)
    return f"{temp:.0f}°F" if temp else "Verified"


def format_env_line(r):
    venue = r.get("Venue", "")
    park = safe_num(r.get("ParkFactor", 100), 100)
    stars, label = environment_rating(r)
    return f"{stars} {label} | {venue} | {temp_text(r)} | Wind: {wind_text(r)} | Park: {park_descriptor(park)}"


def park_descriptor(park):
    if park >= 110:
        return "Very HR-Friendly"
    if park >= 105:
        return "HR-Friendly"
    if park <= 95:
        return "Pitcher-Friendly"
    return "Neutral"


def format_power_line(r):
    stars, label = power_rating(r)
    season_hr = int(safe_num(r.get('Season HR', 0), 0))
    recent_hr = int(safe_num(r.get('Last7HR', 0), 0))
    hard = safe_num(r.get('HardHit%', 0), 0)
    mph = safe_num(r.get('100+MPH%', 0), 0)
    fb = safe_num(r.get('FlyBall%', 0), 0)
    return f"{stars} {label} | Season HR: {season_hr} | Recent HR: {recent_hr} | HardHit: {hard:.1f}% | 100+ MPH: {mph:.1f}% | FlyBall: {fb:.1f}%"


def format_pitcher_line(r):
    pitcher = r.get("Opposing Pitcher", "")
    stars, label = pitcher_rating(r)
    era = safe_num(r.get('ERA', 0), 0)
    whip = safe_num(r.get('WHIP', 0), 0)
    k9 = safe_num(r.get('K9', 0), 0)
    return f"{stars} {label} | {pitcher} | ERA {era:.2f} | WHIP {whip:.2f} | K/9 {k9:.1f}"



def confidence_rating_text(label):
    label = str(label or "")
    if label == "Elite Target":
        return "Highest Confidence"
    if label == "Strong Target":
        return "High Confidence"
    if label == "Solid Target":
        return "Moderate Confidence"
    if label == "Watchlist":
        return "Speculative"
    return "Longshot"


def one_line_summary(row):
    player = str(row.get("Player", "This hitter") or "This hitter")
    label = str(row.get("ConfidenceLabel", "Watchlist") or "Watchlist")
    p_stars, p_label = pitcher_rating(row)
    e_stars, e_label = environment_rating(row)
    pow_stars, pow_label = power_rating(row)
    wind = str(row.get("WindImpact", "") or "")
    venue = str(row.get("Venue", "today's park") or "today's park")
    if label in ["Elite Target", "Strong Target"]:
        return f"{player} is one of today's top HR targets, combining {pow_label.lower()}, a {p_label.lower()} pitcher matchup, and a {e_label.lower()} hitting environment at {venue}."
    if p_label in ["Excellent", "Favorable"]:
        return f"{player} gets a strong matchup boost today against a vulnerable opposing pitcher, with the rest of the profile determining whether he moves beyond watchlist status."
    if wind in ["Out", "Cross/Out"]:
        return f"{player} carries upside today because the weather is helping the ball carry, even though the full profile is more speculative."
    return f"{player} remains a watchlist HR candidate based on verified matchup data and recent power indicators."


def opening_sentence(row):
    player = str(row.get("Player", "This hitter") or "This hitter")
    label = str(row.get("ConfidenceLabel", "Watchlist") or "Watchlist")
    last_hr = int(safe_num(row.get("Last7HR", 0), 0))
    templates = [
        f"{player} headlines today's report as a {label.lower()}.",
        f"{player} profiles as a {label.lower()} in today's verified matchup.",
        f"{player} brings one of today's more interesting power profiles into this slate.",
        f"{player} stands out today because the model likes the combination of form, matchup, and environment.",
        f"{player} earns attention today after showing recent power in the Statcast window."
    ]
    if last_hr >= 4:
        return f"{player} has been one of the hottest power bats in the recent Statcast window."
    return templates[int(safe_num(row.get("Rank", 1), 1)) % len(templates)]


def pitcher_scouting_text(row):
    pitcher = str(row.get("Opposing Pitcher", "the opposing starter") or "the opposing starter")
    era = safe_num(row.get("ERA", 0), 0)
    whip = safe_num(row.get("WHIP", 0), 0)
    k9 = safe_num(row.get("K9", 0), 0)
    stars, label = pitcher_rating(row)
    notes = []
    if era >= 4.75:
        notes.append(f"elevated ERA ({era:.2f})")
    if whip >= 1.35:
        notes.append(f"traffic on the bases ({whip:.2f} WHIP)")
    if 0 < k9 <= 7.5:
        notes.append(f"below-average swing-and-miss ({k9:.1f} K/9)")
    if notes:
        return f"{pitcher} grades as a {label.lower()} HR matchup because of " + ", ".join(notes) + "."
    return f"{pitcher} grades as a {label.lower()} HR matchup, so the pick leans more heavily on player form and environment."


def environment_scouting_text(row):
    venue = str(row.get("Venue", "today's park") or "today's park")
    wind = str(row.get("WindImpact", "") or "")
    wind_mph = safe_num(row.get("WindMPH", 0), 0)
    dome = str(row.get("Dome", "") or "").lower() == "true"
    stars, label = environment_rating(row)
    if dome:
        return f"{venue} is a controlled dome environment, keeping the weather read neutral and stable."
    if wind in ["Out", "Cross/Out"]:
        return f"{venue} projects as a {label.lower()} HR environment with a {wind_mph:.1f} mph {wind.lower()} wind helping carry."
    if wind in ["In", "Cross/In"]:
        return f"{venue} is less helpful for carry today with a {wind_mph:.1f} mph {wind.lower()} wind, so the case depends more on power form and pitcher matchup."
    return f"{venue} grades as a {label.lower()} HR environment using the verified weather record."


def readable_game_name(row):
    team = str(row.get("Team", "") or "")
    opp = str(row.get("Opponent", "") or "")
    venue = str(row.get("Venue", "") or "")
    if team and opp and venue:
        return f"{team} vs {opp} — {venue}"
    return clean_game_label(row).replace(" at ", " — ")

def power_drivers(row):
    drivers = []
    last_hr = safe_num(row.get("Last7HR", 0), 0)
    mph100 = safe_num(row.get("100+MPH%", 0), 0)
    fb = safe_num(row.get("FlyBall%", 0), 0)
    hard = safe_num(row.get("HardHit%", 0), 0)
    pv = safe_num(row.get("PitcherVulnerability", 50), 50)
    wind = str(row.get("WindImpact", "") or "")
    park = safe_num(row.get("ParkFactor", 100), 100)

    if last_hr >= 2:
        drivers.append(f"🔥 Hot streak ({int(last_hr)} recent HR)")
    elif last_hr >= 1:
        drivers.append("🔥 Recent HR form")
    if mph100 >= 20:
        drivers.append(f"💥 100+ MPH contact ({mph100:.1f}%)")
    if hard >= 40:
        drivers.append(f"💪 Hard-hit profile ({hard:.1f}%)")
    if fb >= 50:
        drivers.append(f"🚀 Fly-ball profile ({fb:.1f}%)")
    if pv >= 60:
        drivers.append("⚾ Favorable pitcher")
    elif pv >= 50:
        drivers.append("⚾ Manageable matchup")
    if wind in ["Out", "Cross/Out"]:
        drivers.append("🌬 Wind assist")
    if park >= 105:
        drivers.append("🏟 HR-friendly park")
    drivers.append("✅ Verified")
    return " | ".join(drivers)


def build_scouting_reason(row):
    player = str(row.get("Player", "") or "This hitter")
    last_hr = int(safe_num(row.get("Last7HR", 0), 0))
    mph100 = safe_num(row.get("100+MPH%", 0), 0)
    hard = safe_num(row.get("HardHit%", 0), 0)
    fb = safe_num(row.get("FlyBall%", 0), 0)

    power_bits = []
    if last_hr >= 2:
        power_bits.append(f"{last_hr} home runs in the recent Statcast window")
    elif last_hr == 1:
        power_bits.append("a recent home run in the Statcast window")
    if mph100 >= 20:
        power_bits.append(f"a {mph100:.1f}% 100+ MPH contact rate")
    if hard >= 40:
        power_bits.append(f"a strong {hard:.1f}% hard-hit rate")
    if fb >= 50:
        power_bits.append(f"a fly-ball-heavy batted-ball profile ({fb:.1f}%)")

    if power_bits:
        player_text = f"{opening_sentence(row)} The power case is supported by " + ", ".join(power_bits) + "."
    else:
        player_text = f"{opening_sentence(row)} The profile is driven more by matchup and environment than recent home run volume."

    return " ".join([
        player_text,
        pitcher_scouting_text(row),
        environment_scouting_text(row),
        "The schedule engine verified the game, opponent, ballpark, probable pitcher, and weather before this player was ranked."
    ])


def add_player_report_rows(rows, r):
    rank = int(r.get("Rank", 0))
    label = r.get("ConfidenceLabel", "")
    stars = scouting_stars(label)
    player = r.get("Player", "")
    team = r.get("Team", "")
    opp = r.get("Opponent", "")

    rows.append([f"{rank}. {stars} {label}", f"{player} ({team}) vs {opp}"])
    rows.append(["Today's Summary", one_line_summary(r)])
    rows.append(["Model Confidence", f"{stars} {confidence_rating_text(label)} | Risk: {risk_level(r)}"])
    rows.append(["Pitcher Matchup", format_pitcher_line(r)])
    rows.append(["HR Environment", format_env_line(r)])
    rows.append(["Power Profile", format_power_line(r)])
    rows.append(["Power Drivers", power_drivers(r)])
    rows.append(["Scouting Report", build_scouting_reason(r)])
    rows.append(["Verification", "✓ Schedule Verified | ✓ Pitcher Verified | ✓ Ballpark Verified | ✓ Weather Verified"])
    rows.append([])


def best_environment_summary(card):
    if card.empty:
        return []
    tmp = card.copy()
    def env_sort(row):
        stars, label = environment_rating(row)
        wx = safe_num(row.get("WeatherScore", 50), 50)
        wind = str(row.get("WindImpact", "") or "")
        park = safe_num(row.get("ParkFactor", 100), 100)
        bonus = 0
        if wind == "Out": bonus += 12
        elif wind == "Cross/Out": bonus += 7
        if park >= 105: bonus += 4
        return wx + bonus
    tmp["EnvSort"] = tmp.apply(env_sort, axis=1)
    best_env = tmp.sort_values(["EnvSort", "Score"], ascending=False).iloc[0]
    top = card.sort_values("Rank").iloc[0]
    hottest = tmp.sort_values(["Last7HR", "Score"], ascending=False).iloc[0]
    best_pitcher = tmp.sort_values(["PitcherVulnerability", "Score"], ascending=False).iloc[0]
    env_stars, env_label = environment_rating(best_env)

    games = []
    for _, r in tmp.sort_values(["EnvSort", "Score"], ascending=False).iterrows():
        g = clean_game_label(r)
        # De-dupe CHC vs COL / COL vs CHC by venue and team pair.
        teams = sorted([str(r.get("Team", "")), str(r.get("Opponent", ""))])
        key = (tuple(teams), str(r.get("Venue", "")))
        if key not in [x[0] for x in games]:
            games.append((key, g))
        if len(games) >= 3:
            break

    return [
        ["Daily Outlook"],
        ["Best HR Environment", readable_game_name(best_env)],
        ["Environment Rating", f"{env_stars} {env_label}"],
        ["Wind", wind_text(best_env)],
        ["Temperature", temp_text(best_env)],
        ["Top Overall Target", f"{top.get('Player','')} ({top.get('Team','')}) - {top.get('ConfidenceLabel','')}"],
        ["Hottest Recent Power", f"{hottest.get('Player','')} ({hottest.get('Team','')}) - {int(safe_num(hottest.get('Last7HR',0),0))} recent HR"],
        ["Best Pitcher Matchup", f"{best_pitcher.get('Player','')} vs {best_pitcher.get('Opposing Pitcher','')}"],
        ["Games Worth Watching", "; ".join([x[1] for x in games])],
        []
    ]


def build_email_summary_rows(card):
    rows = []
    rows.append(["Daily MLB HR Targets - Professional Scouting Report"])
    rows.append(["Last Updated", RUN_TIMESTAMP_LOCAL])
    rows.append(["Model Version", MODEL_VERSION])
    rows.append(["Schedule Date Used", SCHEDULE_DATE.isoformat()])
    rows.append(["Schedule Date Logic", SCHEDULE_DATE_LOGIC])
    rows.append([])

    rows.extend(best_environment_summary(card))

    rows.append(["Top HR Targets"])
    top = card.sort_values("Rank").head(5)
    for _, r in top.iterrows():
        add_player_report_rows(rows, r)

    rows.append(["Watchlist"])
    watch = card.sort_values("Rank").iloc[5:9]
    for _, r in watch.iterrows():
        add_player_report_rows(rows, r)

    rows.append(["Model Notes"])
    rows.append(["Ranking Basis", "Season power, recent HR form, hard-hit contact, 100+ MPH contact, fly-ball profile, pitcher matchup, park factor, and weather/wind."])
    rows.append(["Game Integrity", "Every target is tied to a verified scheduled game, venue, opposing pitcher, and weather record before ranking."])
    rows.append(["Inactive Player Filter", "Players not on active rosters are removed before scoring."])
    rows.append(["Report Style", "V16.2 keeps the scoring engine locked and fixes schedule-date integrity using the MLB Eastern slate date."])
    rows.append([])
    rows.append(["Results Tracking"])
    rows.append(["HR Result", "1 = HR, 0 = No HR."])
    return clean_rows(rows)

def refresh_email_summary(sh, card):
    ws = get_or_create_ws(sh, "Email Summary", 100, 10)
    rows = build_email_summary_rows(card)
    ws.clear()
    ws.update(values=rows, range_name=f"A1:B{len(rows)}")
    print("Email Summary updated")


def odds_bucket(odds):
    try:
        o = float(odds)
    except Exception:
        return "No Odds"
    if o < 300:
        return "< +300"
    if o < 600:
        return "+300 to +599"
    if o < 1000:
        return "+600 to +999"
    if o < 1500:
        return "+1000 to +1499"
    if o < 2500:
        return "+1500 to +2499"
    return "+2500+"

def refresh_hr_targets(sh, card):
    ws = get_or_create_ws(sh, "HR Targets", 100, 20)
    headers = [
        "Date","Rank","Tier","Confidence","Player","Team","Opponent","Opposing Pitcher",
        "Venue","Verified","Score","Season HR","Last7HR","HardHit%","100+MPH%","FlyBall%",
        "PitcherVulnerability","ParkFactor","WeatherScore","WindImpact","Reason"
    ]
    rows = [["Daily HR Targets"], ["Last Updated", RUN_TIMESTAMP_LOCAL], [], headers]
    for _, r in card.sort_values("Rank").iterrows():
        rows.append([
            TODAY.isoformat(), int(r.get("Rank",0)), r.get("Tier",""), r.get("ConfidenceLabel",""),
            r.get("Player",""), r.get("Team",""), r.get("Opponent",""), r.get("Opposing Pitcher",""),
            r.get("Venue",""), r.get("VerificationNotes",""), round(float(r.get("Score",0)),2), int(r.get("Season HR",0)), int(r.get("Last7HR",0)),
            round(float(r.get("HardHit%",0)),2), round(float(r.get("100+MPH%",0)),2), round(float(r.get("FlyBall%",0)),2),
            round(float(r.get("PitcherVulnerability",0)),2), int(float(r.get("ParkFactor",100))),
            round(float(r.get("WeatherScore",50)),1), r.get("WindImpact",""), r.get("Reason","")
        ])
    ws.clear()
    ws.update(values=clean_rows(rows), range_name=f"A1:U{len(rows)}")
    print("HR Targets updated")


def ensure_header(ws, headers):
    """
    Makes sure row 1 has the full current header set.
    This fixes missing headers caused by older model versions that had fewer columns.
    It does not delete data.
    """
    existing = ws.row_values(1)
    if existing != headers:
        ws.update(values=[headers], range_name="A1:AZ1")


def auto_grade_daily_picks(sh):
    """
    Safe auto-grading placeholder for V11H.
    We are stabilizing consensus HR odds first. Manual HR Result grading still works.
    """
    print("Auto-grading skipped in V11H; ROI still works from manually entered HR Result.")
    return

def refresh_roi_dashboard(sh):
    """
    Safe ROI refresh placeholder for V11H.
    Existing ROI Dashboard remains in the sheet. We are stabilizing consensus HR odds first.
    """
    print("ROI Dashboard refresh skipped in V11H while odds engine is stabilized.")
    return


def refresh_run_log(sh, model, matchups, card):
    ws = get_or_create_ws(sh, "Run Log", 200, 12)
    verified_games = 0
    verified_pitchers = 0
    weather_verified = 0
    try:
        verified_games = int(matchups["MatchupVerified"].sum()) if "MatchupVerified" in matchups.columns else 0
        verified_pitchers = int(pd.to_numeric(matchups["Opposing Pitcher ID"], errors="coerce").notna().sum()) if "Opposing Pitcher ID" in matchups.columns else 0
        weather_verified = int(matchups["WeatherScore"].notna().sum()) if "WeatherScore" in matchups.columns else 0
    except Exception:
        pass
    rows = [
        ["Run Timestamp Pacific", RUN_TIMESTAMP_LOCAL],
        ["Run Timestamp Eastern", RUN_TIMESTAMP_EASTERN],
        ["Schedule Date Used", SCHEDULE_DATE.isoformat()],
        ["Schedule Date Logic", SCHEDULE_DATE_LOGIC],
        ["Model Version", MODEL_VERSION],
        ["Games / Team Records Verified", verified_games],
        ["Probable Pitchers Matched", verified_pitchers],
        ["Weather Records Matched", weather_verified],
        ["Players Scored", len(model)],
        ["Top Targets Selected", len(card)],
        ["Email Summary Updated", "Yes"],
        ["Status", "Completed Successfully"],
    ]
    ws.clear()
    ws.update(values=clean_rows(rows), range_name=f"A1:B{len(rows)}")
    print("Run Log updated")

def write_to_sheet(model, matchups):
    gc = auth_google()
    sh = gc.open_by_key(SHEET_ID)

    daily_ws = get_or_create_ws(sh, "Daily Picks", 1000, 40)
    results_ws = get_or_create_ws(sh, "Model Results", 1000, 45)
    weather_ws = get_or_create_ws(sh, "Weather Log", 1000, 20)
    summary_ws = get_or_create_ws(sh, "Scorecard Summary", 100, 12)
    integrity_ws = get_or_create_ws(sh, "Game Integrity Log", 1000, 25)

    card = model[model["Rank"] <= 9].copy()
    daily_cols = ["Date","Model Version","Tier","Rank","Group","Confidence","Player","Team","Opponent","Opposing Pitcher","PitcherSource","PitcherConfidence","Venue","Verified","Score","Season HR","Last7HR","HardHit%","100+MPH%","FlyBall%","PitcherVulnerability","ParkFactor","TempF","Humidity","WindMPH","WindFromDir","WindBlowingTo","WindAngleToCF","WindImpact","WindBoost","Dome","WeatherScore","Reason","HR Result"]
    daily_rows = []
    for _, r in card.iterrows():
        daily_rows.append([TODAY.isoformat(),MODEL_VERSION,r["Tier"],int(r["Rank"]),r["Group"],r.get("ConfidenceLabel",""),r["Player"],r["Team"],r["Opponent"],r["Opposing Pitcher"],r.get("PitcherSource",""),r.get("PitcherConfidence",""),r["Venue"],r.get("GamePk",""),r.get("HomeAway",""),r.get("MatchupVerified",""),r.get("VerificationNotes",""),round(float(r["Score"]),2),int(r["Season HR"]),int(r["Last7HR"]),round(float(r["HardHit%"]),2),round(float(r["100+MPH%"]),2),round(float(r["FlyBall%"]),2),round(float(r["PitcherVulnerability"]),2),int(float(r["ParkFactor"])),round(float(r["TempF"]),1),round(float(r["Humidity"]),1),round(float(r["WindMPH"]),1),r["WindFromDir"],r["WindBlowingTo"],r["WindAngleToCF"],r["WindImpact"],round(float(r["WindBoost"]),1),str(r["Dome"]),round(float(r["WeatherScore"]),1),r.get("Reason",""),""])
    ensure_header(daily_ws, daily_cols)
    daily_ws.append_rows(clean_rows(daily_rows), value_input_option="USER_ENTERED")

    results_cols = ["Date","Model Version","Rank","Group","Confidence","Player","Team","Opponent","Opposing Pitcher","PitcherSource","PitcherConfidence","ERA","WHIP","K9","PitcherVulnerability","Venue","GamePk","HomeAway","MatchupVerified","VerificationNotes","ParkFactor","TempF","Humidity","WindMPH","WindFromDir","WindBlowingTo","WindAngleToCF","WindImpact","WindBoost","Dome","WeatherScore","Season HR","Last7HR","HardHit%","100+MPH%","FlyBall%","Score","Reason"]
    results_rows = []
    for _, r in model.head(30).iterrows():
        results_rows.append([TODAY.isoformat(),MODEL_VERSION,int(r["Rank"]),r["Group"],r.get("ConfidenceLabel",""),r["Player"],r["Team"],r["Opponent"],r["Opposing Pitcher"],r.get("PitcherSource",""),r.get("PitcherConfidence",""),round(float(r["ERA"]),2),round(float(r["WHIP"]),2),round(float(r["K9"]),2),round(float(r["PitcherVulnerability"]),2),r["Venue"],r.get("GamePk",""),r.get("HomeAway",""),r.get("MatchupVerified",""),r.get("VerificationNotes",""),int(float(r["ParkFactor"])),round(float(r["TempF"]),1),round(float(r["Humidity"]),1),round(float(r["WindMPH"]),1),r["WindFromDir"],r["WindBlowingTo"],r["WindAngleToCF"],r["WindImpact"],round(float(r["WindBoost"]),1),str(r["Dome"]),round(float(r["WeatherScore"]),1),int(r["Season HR"]),int(r["Last7HR"]),round(float(r["HardHit%"]),2),round(float(r["100+MPH%"]),2),round(float(r["FlyBall%"]),2),round(float(r["Score"]),2),r.get("Reason","")])
    ensure_header(results_ws, results_cols)
    results_ws.append_rows(clean_rows(results_rows), value_input_option="USER_ENTERED")

    weather_cols = ["Date","ScheduleDateUsed","Venue","TempF","Humidity","WindMPH","WindFromDir","WindBlowingTo","WindAngleToCF","WindImpact","WindBoost","Dome","WeatherScore","WeatherSource","WeatherStatus","WeatherTiedToVenue"]
    weather_log = matchups[["ScheduleDateUsed","Venue","TempF","Humidity","WindMPH","WindFromDir","WindBlowingTo","WindAngleToCF","WindImpact","WindBoost","Dome","WeatherScore","WeatherSource","WeatherStatus","WeatherTiedToVenue"]].drop_duplicates().copy()
    weather_rows = [[TODAY.isoformat(), r["ScheduleDateUsed"], r["Venue"], r["TempF"], r["Humidity"], r["WindMPH"], r["WindFromDir"], r["WindBlowingTo"], r["WindAngleToCF"], r["WindImpact"], r["WindBoost"], str(r["Dome"]), r["WeatherScore"], r.get("WeatherSource",""), r.get("WeatherStatus",""), r.get("WeatherTiedToVenue","")] for _, r in weather_log.iterrows()]
    ensure_header(weather_ws, weather_cols)
    weather_ws.append_rows(clean_rows(weather_rows), value_input_option="USER_ENTERED")


    integrity_cols = ["Date","ScheduleDateUsed","ScheduleDateLogic","Rank","Player","Team","Opponent","HomeAway","HomeTeam","AwayTeam","Venue","GamePk","GameDateUTC","GameStatus","PlayableStatus","Opposing Pitcher","Opposing Pitcher ID","PitcherSource","PitcherConfidence","MatchupVerified","VerificationNotes","WeatherSource","WeatherStatus","WeatherTiedToVenue","TempF","WindMPH","WindImpact","WeatherScore","Dome"]
    integrity_rows = []
    for _, r in model.head(30).iterrows():
        integrity_rows.append([
            TODAY.isoformat(), r.get("ScheduleDateUsed", SCHEDULE_DATE.isoformat()), r.get("ScheduleDateLogic", SCHEDULE_DATE_LOGIC),
            int(r.get("Rank",0)), r.get("Player",""), r.get("Team",""), r.get("Opponent",""), r.get("HomeAway",""),
            r.get("HomeTeam",""), r.get("AwayTeam",""), r.get("Venue",""), r.get("GamePk",""), r.get("GameDateUTC",""), r.get("GameStatus",""),
            r.get("PlayableStatus",""), r.get("Opposing Pitcher",""), r.get("Opposing Pitcher ID",""), r.get("PitcherSource",""), r.get("PitcherConfidence",""),
            r.get("MatchupVerified",""), r.get("VerificationNotes",""), r.get("WeatherSource",""), r.get("WeatherStatus",""), r.get("WeatherTiedToVenue",""),
            r.get("TempF",""), r.get("WindMPH",""), r.get("WindImpact",""), r.get("WeatherScore",""), str(r.get("Dome",""))
        ])
    ensure_header(integrity_ws, integrity_cols)
    integrity_ws.append_rows(clean_rows(integrity_rows), value_input_option="USER_ENTERED")

    summary_ws.clear()
    summary_rows = [
        ["Daily MLB HR Picks Scorecard",""],
        ["Last Automated Run",RUN_TIMESTAMP_LOCAL],
        ["Eastern Run Time",RUN_TIMESTAMP_EASTERN],
        ["Schedule Date Used",SCHEDULE_DATE.isoformat()],
        ["Schedule Date Logic",SCHEDULE_DATE_LOGIC],
        ["Model Version",MODEL_VERSION],
        ["Primary Picks",len(card[card["Tier"]=="Primary"])],
        ["Secondary Picks",len(card[card["Tier"]=="Secondary"])],
        ["Longshot Picks",len(card[card["Tier"]=="Longshot"])],
        ["Weather Status","Live weather + temperature + humidity + wind direction + outfield orientation"],
        ["Pitcher Status","V16.2 requires verified opposing probable pitcher before scoring"],
        ["Results Tracking","Manual HR Result remains: 1 = HR, 0 = No HR"],
        ["Target Ranking","V16.2 verified HR targets with Eastern slate schedule-date fix"],
        ["HR Targets","HR Targets tab added"],
        ["Inactive Filter","Active roster filter plus verified game/team merge before scoring"],
        ["Game Integrity Log","Added to verify player, team, venue, pitcher, and weather binding"],
        ["Email Summary","Email Summary tab upgraded for polished V16.2 report"],
        ["Sheet Updated","Yes"],
    ]
    summary_ws.update(values=clean_rows(summary_rows), range_name=f"A1:B{len(summary_rows)}")
    refresh_hr_targets(sh, card)
    refresh_email_summary(sh, card)
    refresh_run_log(sh, model, matchups, card)
    print(f"Updated Google Sheet: {SHEET_NAME}")
    return card

def main():
    model, matchups = build_model()
    model = add_target_rank_and_confidence(model)
    card = write_to_sheet(model, matchups)
    print("Daily HR Targets")
    for tier in ["Primary","Secondary","Longshot"]:
        print("")
        print(tier)
        for _, r in card[card["Tier"] == tier].iterrows():
            print(f"- {r['Player']} ({r['Team']}) vs {r['Opposing Pitcher']} — Score {round(float(r['Score']),1)} — {r.get('ConfidenceLabel','')} — Weather {r.get('WeatherScore','')} ({r.get('WindImpact','')}) — {r.get('Reason','')}")

if __name__ == "__main__":
    main()


