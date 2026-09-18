"""Build exactly three same-game MLB stacks ranked with cleaned historical results."""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from best_card_math import composite_stack_score, number, select_distinct_props, top_complete_stacks


MODEL_VERSION = "Best Card V1.2 - Historical Hit-Rate Ranking"
MODEL_TIMEZONE = os.environ.get("MLB_SCHEDULE_TZ", "America/New_York")
DATE_OVERRIDE = os.environ.get("MLB_SCHEDULE_DATE", "").strip()
SHEET_NAME = os.environ.get("SHEET_NAME", "MLB Daily Model")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

GAME_SOURCE_TAB = "Game Picks"
HR_SOURCE_TAB = "Model Results"
PROP_SOURCE_TAB = "Player Props"
PROP_HISTORY_TAB = "Player Props Model Results"
BEST_CARD_TAB = "Best Card"
BEST_CARD_HISTORY_TAB = "Best Card Model Results"
BEST_CARD_EMAIL_TAB = "Best Card Email Summary"
BEST_CARD_INTEGRITY_TAB = "Best Card Integrity Log"
BEST_CARD_RUN_TAB = "Best Card Run Log"

PUBLISHED_GAME_LIMIT = 7
PUBLISHED_HR_LIMIT = 9
EXTENDED_HR_LIMIT = 30
PUBLISHED_PROP_LIMIT = 20

HISTORICAL_HR_RATES = {
    "Published HR Target": 22.4,
    "Extended HR Candidate": 12.5,
}
HISTORICAL_PROP_RATES = {
    "Hits": 70.8,
    "RBIs": 38.9,
    "Strikeouts": 18.2,
}


def resolve_date():
    if DATE_OVERRIDE:
        return datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d").date(), "Environment override MLB_SCHEDULE_DATE"
    now = datetime.now(ZoneInfo(MODEL_TIMEZONE))
    if now.hour >= 18:
        return now.date() + timedelta(days=1), f"{MODEL_TIMEZONE} evening run; using next MLB slate"
    return now.date(), f"Official MLB slate date from {MODEL_TIMEZONE}"


TODAY, DATE_LOGIC = resolve_date()
RUN_UTC = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
RUN_LOCAL = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")


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


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def date_text(value: Any) -> str:
    return clean_text(value)[:10]


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def normalized_game_pk(value: Any) -> str:
    parsed = as_int(value)
    return str(parsed) if parsed > 0 else ""


def rows_as_records(values: list[list[str]]) -> list[dict[str, str]]:
    if not values:
        return []
    header_index = {}
    for index, header in enumerate(values[0]):
        if header and header not in header_index:
            header_index[header] = index
    records = []
    for row in values[1:]:
        record = {
            header: row[index] if index < len(row) else ""
            for header, index in header_index.items()
        }
        if any(clean_text(value) for value in record.values()):
            records.append(record)
    return records


def summary_map(workbook, tab_name: str) -> dict[str, str]:
    return {
        row[0]: row[1] if len(row) > 1 else ""
        for row in workbook.worksheet(tab_name).get_all_values()
        if row and row[0]
    }


def require_fresh(summary: dict[str, str], label: str):
    found = clean_text(summary.get("Schedule Date Used"))
    if found != TODAY.isoformat():
        raise RuntimeError(f"{label} is not fresh for {TODAY.isoformat()}; found {found or 'missing'}.")


def verified(value: Any) -> bool:
    return clean_text(value).lower() in {"yes", "true", "verified"}


