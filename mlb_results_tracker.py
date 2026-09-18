"""Downstream MLB prediction archiving and official-result grading.

This module intentionally does not import or modify either production selection model.
It reads their already-published Google Sheet outputs, takes idempotent snapshots, and
later fills result-only columns from the official MLB game feed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo



SHEET_NAME = os.environ.get("SHEET_NAME", "MLB Daily Model")
MODEL_TIMEZONE = os.environ.get("MLB_SCHEDULE_TZ", "America/New_York")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
MLB_FEED_URL = "https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"

GAME_SOURCE_TAB = "Game Picks"
HR_SOURCE_TAB = "Model Results"
PROP_SOURCE_TAB = "Player Props"
BEST_CARD_SOURCE_TAB = "Best Card"
GAME_TRACKING_TAB = "Tracking - Game Picks"
HR_TRACKING_TAB = "Tracking - HR Picks"
PROP_TRACKING_TAB = "Tracking - Player Props"
BEST_CARD_TRACKING_TAB = "Tracking - Best Card"
PERFORMANCE_TAB = "Tracking - Performance"
RUN_LOG_TAB = "Tracking - Run Log"
BEST_CARD_RESULTS_EMAIL_TAB = "Best Card Results Email Summary"

GAME_HEADERS = [
    "Prediction ID", "Date", "Model Version", "Rank", "GamePk", "Game",
    "Venue", "Projected Winner", "Opponent", "Confidence", "Win Probability",
    "Expected Margin", "Away Team", "Home Team", "Away Projected Runs",
    "Home Projected Runs", "Published Source", "Snapshot Timestamp UTC",
    "Result Status", "Game Status", "Actual Winner", "Correct?", "Final Score",
    "Away Runs", "Home Runs", "Graded At UTC", "Result Source", "Notes",
]

HR_HEADERS = [
    "Prediction ID", "Date", "Model Version", "Rank", "Tier", "Confidence",
    "Player", "Team", "Opponent", "Opposing Pitcher", "Venue", "GamePk",
    "HomeAway", "Score", "Season HR", "Last7HR", "HardHit%", "100+MPH%",
    "FlyBall%", "PitcherVulnerability", "ParkFactor", "WeatherScore",
    "Published Source", "Snapshot Timestamp UTC", "Result Status", "Game Status",
    "Matched Player ID", "Plate Appearances", "Home Runs", "Hit HR?",
    "Graded At UTC", "Result Source", "Notes",
]

PROP_HEADERS = [
    "Prediction ID", "Date", "Model Version", "Overall Rank", "Report Rank",
    "Report Section", "Player Type", "Player ID", "Player", "Team", "Opponent",
    "GamePk", "Game", "HomeAway", "Venue", "Prop Type", "Threshold",
    "Recommended Prop", "Projected Probability", "Probability Gate", "Projected Mean",
    "Prop Score", "Confidence", "Opposing Pitcher", "Projected PA/IP", "ParkFactor",
    "WeatherScore", "Published Source", "Snapshot Timestamp UTC", "Result Status",
    "Game Status", "Matched Player ID", "Plate Appearances", "At Bats", "Hits",
    "Total Bases", "RBIs", "Innings Pitched", "Strikeouts", "Actual Value",
    "Hit Prop?", "Graded At UTC", "Result Source", "Notes",
]

BEST_CARD_HEADERS = [
    "Prediction ID", "Date", "Model Version", "Card Rank", "GamePk", "Game",
    "Venue", "Projected Winner", "Win Probability", "HR Player", "HR Team",
    "HR Rank", "HR Candidate Source", "Prop 1 Player", "Prop 1 Type",
    "Prop 1 Threshold", "Prop 1 Pick", "Prop 1 Prediction ID", "Prop 2 Player",
    "Prop 2 Type", "Prop 2 Threshold", "Prop 2 Pick", "Prop 2 Prediction ID",
    "Stack Score", "Published Source", "Snapshot Timestamp UTC", "Result Status",
    "Game Status", "Actual Winner", "Winner Correct?", "Final Score",
    "HR Component Status", "HR Matched Player ID", "HR Plate Appearances",
    "HR Home Runs", "HR Hit?", "Prop 1 Component Status", "Prop 1 Matched Player ID",
    "Prop 1 Plate Appearances", "Prop 1 At Bats", "Prop 1 Hits",
    "Prop 1 Total Bases", "Prop 1 RBIs", "Prop 1 Innings Pitched",
    "Prop 1 Strikeouts", "Prop 1 Actual Value", "Prop 1 Hit?",
    "Prop 2 Component Status", "Prop 2 Matched Player ID", "Prop 2 Plate Appearances",
    "Prop 2 At Bats", "Prop 2 Hits", "Prop 2 Total Bases", "Prop 2 RBIs",
    "Prop 2 Innings Pitched", "Prop 2 Strikeouts", "Prop 2 Actual Value",
    "Prop 2 Hit?", "Components Graded", "Components Hit", "Perfect Stack?",
    "Graded At UTC", "Result Source", "Notes",
]

RESULT_SOURCE = "MLB Stats API live game feed"


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def target_date_text(override: str = "") -> str:
    if override:
        datetime.strptime(override, "%Y-%m-%d")
        return override
    env_override = os.environ.get("MLB_SCHEDULE_DATE", "").strip()
    if env_override:
        datetime.strptime(env_override, "%Y-%m-%d")
        return env_override
    return datetime.now(ZoneInfo(MODEL_TIMEZONE)).date().isoformat()


def auth_google():
    import gspread
    from google.oauth2.service_account import Credentials

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw and os.path.exists("service_account.json"):
        with open("service_account.json", "r", encoding="utf-8") as stream:
            raw = stream.read()
    if not raw:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON secret.")
    credentials = Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    return gspread.authorize(credentials)


def clean(value: Any) -> Any:
    if value is None:
        return ""
    return value


def normalized_id_piece(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def first_header_index(headers: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for index, header in enumerate(headers):
        if header and header not in result:
            result[header] = index
    return result


def rows_as_records(values: list[list[str]]) -> list[dict[str, str]]:
    if not values:
        return []
    index = first_header_index(values[0])
    records = []
    for row in values[1:]:
        record = {
            header: row[column] if column < len(row) else ""
            for header, column in index.items()
        }
        if any(str(value).strip() for value in record.values()):
            records.append(record)
    return records


def quota_retry(operation, attempts: int = 5):
    """Retry temporary Google Sheets per-minute quota responses."""
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as error:
            message = str(error).lower()
            is_quota = "429" in message or "quota exceeded" in message
            if not is_quota or attempt + 1 >= attempts:
                raise
            time.sleep(15 * (attempt + 1))


def worksheet_by_title(workbook, title: str):
    """Reuse worksheet objects so gspread does not refetch sheet metadata."""
    cache = getattr(workbook, "_tracking_worksheet_cache", None)
    if cache is None:
        cache = {}
        setattr(workbook, "_tracking_worksheet_cache", cache)
    if title not in cache:
        cache[title] = quota_retry(lambda: workbook.worksheet(title))
    return cache[title]


def get_or_create_sheet(workbook, title: str, headers: list[str], rows: int = 5000):
    try:
        worksheet = worksheet_by_title(workbook, title)
    except Exception as error:
        if error.__class__.__name__ != "WorksheetNotFound":
            raise
        worksheet = quota_retry(
            lambda: workbook.add_worksheet(title=title, rows=rows, cols=len(headers))
        )
        workbook._tracking_worksheet_cache[title] = worksheet
        quota_retry(
            lambda: worksheet.update(
                values=[headers], range_name=f"A1:{column_letter(len(headers))}1"
            )
        )
        return worksheet

    verified = getattr(workbook, "_tracking_verified_headers", None)
    if verified is None:
        verified = set()
        setattr(workbook, "_tracking_verified_headers", verified)
    if title in verified:
        return worksheet
    existing = quota_retry(lambda: worksheet.row_values(1))
    if not existing:
        quota_retry(
            lambda: worksheet.update(
                values=[headers], range_name=f"A1:{column_letter(len(headers))}1"
            )
        )
    elif existing[: len(headers)] != headers:
        raise RuntimeError(
            f"Refusing to write {title}: existing header does not match the tracking schema."
        )
    verified.add(title)
    return worksheet


def column_letter(number: int) -> str:
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def existing_prediction_ids(worksheet) -> set[str]:
    values = quota_retry(lambda: worksheet.col_values(1))
    return {str(value).strip() for value in values[1:] if str(value).strip()}


def append_new_rows(worksheet, headers: list[str], records: Iterable[dict[str, Any]]) -> int:
    existing = existing_prediction_ids(worksheet)
    rows = []
    for record in records:
        prediction_id = str(record.get("Prediction ID", "")).strip()
        if not prediction_id or prediction_id in existing:
            continue
        rows.append([clean(record.get(header, "")) for header in headers])
        existing.add(prediction_id)
    if rows:
        quota_retry(lambda: worksheet.append_rows(rows, value_input_option="USER_ENTERED"))
    return len(rows)


def summary_value(workbook, tab_name: str, label: str) -> str:
    worksheet = worksheet_by_title(workbook, tab_name)
    records = quota_retry(worksheet.get_all_values)
    for row in records:
        if row and row[0] == label:
            return row[1] if len(row) > 1 else ""
    return ""


def game_prediction_id(record: dict[str, str]) -> str:
    return "|".join(
        [
            "GAME",
            record.get("Date", ""),
            record.get("GamePk", ""),
            normalized_id_piece(record.get("Projected Winner", "")),
            normalized_id_piece(record.get("Model Version", "")),
        ]
    )


def hr_prediction_id(record: dict[str, str]) -> str:
    return "|".join(
        [
            "HR",
            record.get("Date", ""),
            record.get("GamePk", ""),
            normalized_id_piece(record.get("Player", "")),
            normalized_id_piece(record.get("Model Version", "")),
        ]
    )


def prop_prediction_id(record: dict[str, str]) -> str:
    existing = str(record.get("Prediction ID", "")).strip()
    if existing:
        return existing
    return "|".join(
        [
            "PROP",
            record.get("Date", ""),
            record.get("GamePk", ""),
            normalized_id_piece(record.get("Player", "")),
            normalized_id_piece(record.get("Prop Type", "")),
            str(as_int(record.get("Threshold"))),
            normalized_id_piece(record.get("Model Version", "")),
        ]
    )


def best_card_prediction_id(record: dict[str, str]) -> str:
    existing = str(record.get("Prediction ID", "")).strip()
    if existing:
        return existing
    return "|".join(
        [
            "BESTCARD",
            record.get("Date", ""),
            record.get("GamePk", ""),
            normalized_id_piece(record.get("Model Version", "")),
        ]
    )


def tier_from_rank(rank: int) -> str:
    if rank <= 3:
        return "Primary"
    if rank <= 6:
        return "Secondary"
    if rank <= 9:
        return "Longshot"
    return ""


def snapshot_game_picks(workbook, target_date: str) -> int:
    summary_date = summary_value(workbook, "Game Email Summary", "Schedule Date Used")
    if summary_date != target_date:
        raise RuntimeError(
            f"Game Email Summary is not fresh for {target_date}; found {summary_date or 'missing'}."
        )
    source = worksheet_by_title(workbook, GAME_SOURCE_TAB)
    records = rows_as_records(quota_retry(source.get_all_values))
    candidates = []
    for record in records:
        rank = as_int(record.get("Rank"))
        if record.get("Date") != target_date or not 1 <= rank <= 7:
            continue
        if str(record.get("Verified", "")).strip().lower() not in {"yes", "true", "verified"}:
            continue
        item = {header: "" for header in GAME_HEADERS}
        for header in GAME_HEADERS:
            if header in record:
                item[header] = record[header]
        item.update(
            {
                "Prediction ID": game_prediction_id(record),
                "Published Source": GAME_SOURCE_TAB,
                "Snapshot Timestamp UTC": utc_now_text(),
                "Result Status": "Pending",
            }
        )
        candidates.append(item)
    tracking = get_or_create_sheet(workbook, GAME_TRACKING_TAB, GAME_HEADERS)
    return append_new_rows(tracking, GAME_HEADERS, candidates)


def snapshot_hr_picks(workbook, target_date: str) -> int:
    summary_date = summary_value(workbook, "Email Summary", "Schedule Date Used")
    published_version = summary_value(workbook, "Email Summary", "Model Version")
    if summary_date != target_date:
        raise RuntimeError(
            f"Email Summary is not fresh for {target_date}; found {summary_date or 'missing'}."
        )
    source = worksheet_by_title(workbook, HR_SOURCE_TAB)
    records = rows_as_records(quota_retry(source.get_all_values))
    candidates_by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        rank = as_int(record.get("Rank"))
        if record.get("Date") != target_date or not 1 <= rank <= 9:
            continue
        if published_version and record.get("Model Version") != published_version:
            continue
        if not record.get("GamePk") or not record.get("Player"):
            continue
        item = {header: "" for header in HR_HEADERS}
        for header in HR_HEADERS:
            if header in record:
                item[header] = record[header]
        item.update(
            {
                "Tier": tier_from_rank(rank),
                "Prediction ID": hr_prediction_id(record),
                "Published Source": HR_SOURCE_TAB,
                "Snapshot Timestamp UTC": utc_now_text(),
                "Result Status": "Pending",
            }
        )
        candidates_by_id[item["Prediction ID"]] = item
    tracking = get_or_create_sheet(workbook, HR_TRACKING_TAB, HR_HEADERS)
    return append_new_rows(tracking, HR_HEADERS, candidates_by_id.values())


def snapshot_player_props(workbook, target_date: str) -> int:
    summary_date = summary_value(workbook, "Player Props Email Summary", "Schedule Date Used")
    published_version = summary_value(workbook, "Player Props Email Summary", "Model Version")
    if summary_date != target_date:
        raise RuntimeError(
            f"Player Props Email Summary is not fresh for {target_date}; "
            f"found {summary_date or 'missing'}."
        )
    source = worksheet_by_title(workbook, PROP_SOURCE_TAB)
    records = rows_as_records(quota_retry(source.get_all_values))
    candidates = []
    for record in records:
        report_rank = as_int(record.get("Report Rank"))
        if record.get("Date") != target_date or not 1 <= report_rank <= 20:
            continue
        if published_version and record.get("Model Version") != published_version:
            continue
        if not record.get("GamePk") or not record.get("Player") or not record.get("Prop Type"):
            continue
        if as_int(record.get("Threshold")) <= 0:
            continue
        item = {header: "" for header in PROP_HEADERS}
        for header in PROP_HEADERS:
            if header in record:
                item[header] = record[header]
        item.update(
            {
                "Prediction ID": prop_prediction_id(record),
                "Published Source": PROP_SOURCE_TAB,
                "Snapshot Timestamp UTC": utc_now_text(),
                "Result Status": "Pending",
            }
        )
        candidates.append(item)
    tracking = get_or_create_sheet(workbook, PROP_TRACKING_TAB, PROP_HEADERS, rows=10000)
    return append_new_rows(tracking, PROP_HEADERS, candidates)


def snapshot_best_card(workbook, target_date: str) -> int:
    summary_date = summary_value(workbook, "Best Card Email Summary", "Schedule Date Used")
    published_version = summary_value(workbook, "Best Card Email Summary", "Model Version")
    if summary_date != target_date:
        raise RuntimeError(
            f"Best Card Email Summary is not fresh for {target_date}; "
            f"found {summary_date or 'missing'}."
        )
    source = worksheet_by_title(workbook, BEST_CARD_SOURCE_TAB)
    records = rows_as_records(quota_retry(source.get_all_values))
    candidates = []
    for record in records:
        card_rank = as_int(record.get("Card Rank"))
        if record.get("Date") != target_date or not 1 <= card_rank <= 3:
            continue
        if published_version and record.get("Model Version") != published_version:
            continue
        if not record.get("GamePk") or not record.get("HR Player"):
            continue
        if not record.get("Prop 1 Player") or not record.get("Prop 2 Player"):
            continue
        item = {header: "" for header in BEST_CARD_HEADERS}
        for header in BEST_CARD_HEADERS:
            if header in record:
                item[header] = record[header]
        item.update(
            {
                "Prediction ID": best_card_prediction_id(record),
                "Published Source": BEST_CARD_SOURCE_TAB,
                "Snapshot Timestamp UTC": utc_now_text(),
                "Result Status": "Pending",
            }
        )
        candidates.append(item)
    if len(candidates) > 3:
        raise RuntimeError(
            f"Best Card snapshot found {len(candidates)} published stacks for {target_date}; at most 3 allowed."
        )
    tracking = get_or_create_sheet(workbook, BEST_CARD_TRACKING_TAB, BEST_CARD_HEADERS, rows=5000)
    return append_new_rows(tracking, BEST_CARD_HEADERS, candidates)



def snapshot_best_card_if_available(workbook, target_date: str) -> tuple[int, str]:
    """Snapshot Best Card without blocking independent models when it is delayed."""
    try:
        return snapshot_best_card(workbook, target_date), ""
    except RuntimeError as error:
        message = str(error)
        recoverable = (
            "Best Card Email Summary is not fresh" in message
            or "Best Card snapshot found" in message
        )
        if not recoverable:
            raise
        warning = f"Best Card snapshot deferred: {message}"
        print(f"WARNING: {warning}")
        return 0, warning

def fetch_game_feed(game_pk: str, attempts: int = 3) -> dict[str, Any]:
    import requests

    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = requests.get(MLB_FEED_URL.format(game_pk=game_pk), timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as error:  # network/schema failures remain visible in the run log
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(1 + attempt)
    raise RuntimeError(f"MLB feed failed for gamePk {game_pk}: {last_error}")


def game_status(feed: dict[str, Any]) -> tuple[str, str, str]:
    status = feed.get("gameData", {}).get("status", {}) or {}
    abstract = str(status.get("abstractGameState", ""))
    coded = str(status.get("codedGameState", ""))
    detailed = str(status.get("detailedState", ""))
    return abstract, coded, detailed


def is_final(feed: dict[str, Any]) -> bool:
    abstract, coded, detailed = game_status(feed)
    return abstract.lower() == "final" or coded.upper() == "F" or detailed.lower() == "final"


def is_void(feed: dict[str, Any]) -> bool:
    _, _, detailed = game_status(feed)
    text = detailed.lower()
    return "cancel" in text or "postpon" in text or "forfeit" in text


def final_score(feed: dict[str, Any]) -> tuple[str, str, int, int, str]:
    data = feed.get("gameData", {})
    teams = data.get("teams", {})
    linescore = feed.get("liveData", {}).get("linescore", {})
    score_teams = linescore.get("teams", {})
    away_name = str(teams.get("away", {}).get("abbreviation") or teams.get("away", {}).get("name", ""))
    home_name = str(teams.get("home", {}).get("abbreviation") or teams.get("home", {}).get("name", ""))
    away_runs = as_int(score_teams.get("away", {}).get("runs"))
    home_runs = as_int(score_teams.get("home", {}).get("runs"))
    winner = away_name if away_runs > home_runs else home_name
    return away_name, home_name, away_runs, home_runs, winner


def find_player_boxscore(feed: dict[str, Any], full_name: str, home_away: str = ""):
    target = normalized_id_piece(full_name)
    teams = feed.get("liveData", {}).get("boxscore", {}).get("teams", {})
    sides = [home_away.lower()] if home_away.lower() in {"home", "away"} else ["away", "home"]
    matches = []
    for side in sides:
        for player in (teams.get(side, {}).get("players", {}) or {}).values():
            person = player.get("person", {}) or {}
            if normalized_id_piece(person.get("fullName", "")) == target:
                matches.append(player)
    if len(matches) == 1:
        return matches[0]
    return None


def find_player_boxscore_by_id(feed: dict[str, Any], player_id: Any, home_away: str = ""):
    target_id = as_int(player_id)
    if target_id <= 0:
        return None
    teams = feed.get("liveData", {}).get("boxscore", {}).get("teams", {})
    sides = [home_away.lower()] if home_away.lower() in {"home", "away"} else ["away", "home"]
    matches = []
    for side in sides:
        for player in (teams.get(side, {}).get("players", {}) or {}).values():
            person = player.get("person", {}) or {}
            if as_int(person.get("id")) == target_id:
                matches.append(player)
    return matches[0] if len(matches) == 1 else None


def total_bases_from_batting(batting: dict[str, Any]) -> int:
    if str(batting.get("totalBases", "")).strip() != "":
        return as_int(batting.get("totalBases"))
    hits = as_int(batting.get("hits"))
    doubles = as_int(batting.get("doubles"))
    triples = as_int(batting.get("triples"))
    home_runs = as_int(batting.get("homeRuns"))
    return hits + doubles + 2 * triples + 3 * home_runs


def player_prop_boxscore_values(player: dict[str, Any], prop_type: str) -> dict[str, Any]:
    stats = player.get("stats", {}) or {}
    batting = stats.get("batting", {}) or {}
    pitching = stats.get("pitching", {}) or {}
    plate_appearances = as_int(batting.get("plateAppearances"), as_int(batting.get("atBats")))
    at_bats = as_int(batting.get("atBats"))
    hits = as_int(batting.get("hits"))
    total_bases = total_bases_from_batting(batting)
    rbis = as_int(batting.get("rbi"))
    innings_pitched = str(pitching.get("inningsPitched", "0") or "0")
    strikeouts = as_int(pitching.get("strikeOuts"))
    normalized_type = normalized_id_piece(prop_type)
    actual_by_type = {
        "hits": hits,
        "totalbases": total_bases,
        "rbis": rbis,
        "strikeouts": strikeouts,
    }
    if normalized_type not in actual_by_type:
        raise ValueError(f"Unsupported Player Props type: {prop_type}")
    is_pitching_prop = normalized_type == "strikeouts"
    appeared = as_baseball_innings(innings_pitched) > 0 if is_pitching_prop else plate_appearances > 0
    return {
        "Plate Appearances": plate_appearances,
        "At Bats": at_bats,
        "Hits": hits,
        "Total Bases": total_bases,
        "RBIs": rbis,
        "Innings Pitched": innings_pitched,
        "Strikeouts": strikeouts,
        "Actual Value": actual_by_type[normalized_type],
        "Appeared": appeared,
    }


def as_baseball_innings(value: Any) -> float:
    text = str(value or "0").strip()
    if "." not in text:
        try:
            return float(text)
        except ValueError:
            return 0.0
    whole, fraction = text.split(".", 1)
    try:
        innings = int(whole)
    except ValueError:
        return 0.0
    outs = int(fraction[:1]) if fraction[:1].isdigit() else 0
    return innings + (outs / 3.0 if outs in {0, 1, 2} else 0.0)


def update_result_fields(
    worksheet,
    headers: list[str],
    row_number: int,
    updates: dict[str, Any],
    record: dict[str, Any] | None = None,
):
    indices = {header: index + 1 for index, header in enumerate(headers)}
    current = [clean((record or {}).get(header, "")) for header in headers]
    for header, value in updates.items():
        current[indices[header] - 1] = clean(value)
    first_column = min(indices[header] for header in updates)
    last_column = max(indices[header] for header in updates)
    quota_retry(
        lambda: worksheet.update(
            values=[current[first_column - 1:last_column]],
            range_name=f"{column_letter(first_column)}{row_number}:{column_letter(last_column)}{row_number}",
        )
    )


def update_game_status_if_changed(
    worksheet, headers: list[str], row_number: int, record: dict[str, Any], detailed: str
) -> bool:
    """Avoid consuming a write request when a pending game's status is unchanged."""
    if str(record.get("Game Status", "")) == str(detailed):
        return False
    update_result_fields(
        worksheet, headers, row_number, {"Game Status": detailed}, record
    )
    return True


def cached_feed(feed_cache: dict[str, dict[str, Any]], game_pk: str) -> dict[str, Any]:
    if game_pk not in feed_cache:
        feed_cache[game_pk] = fetch_game_feed(game_pk)
    return feed_cache[game_pk]


def grade_game_rows(workbook, feed_cache: dict[str, dict[str, Any]]) -> int:
    worksheet = get_or_create_sheet(workbook, GAME_TRACKING_TAB, GAME_HEADERS)
    values = quota_retry(worksheet.get_all_values)
    records = rows_as_records(values)
    graded = 0
    for offset, record in enumerate(records, start=2):
        if record.get("Result Status") not in {"", "Pending", "Error"}:
            continue
        game_pk = str(record.get("GamePk", "")).strip()
        if not game_pk:
            continue
        feed = cached_feed(feed_cache, game_pk)
        _, _, detailed = game_status(feed)
        if is_void(feed):
            update_result_fields(worksheet, GAME_HEADERS, offset, {
                "Result Status": "Void", "Game Status": detailed,
                "Graded At UTC": utc_now_text(), "Result Source": RESULT_SOURCE,
            }, record)
            graded += 1
            continue
        if not is_final(feed):
            update_game_status_if_changed(worksheet, GAME_HEADERS, offset, record, detailed)
            continue
        away, home, away_runs, home_runs, winner = final_score(feed)
        projected = normalized_id_piece(record.get("Projected Winner", ""))
        correct = "Yes" if projected == normalized_id_piece(winner) else "No"
        update_result_fields(worksheet, GAME_HEADERS, offset, {
            "Result Status": "Final", "Game Status": detailed, "Actual Winner": winner,
            "Correct?": correct, "Final Score": f"{away} {away_runs} - {home} {home_runs}",
            "Away Runs": away_runs, "Home Runs": home_runs, "Graded At UTC": utc_now_text(),
            "Result Source": RESULT_SOURCE,
        }, record)
        graded += 1
    return graded