def load_inputs(workbook):
    game_summary = summary_map(workbook, "Game Email Summary")
    hr_summary = summary_map(workbook, "Email Summary")
    prop_summary = summary_map(workbook, "Player Props Email Summary")
    require_fresh(game_summary, "Game Email Summary")
    require_fresh(hr_summary, "HR Email Summary")
    require_fresh(prop_summary, "Player Props Email Summary")

    game_records = rows_as_records(workbook.worksheet(GAME_SOURCE_TAB).get_all_values())
    hr_records = rows_as_records(workbook.worksheet(HR_SOURCE_TAB).get_all_values())
    prop_records = rows_as_records(workbook.worksheet(PROP_SOURCE_TAB).get_all_values())
    prop_history_records = rows_as_records(workbook.worksheet(PROP_HISTORY_TAB).get_all_values())

    games = []
    for row in game_records:
        rank = as_int(row.get("Rank"))
        if date_text(row.get("Date")) != TODAY.isoformat() or not 1 <= rank <= PUBLISHED_GAME_LIMIT:
            continue
        if not verified(row.get("Verified")):
            continue
        row = dict(row)
        row["GamePk"] = normalized_game_pk(row.get("GamePk"))
        if row["GamePk"]:
            games.append(row)

    hr_version = clean_text(hr_summary.get("Model Version"))
    hr_candidates = []
    for row in hr_records:
        rank = as_int(row.get("Rank"))
        if date_text(row.get("Date")) != TODAY.isoformat() or not 1 <= rank <= EXTENDED_HR_LIMIT:
            continue
        if hr_version and row.get("Model Version") != hr_version:
            continue
        if row.get("MatchupVerified") and not verified(row.get("MatchupVerified")):
            continue
        row = dict(row)
        row["GamePk"] = normalized_game_pk(row.get("GamePk"))
        row["HR Candidate Source"] = "Published HR Target" if rank <= PUBLISHED_HR_LIMIT else "Extended HR Candidate"
        if row["GamePk"] and row.get("Player"):
            hr_candidates.append(row)

    prop_version = clean_text(prop_summary.get("Model Version"))
    props_by_id = {}
    for row in prop_records:
        report_rank = as_int(row.get("Report Rank"))
        if date_text(row.get("Date")) != TODAY.isoformat() or not 1 <= report_rank <= PUBLISHED_PROP_LIMIT:
            continue
        if prop_version and row.get("Model Version") != prop_version:
            continue
        if not verified(row.get("Roster Verified")) or not verified(row.get("Schedule Verified")):
            continue
        row = dict(row)
        row["GamePk"] = normalized_game_pk(row.get("GamePk"))
        row["Prop Candidate Source"] = "Published Player Prop"
        if row["GamePk"] and row.get("Player") and row.get("Prop Type"):
            props_by_id[row.get("Prediction ID") or f"published-{len(props_by_id)}"] = row
    for row in prop_history_records:
        if date_text(row.get("Date")) != TODAY.isoformat():
            continue
        if prop_version and row.get("Model Version") != prop_version:
            continue
        if not verified(row.get("Roster Verified")) or not verified(row.get("Schedule Verified")):
            continue
        row = dict(row)
        row["GamePk"] = normalized_game_pk(row.get("GamePk"))
        row["Prop Candidate Source"] = "Extended Player Prop"
        prediction_key = row.get("Prediction ID") or f"extended-{len(props_by_id)}"
        if row["GamePk"] and row.get("Player") and row.get("Prop Type"):
            props_by_id.setdefault(prediction_key, row)
    props = list(props_by_id.values())
    return games, hr_candidates, props, game_summary, hr_summary, prop_summary


def best_hr_for_game(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda row: (
            HISTORICAL_HR_RATES.get(row.get("HR Candidate Source"), 0.0),
            number(row.get("Score")),
            -as_int(row.get("Rank"), 9999),
        ),
        reverse=True,
    )[0]


def historical_prop_rate(prop: dict[str, Any]) -> float:
    return HISTORICAL_PROP_RATES.get(clean_text(prop.get("Prop Type")), 0.0)


def blended_component_score(model_score: Any, historical_rate: float) -> float:
    return 0.5 * number(model_score) + 0.5 * historical_rate


def prediction_id(game_pk: str, hr: dict[str, Any] | None = None, props: list[dict[str, Any]] | None = None) -> str:
    parts = [TODAY.isoformat(), game_pk, re.sub(r"[^a-z0-9]+", "", MODEL_VERSION.lower())]
    if hr is not None:
        parts.append(clean_text(hr.get("Player ID") or hr.get("Player")))
    for prop in props or []:
        parts.append(clean_text(prop.get("Prediction ID") or prop.get("Player")))
    return "|".join(parts)