def grade_hr_rows(workbook, feed_cache: dict[str, dict[str, Any]]) -> int:
    worksheet = get_or_create_sheet(workbook, HR_TRACKING_TAB, HR_HEADERS)
    values = quota_retry(worksheet.get_all_values)
    records = rows_as_records(values)
    graded = 0
    for offset, record in enumerate(records, start=2):
        if record.get("Result Status") not in {"", "Pending", "Error"}:
            continue
        game_pk = str(record.get("GamePk", "")).strip()
        if not game_pk:
            continue
        feed = cached_feed(feed_cache, game_pk)
        _, _, detailed = game_status(feed)
        if is_void(feed):
            update_result_fields(worksheet, HR_HEADERS, offset, {
                "Result Status": "Void", "Game Status": detailed,
                "Graded At UTC": utc_now_text(), "Result Source": RESULT_SOURCE,
            }, record)
            graded += 1
            continue
        if not is_final(feed):
            update_game_status_if_changed(worksheet, HR_HEADERS, offset, record, detailed)
            continue
        player = find_player_boxscore(feed, record.get("Player", ""), record.get("HomeAway", ""))
        if not player:
            update_result_fields(worksheet, HR_HEADERS, offset, {
                "Result Status": "DNP/Unmatched", "Game Status": detailed,
                "Graded At UTC": utc_now_text(), "Result Source": RESULT_SOURCE,
                "Notes": "No unique player match in the official boxscore; not graded as a miss.",
            }, record)
            graded += 1
            continue
        person = player.get("person", {}) or {}
        batting = player.get("stats", {}).get("batting", {}) or {}
        plate_appearances = as_int(batting.get("plateAppearances"), as_int(batting.get("atBats")))
        home_runs = as_int(batting.get("homeRuns"))
        if plate_appearances <= 0:
            result_status, hit_hr = "DNP", ""
        else:
            result_status, hit_hr = "Final", "Yes" if home_runs >= 1 else "No"
        update_result_fields(worksheet, HR_HEADERS, offset, {
            "Result Status": result_status, "Game Status": detailed,
            "Matched Player ID": person.get("id", ""), "Plate Appearances": plate_appearances,
            "Home Runs": home_runs, "Hit HR?": hit_hr, "Graded At UTC": utc_now_text(),
            "Result Source": RESULT_SOURCE,
        }, record)
        graded += 1
    return graded


def grade_player_prop_rows(workbook, feed_cache: dict[str, dict[str, Any]]) -> int:
    worksheet = get_or_create_sheet(workbook, PROP_TRACKING_TAB, PROP_HEADERS, rows=10000)
    records = rows_as_records(quota_retry(worksheet.get_all_values))
    graded = 0
    for offset, record in enumerate(records, start=2):
        if record.get("Result Status") not in {"", "Pending", "Error"}:
            continue
        game_pk = str(record.get("GamePk", "")).strip()
        if not game_pk:
            continue
        feed = cached_feed(feed_cache, game_pk)
        _, _, detailed = game_status(feed)
        if is_void(feed):
            update_result_fields(worksheet, PROP_HEADERS, offset, {
                "Result Status": "Void", "Game Status": detailed,
                "Graded At UTC": utc_now_text(), "Result Source": RESULT_SOURCE,
            }, record)
            graded += 1
            continue
        if not is_final(feed):
            update_game_status_if_changed(worksheet, PROP_HEADERS, offset, record, detailed)
            continue
        player = find_player_boxscore_by_id(feed, record.get("Player ID"), record.get("HomeAway", ""))
        if not player:
            player = find_player_boxscore(feed, record.get("Player", ""), record.get("HomeAway", ""))
        if not player:
            update_result_fields(worksheet, PROP_HEADERS, offset, {
                "Result Status": "DNP/Unmatched", "Game Status": detailed,
                "Graded At UTC": utc_now_text(), "Result Source": RESULT_SOURCE,
                "Notes": "No unique player match in the official boxscore; not graded as a miss.",
            }, record)
            graded += 1
            continue
        try:
            values = player_prop_boxscore_values(player, record.get("Prop Type", ""))
        except ValueError as error:
            update_result_fields(worksheet, PROP_HEADERS, offset, {
                "Result Status": "Error", "Game Status": detailed,
                "Graded At UTC": utc_now_text(), "Result Source": RESULT_SOURCE,
                "Notes": str(error),
            }, record)
            graded += 1
            continue
        person = player.get("person", {}) or {}
        if not values.pop("Appeared"):
            result_status, hit_prop = "DNP", ""
        else:
            result_status = "Final"
            hit_prop = "Yes" if as_int(values["Actual Value"]) >= as_int(record.get("Threshold")) else "No"
        update_result_fields(worksheet, PROP_HEADERS, offset, {
            "Result Status": result_status, "Game Status": detailed,
            "Matched Player ID": person.get("id", ""), **values, "Hit Prop?": hit_prop,
            "Graded At UTC": utc_now_text(), "Result Source": RESULT_SOURCE,
        }, record)
        graded += 1
    return graded