def build_stacks(games, hr_candidates, props):
    hrs_by_game = defaultdict(list)
    props_by_game = defaultdict(list)
    for row in hr_candidates:
        hrs_by_game[row["GamePk"]].append(row)
    for row in props:
        props_by_game[row["GamePk"]].append(row)

    stacks, integrity = [], []
    primary_keys = set()

    def make_stack(game, hr, selected_props, selection_mode):
        game_pk = game["GamePk"]
        prop_one, prop_two = selected_props
        stack_score = composite_stack_score(
            game.get("Win Probability"),
            blended_component_score(
                hr.get("Score"),
                HISTORICAL_HR_RATES.get(hr.get("HR Candidate Source"), 0.0),
            ),
            blended_component_score(prop_one.get("Prop Score"), historical_prop_rate(prop_one)),
            blended_component_score(prop_two.get("Prop Score"), historical_prop_rate(prop_two)),
        )
        stack = {
            "Prediction ID": prediction_id(game_pk, hr, selected_props), "Date": TODAY.isoformat(),
            "Model Version": MODEL_VERSION, "GamePk": game_pk, "Game": game.get("Game", ""),
            "Venue": game.get("Venue", ""), "Projected Winner": game.get("Projected Winner", ""),
            "Game Rank": as_int(game.get("Rank")), "Win Probability": number(game.get("Win Probability")),
            "Game Confidence": game.get("Confidence", ""), "HR Player": hr.get("Player", ""),
            "HR Team": hr.get("Team", ""), "HR Rank": as_int(hr.get("Rank")),
            "HR Score": number(hr.get("Score")), "HR Confidence": hr.get("Confidence", ""),
            "HR Candidate Source": hr.get("HR Candidate Source", ""),
            "Prop 1 Player": prop_one.get("Player", ""), "Prop 1 Type": prop_one.get("Prop Type", ""),
            "Prop 1 Threshold": as_int(prop_one.get("Threshold")), "Prop 1 Pick": prop_one.get("Recommended Prop", ""),
            "Prop 1 Score": number(prop_one.get("Prop Score")), "Prop 1 Probability": number(prop_one.get("Projected Probability")),
            "Prop 1 Prediction ID": prop_one.get("Prediction ID", ""),
            "Prop 2 Player": prop_two.get("Player", ""), "Prop 2 Type": prop_two.get("Prop Type", ""),
            "Prop 2 Threshold": as_int(prop_two.get("Threshold")), "Prop 2 Pick": prop_two.get("Recommended Prop", ""),
            "Prop 2 Score": number(prop_two.get("Prop Score")), "Prop 2 Probability": number(prop_two.get("Projected Probability")),
            "Prop 2 Prediction ID": prop_two.get("Prediction ID", ""), "Stack Score": stack_score, "Complete": True,
            "Selection Notes": (
                f"{selection_mode}; same-game statistical synthesis; HR player excluded from both props; "
                f"Prop sources: {prop_one.get('Prop Candidate Source', '')}, {prop_two.get('Prop Candidate Source', '')}."
            ),
            "Prop 1 Candidate Source": prop_one.get("Prop Candidate Source", ""),
            "Prop 2 Candidate Source": prop_two.get("Prop Candidate Source", ""), "Result": "",
        }
        return stack

    for game in games:
        game_pk = game["GamePk"]
        hr = best_hr_for_game(hrs_by_game.get(game_pk, []))
        game_props = props_by_game.get(game_pk, [])
        selected_props = select_distinct_props(game_props, hr or {}, count=2)
        selection_mode = "Historical priority: Hits, then RBIs, then strikeouts"
        reasons = []
        if not hr:
            reasons.append("No verified HR candidate in ranks 1-30")
        if len(selected_props) < 2:
            reasons.append("Fewer than two distinct verified props after excluding HR hitter")
        complete = not reasons
        integrity.append({
            "Date": TODAY.isoformat(), "GamePk": game_pk, "Game": game.get("Game", ""),
            "Game Rank": as_int(game.get("Rank")), "Projected Winner": game.get("Projected Winner", ""),
            "HR Candidates": len(hrs_by_game.get(game_pk, [])),
            "Published Props": sum(row.get("Prop Candidate Source") == "Published Player Prop" for row in props_by_game.get(game_pk, [])),
            "Extended Props": sum(row.get("Prop Candidate Source") == "Extended Player Prop" for row in props_by_game.get(game_pk, [])),
            "Complete": "Yes" if complete else "No", "Notes": "Complete stack" if complete else "; ".join(reasons),
        })
        if complete:
            stack = make_stack(game, hr, selected_props, selection_mode)
            stacks.append(stack)
            primary_keys.add(stack["Prediction ID"])

    card = top_complete_stacks(stacks, count=3)

    # If fewer than three games form a primary stack, rank alternate complete
    # combinations using the same historical component rates. This keeps three
    # stacks without inventing or duplicating a selection.
    if len(card) < 3:
        alternates = []
        for game in games:
            game_pk = game["GamePk"]
            ordered_hrs = sorted(
                hrs_by_game.get(game_pk, []),
                key=lambda row: (
                    HISTORICAL_HR_RATES.get(row.get("HR Candidate Source"), 0.0),
                    number(row.get("Score")),
                    -as_int(row.get("Rank"), 9999),
                ),
                reverse=True,
            )
            for hr in ordered_hrs:
                eligible_props = select_distinct_props(
                    props_by_game.get(game_pk, []), hr, count=6
                )
                for first_index in range(len(eligible_props)):
                    for second_index in range(first_index + 1, len(eligible_props)):
                        selected = [
                            eligible_props[first_index],
                            eligible_props[second_index],
                        ]
                        stack = make_stack(
                            game,
                            hr,
                            selected,
                            "Historical-priority alternate complete stack",
                        )
                        if stack["Prediction ID"] not in primary_keys:
                            alternates.append(stack)
        card.extend(
            top_complete_stacks(alternates, count=max(0, 3 - len(card)))
        )

    if len(card) != 3:
        raise RuntimeError(
            f"Only {len(card)} complete stacks could be formed; exactly three are required."
        )

    for rank, stack in enumerate(card, start=1):
        stack["Card Rank"] = rank
    return card, integrity