def grade_hr_component(feed: dict[str, Any], player_name: str) -> dict[str, Any]:
    player = find_player_boxscore(feed, player_name)
    if not player:
        return {
            "Status": "DNP/Unmatched", "Matched Player ID": "",
            "Plate Appearances": "", "Home Runs": "", "Hit?": "",
        }
    person = player.get("person", {}) or {}
    batting = player.get("stats", {}).get("batting", {}) or {}
    plate_appearances = as_int(batting.get("plateAppearances"), as_int(batting.get("atBats")))
    home_runs = as_int(batting.get("homeRuns"))
    if plate_appearances <= 0:
        status, hit = "DNP", ""
    else:
        status, hit = "Final", "Yes" if home_runs >= 1 else "No"
    return {
        "Status": status, "Matched Player ID": person.get("id", ""),
        "Plate Appearances": plate_appearances, "Home Runs": home_runs, "Hit?": hit,
    }


def grade_prop_component(
    feed: dict[str, Any], player_name: str, prop_type: str, threshold: Any
) -> dict[str, Any]:
    player = find_player_boxscore(feed, player_name)
    if not player:
        return {
            "Status": "DNP/Unmatched", "Matched Player ID": "", "Hit?": "",
        }
    person = player.get("person", {}) or {}
    try:
        values = player_prop_boxscore_values(player, prop_type)
    except ValueError as error:
        return {
            "Status": "Error", "Matched Player ID": person.get("id", ""),
            "Hit?": "", "Notes": str(error),
        }
    appeared = values.pop("Appeared")
    if not appeared:
        status, hit = "DNP", ""
    else:
        status = "Final"
        hit = "Yes" if as_int(values["Actual Value"]) >= as_int(threshold) else "No"
    return {
        "Status": status, "Matched Player ID": person.get("id", ""),
        **values, "Hit?": hit,
    }


def grade_best_card_rows(workbook, feed_cache: dict[str, dict[str, Any]]) -> int:
    worksheet = get_or_create_sheet(
        workbook, BEST_CARD_TRACKING_TAB, BEST_CARD_HEADERS, rows=5000
    )
    records = rows_as_records(quota_retry(worksheet.get_all_values))
    graded = 0
    for offset, record in enumerate(records, start=2):
        if record.get("Result Status") not in {"", "Pending", "Error"}:
            continue
        game_pk = str(record.get("GamePk", "")).strip()
        if not game_pk:
            continue
        feed = cached_feed(feed_cache, game_pk)
        _, _, detailed = game_status(feed)
        if is_void(feed):
            update_result_fields(worksheet, BEST_CARD_HEADERS, offset, {
                "Result Status": "Void", "Game Status": detailed,
                "Graded At UTC": utc_now_text(), "Result Source": RESULT_SOURCE,
            }, record)
            graded += 1
            continue
        if not is_final(feed):
            update_game_status_if_changed(
                worksheet, BEST_CARD_HEADERS, offset, record, detailed
            )
            continue

        away, home, away_runs, home_runs, winner = final_score(feed)
        winner_hit = (
            "Yes" if normalized_id_piece(record.get("Projected Winner", ""))
            == normalized_id_piece(winner) else "No"
        )
        hr = grade_hr_component(feed, record.get("HR Player", ""))
        prop_one = grade_prop_component(
            feed, record.get("Prop 1 Player", ""), record.get("Prop 1 Type", ""),
            record.get("Prop 1 Threshold"),
        )
        prop_two = grade_prop_component(
            feed, record.get("Prop 2 Player", ""), record.get("Prop 2 Type", ""),
            record.get("Prop 2 Threshold"),
        )
        component_results = [winner_hit, hr.get("Hit?", ""), prop_one.get("Hit?", ""), prop_two.get("Hit?", "")]
        components_graded = sum(value in {"Yes", "No"} for value in component_results)
        components_hit = sum(value == "Yes" for value in component_results)
        component_statuses = [hr.get("Status"), prop_one.get("Status"), prop_two.get("Status")]
        if components_graded == 4:
            result_status = "Final"
            perfect = "Yes" if components_hit == 4 else "No"
        elif "DNP/Unmatched" in component_statuses:
            result_status, perfect = "DNP/Unmatched", ""
        elif "DNP" in component_statuses:
            result_status, perfect = "DNP", ""
        else:
            result_status, perfect = "Error", ""
        notes = "; ".join(
            value for value in [prop_one.get("Notes", ""), prop_two.get("Notes", "")] if value
        )
        update_result_fields(worksheet, BEST_CARD_HEADERS, offset, {
            "Result Status": result_status, "Game Status": detailed,
            "Actual Winner": winner, "Winner Correct?": winner_hit,
            "Final Score": f"{away} {away_runs} - {home} {home_runs}",
            "HR Component Status": hr.get("Status", ""),
            "HR Matched Player ID": hr.get("Matched Player ID", ""),
            "HR Plate Appearances": hr.get("Plate Appearances", ""),
            "HR Home Runs": hr.get("Home Runs", ""), "HR Hit?": hr.get("Hit?", ""),
            "Prop 1 Component Status": prop_one.get("Status", ""),
            "Prop 1 Matched Player ID": prop_one.get("Matched Player ID", ""),
            "Prop 1 Plate Appearances": prop_one.get("Plate Appearances", ""),
            "Prop 1 At Bats": prop_one.get("At Bats", ""),
            "Prop 1 Hits": prop_one.get("Hits", ""),
            "Prop 1 Total Bases": prop_one.get("Total Bases", ""),
            "Prop 1 RBIs": prop_one.get("RBIs", ""),
            "Prop 1 Innings Pitched": prop_one.get("Innings Pitched", ""),
            "Prop 1 Strikeouts": prop_one.get("Strikeouts", ""),
            "Prop 1 Actual Value": prop_one.get("Actual Value", ""),
            "Prop 1 Hit?": prop_one.get("Hit?", ""),
            "Prop 2 Component Status": prop_two.get("Status", ""),
            "Prop 2 Matched Player ID": prop_two.get("Matched Player ID", ""),
            "Prop 2 Plate Appearances": prop_two.get("Plate Appearances", ""),
            "Prop 2 At Bats": prop_two.get("At Bats", ""),
            "Prop 2 Hits": prop_two.get("Hits", ""),
            "Prop 2 Total Bases": prop_two.get("Total Bases", ""),
            "Prop 2 RBIs": prop_two.get("RBIs", ""),
            "Prop 2 Innings Pitched": prop_two.get("Innings Pitched", ""),
            "Prop 2 Strikeouts": prop_two.get("Strikeouts", ""),
            "Prop 2 Actual Value": prop_two.get("Actual Value", ""),
            "Prop 2 Hit?": prop_two.get("Hit?", ""),
            "Components Graded": components_graded, "Components Hit": components_hit,
            "Perfect Stack?": perfect, "Graded At UTC": utc_now_text(),
            "Result Source": RESULT_SOURCE, "Notes": notes,
        }, record)
        graded += 1
    return graded