HEADERS = [
    "Prediction ID", "Date", "Model Version", "Card Rank", "GamePk", "Game", "Venue",
    "Projected Winner", "Game Rank", "Win Probability", "Game Confidence", "HR Player", "HR Team",
    "HR Rank", "HR Score", "HR Confidence", "HR Candidate Source", "Prop 1 Player", "Prop 1 Type",
    "Prop 1 Threshold", "Prop 1 Pick", "Prop 1 Score", "Prop 1 Probability", "Prop 1 Prediction ID",
    "Prop 2 Player", "Prop 2 Type", "Prop 2 Threshold", "Prop 2 Pick", "Prop 2 Score",
    "Prop 2 Probability", "Prop 2 Prediction ID", "Stack Score", "Selection Notes", "Result",
]


def column_letter(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def get_or_create_sheet(workbook, title: str, rows=1000, cols=40):
    import gspread
    try:
        return workbook.worksheet(title)
    except gspread.WorksheetNotFound:
        return workbook.add_worksheet(title=title, rows=rows, cols=cols)


def ensure_header(worksheet, headers):
    existing = worksheet.row_values(1)
    if existing and existing[:len(headers)] != headers:
        raise RuntimeError(f"Header mismatch on {worksheet.title}; refusing to shift historical data.")
    if not existing:
        worksheet.update(values=[headers], range_name=f"A1:{column_letter(len(headers))}1")


def append_unique(worksheet, headers, records):
    ensure_header(worksheet, headers)
    existing = set(worksheet.col_values(1)[1:])
    rows = [[record.get(header, "") for header in headers] for record in records if record.get("Prediction ID") not in existing]
    if rows:
        worksheet.append_rows(rows, value_input_option="USER_ENTERED")


def build_email_rows(card):
    rows = [
        ["Daily MLB Best Card - Three Historically Ranked Statistical Game Stacks"], ["Last Updated", RUN_LOCAL],
        ["Model Version", MODEL_VERSION], ["Schedule Date Used", TODAY.isoformat()],
        ["Schedule Date Logic", DATE_LOGIC],
        ["Selection Method", "35% Game winner, 25% HR, 20% per prop; component scores blend current model strength with cleaned historical hit rates."], [],
    ]
    for stack in card:
        rows.extend([
            [f"Stack {stack['Card Rank']}", f"{stack['Projected Winner']} | {stack['Game']} | {stack['Venue']}"],
            ["Home Run", f"{stack['HR Player']} to hit a home run | {stack['HR Candidate Source']} | HR score {stack['HR Score']:.2f}"],
            ["Player Prop 1", f"{stack['Prop 1 Pick']} | {stack['Prop 1 Probability']:.1f}% | {stack.get('Prop 1 Candidate Source', 'Published Player Prop')}"],
            ["Player Prop 2", f"{stack['Prop 2 Pick']} | {stack['Prop 2 Probability']:.1f}% | {stack.get('Prop 2 Candidate Source', 'Published Player Prop')}"],
            ["Stack Score", f"{stack['Stack Score']:.2f}"],
            ["Game Model", f"{stack['Projected Winner']} win probability {stack['Win Probability']:.1f}% | Game rank {stack['Game Rank']}"],
            ["Integrity", "Same game confirmed | HR player distinct from both prop players | Two distinct prop players"], [],
        ])
    rows.extend([
        ["Model Notes"],
        ["HR Priority Rule", "Published HR targets are prioritized using a 22.4% historical rate; extended candidates (12.5%) are fallback-only."],
        ["Data Constraint", "Statistics only. No sportsbook odds, lines, implied probability, or market influence."],
        ["Prop Priority Rule", "Cleaned historical rates drive priority: Hits 70.8%, RBIs 38.9%, strikeouts 18.2%."],
        ["Card Count Rule", "Exactly three complete stacks are published; verified fallbacks are used only when required."],
        ["Winner Priority Rule", "Higher game-model win probability is preferred; no hard cutoff prevents completion of the three-stack card."],
        ["Results Tracking", "Each component and complete stack will be archived and graded after games become final."],
    ])
    return rows


def write_outputs(workbook, card, integrity):
    current_ws = get_or_create_sheet(workbook, BEST_CARD_TAB, 20, 40)
    history_ws = get_or_create_sheet(workbook, BEST_CARD_HISTORY_TAB, 2000, 40)
    email_ws = get_or_create_sheet(workbook, BEST_CARD_EMAIL_TAB, 100, 5)
    integrity_ws = get_or_create_sheet(workbook, BEST_CARD_INTEGRITY_TAB, 1000, 15)
    run_ws = get_or_create_sheet(workbook, BEST_CARD_RUN_TAB, 100, 5)

    current_ws.clear()
    current_ws.update(values=[HEADERS] + [[row.get(header, "") for header in HEADERS] for row in card], range_name=f"A1:{column_letter(len(HEADERS))}{len(card)+1}")
    append_unique(history_ws, HEADERS, card)
    email_rows = build_email_rows(card)
    email_ws.clear(); email_ws.update(values=email_rows, range_name=f"A1:B{len(email_rows)}")
    integrity_headers = ["Date", "GamePk", "Game", "Game Rank", "Projected Winner", "HR Candidates", "Published Props", "Extended Props", "Complete", "Notes"]
    integrity_ws.clear(); integrity_ws.update(values=[integrity_headers] + [[row.get(header, "") for header in integrity_headers] for row in integrity], range_name=f"A1:J{len(integrity)+1}")
    extended_count = sum(row["HR Candidate Source"] == "Extended HR Candidate" for row in card)
    extended_prop_count = sum(
        row.get(source) == "Extended Player Prop"
        for row in card
        for source in ("Prop 1 Candidate Source", "Prop 2 Candidate Source")
    )
    run_rows = [
        ["Run Timestamp UTC", RUN_UTC], ["Run Timestamp Pacific", RUN_LOCAL],
        ["Schedule Date Used", TODAY.isoformat()], ["Schedule Date Logic", DATE_LOGIC],
        ["Model Version", MODEL_VERSION], ["Complete Candidate Games", sum(row["Complete"] == "Yes" for row in integrity)],
        ["Published Stacks", len(card)], ["Extended HR Candidates Used", extended_count],
        ["Extended Player Props Used", extended_prop_count],
        ["Status", "Completed Successfully"],
    ]
    run_ws.clear(); run_ws.update(values=run_rows, range_name=f"A1:B{len(run_rows)}")


def main():
    print(f"Starting {MODEL_VERSION} for {TODAY.isoformat()}")
    client = auth_google()
    workbook = client.open(SHEET_NAME)
    games, hrs, props, _, _, _ = load_inputs(workbook)
    card, integrity = build_stacks(games, hrs, props)
    write_outputs(workbook, card, integrity)
    for stack in card:
        print(
            f"{stack['Card Rank']}. {stack['Projected Winner']} | {stack['HR Player']} HR | "
            f"{stack['Prop 1 Pick']} | {stack['Prop 2 Pick']} | {stack['Stack Score']:.2f}"
        )


if __name__ == "__main__":
    main()