def performance_rows(workbook) -> list[list[Any]]:
    rows: list[list[Any]] = [["Model", "Segment", "Graded", "Correct/Hits", "Accuracy", "Pending", "Void/DNP", "Last Updated UTC"]]
    for model, tab, result_field, success_value in [
        ("Game Picks", GAME_TRACKING_TAB, "Correct?", "Yes"),
        ("HR Picks", HR_TRACKING_TAB, "Hit HR?", "Yes"),
        ("Player Props", PROP_TRACKING_TAB, "Hit Prop?", "Yes"),
    ]:
        try:
            worksheet = worksheet_by_title(workbook, tab)
            records = rows_as_records(quota_retry(worksheet.get_all_values))
        except Exception as error:
            if error.__class__.__name__ != "WorksheetNotFound":
                raise
            records = []
        segments = {"Overall": records}
        if model == "Game Picks":
            for label in sorted({r.get("Confidence", "") for r in records if r.get("Confidence")}):
                segments[label] = [r for r in records if r.get("Confidence") == label]
        elif model == "HR Picks":
            for label in ["Primary", "Secondary", "Longshot"]:
                segments[label] = [r for r in records if r.get("Tier") == label]
        else:
            for label in ["Hits", "Total Bases", "RBIs", "Strikeouts"]:
                segments[label] = [r for r in records if r.get("Prop Type") == label]
            for label in ["Top Props", "Watchlist"]:
                segments[label] = [r for r in records if r.get("Report Section") == label]
        for segment, segment_rows in segments.items():
            graded_rows = [r for r in segment_rows if r.get(result_field) in {"Yes", "No"}]
            successes = sum(r.get(result_field) == success_value for r in graded_rows)
            pending = sum(r.get("Result Status") in {"", "Pending", "Error"} for r in segment_rows)
            excluded = sum(r.get("Result Status") in {"Void", "DNP", "DNP/Unmatched"} for r in segment_rows)
            accuracy = f"{successes / len(graded_rows):.1%}" if graded_rows else ""
            rows.append([model, segment, len(graded_rows), successes, accuracy, pending, excluded, utc_now_text()])

    try:
        worksheet = worksheet_by_title(workbook, BEST_CARD_TRACKING_TAB)
        best_card_records = rows_as_records(quota_retry(worksheet.get_all_values))
    except Exception as error:
        if error.__class__.__name__ != "WorksheetNotFound":
            raise
        best_card_records = []

    # Same-day model refreshes can archive more than one version of a card.
    # Performance must count only the newest version for each date/card rank.
    latest_best_cards: dict[tuple[str, int], dict[str, str]] = {}
    for row in best_card_records:
        card_date = str(row.get("Date", ""))[:10]
        card_rank = as_int(row.get("Card Rank"), 0)
        if not card_date or card_rank not in {1, 2, 3}:
            continue
        key = (card_date, card_rank)
        current = latest_best_cards.get(key)
        if current is None or str(row.get("Snapshot Timestamp UTC", "")) >= str(
            current.get("Snapshot Timestamp UTC", "")
        ):
            latest_best_cards[key] = row
    best_card_records = list(latest_best_cards.values())

    best_card_segments = [
        ("Complete Stacks", "Perfect Stack?"),
        ("Game Winners", "Winner Correct?"),
        ("Home Runs", "HR Hit?"),
        ("Player Prop 1", "Prop 1 Hit?"),
        ("Player Prop 2", "Prop 2 Hit?"),
    ]
    for segment, result_field in best_card_segments:
        graded_rows = [r for r in best_card_records if r.get(result_field) in {"Yes", "No"}]
        successes = sum(r.get(result_field) == "Yes" for r in graded_rows)
        pending = sum(r.get("Result Status") in {"", "Pending", "Error"} for r in best_card_records)
        excluded = sum(
            r.get("Result Status") in {"Void", "DNP", "DNP/Unmatched"}
            for r in best_card_records
        )
        accuracy = f"{successes / len(graded_rows):.1%}" if graded_rows else ""
        rows.append([
            "Best Card", segment, len(graded_rows), successes, accuracy, pending,
            excluded, utc_now_text(),
        ])
    return rows


def refresh_performance(workbook):
    rows = performance_rows(workbook)
    headers = rows[0]
    worksheet = get_or_create_sheet(workbook, PERFORMANCE_TAB, headers, rows=500)
    quota_retry(worksheet.clear)
    quota_retry(lambda: worksheet.update(values=rows, range_name=f"A1:H{len(rows)}"))


def refresh_best_card_results_email(workbook, target_date: str):
    """Publish the prior slate's graded Best Card in an email-friendly table."""
    result_date = (datetime.strptime(target_date, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
    tracking = get_or_create_sheet(
        workbook, BEST_CARD_TRACKING_TAB, BEST_CARD_HEADERS, rows=5000
    )
    records = [
        row for row in rows_as_records(quota_retry(tracking.get_all_values))
        if str(row.get("Date", ""))[:10] == result_date
    ]

    # A card can be snapshotted more than once as same-day model outputs refresh.
    # Keep only the newest published version of each card rank for the email.
    latest_by_card: dict[int, dict[str, str]] = {}
    for row in records:
        card_rank = as_int(row.get("Card Rank"), 0)
        if card_rank not in {1, 2, 3}:
            continue
        current = latest_by_card.get(card_rank)
        if current is None or str(row.get("Snapshot Timestamp UTC", "")) >= str(
            current.get("Snapshot Timestamp UTC", "")
        ):
            latest_by_card[card_rank] = row
    records = [latest_by_card[rank] for rank in sorted(latest_by_card)]

    headers = [
        "Result Date", "Card", "Game", "Final Score", "Component",
        "Selection", "Actual", "Result", "Status",
    ]
    output: list[list[Any]] = []
    for row in records:
        card = row.get("Card Rank", "")
        game = row.get("Game", "")
        score = row.get("Final Score", "")
        status = row.get("Result Status", "Pending") or "Pending"
        output.extend([
            [result_date, card, game, score, "Game Winner", row.get("Projected Winner", ""),
             row.get("Actual Winner", ""), row.get("Winner Correct?", ""), status],
            [result_date, card, game, score, "Home Run", row.get("HR Player", ""),
             row.get("HR Home Runs", ""), row.get("HR Hit?", ""), row.get("HR Component Status", status)],
            [result_date, card, game, score, row.get("Prop 1 Type", "Player Prop"),
             row.get("Prop 1 Pick", ""), row.get("Prop 1 Actual Value", ""),
             row.get("Prop 1 Hit?", ""), row.get("Prop 1 Component Status", status)],
            [result_date, card, game, score, row.get("Prop 2 Type", "Player Prop"),
             row.get("Prop 2 Pick", ""), row.get("Prop 2 Actual Value", ""),
             row.get("Prop 2 Hit?", ""), row.get("Prop 2 Component Status", status)],
        ])

    summary = get_or_create_sheet(workbook, BEST_CARD_RESULTS_EMAIL_TAB, headers, rows=100)
    quota_retry(summary.clear)
    values = [headers] + output
    quota_retry(lambda: summary.update(
        values=values, range_name=f"A1:I{max(1, len(values))}"
    ))


def append_run_log(workbook, mode: str, target_date: str, details: str, status: str):
    headers = ["Run Timestamp UTC", "Mode", "Target Date", "Status", "Details"]
    worksheet = get_or_create_sheet(workbook, RUN_LOG_TAB, headers, rows=2000)
    quota_retry(
        lambda: worksheet.append_row(
            [utc_now_text(), mode, target_date, status, details],
            value_input_option="USER_ENTERED",
        )
    )


def run(mode: str, target_date: str) -> dict[str, int]:
    client = auth_google()
    workbook = client.open(SHEET_NAME)
    counts = {
        "game_snapshots": 0, "hr_snapshots": 0, "prop_snapshots": 0,
        "best_card_snapshots": 0, "game_graded": 0, "hr_graded": 0,
        "prop_graded": 0, "best_card_graded": 0,
    }
    try:
        if mode in {"snapshot", "both"}:
            counts["game_snapshots"] = snapshot_game_picks(workbook, target_date)
            counts["hr_snapshots"] = snapshot_hr_picks(workbook, target_date)
            counts["prop_snapshots"] = snapshot_player_props(workbook, target_date)
            counts["best_card_snapshots"], best_card_warning = snapshot_best_card_if_available(
                workbook, target_date
            )
        else:
            best_card_warning = ""
        if mode in {"grade", "both"}:
            cache: dict[str, dict[str, Any]] = {}
            counts["game_graded"] = grade_game_rows(workbook, cache)
            counts["hr_graded"] = grade_hr_rows(workbook, cache)
            counts["prop_graded"] = grade_player_prop_rows(workbook, cache)
            counts["best_card_graded"] = grade_best_card_rows(workbook, cache)
        refresh_performance(workbook)
        refresh_best_card_results_email(workbook, target_date)
        details = json.dumps(counts, sort_keys=True)
        if best_card_warning:
            details += f" | {best_card_warning}"
        append_run_log(workbook, mode, target_date, details, "Completed with warning" if best_card_warning else "Completed")
        return counts
    except Exception as error:
        append_run_log(workbook, mode, target_date, str(error), "Failed")
        raise


def parse_args():
    parser = argparse.ArgumentParser(description="Archive and grade published MLB model picks.")
    parser.add_argument("--mode", choices=["snapshot", "grade", "both"], default="both")
    parser.add_argument("--date", default="", help="Optional YYYY-MM-DD snapshot date.")
    return parser.parse_args()


def main():
    args = parse_args()
    date_text = target_date_text(args.date)
    counts = run(args.mode, date_text)
    print(json.dumps({"target_date": date_text, "mode": args.mode, **counts}, sort_keys=True))


if __name__ == "__main__":
    main()
