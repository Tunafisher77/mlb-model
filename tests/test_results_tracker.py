import re
import unittest
from unittest.mock import patch

import mlb_results_tracker as tracker


class WorksheetNotFound(Exception):
    pass


class FakeWorksheet:
    def __init__(self, values=None):
        self.values = [list(row) for row in (values or [])]
        self.update_calls = 0

    def get_all_values(self):
        return [list(row) for row in self.values]

    def row_values(self, row):
        return list(self.values[row - 1]) if row <= len(self.values) else []

    def col_values(self, column):
        return [row[column - 1] if len(row) >= column else "" for row in self.values]

    def append_rows(self, rows, value_input_option=None):
        self.values.extend([list(row) for row in rows])

    def update(self, values, range_name=None):
        self.update_calls += 1
        match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", range_name or "")
        if not match:
            return
        start_letters, start_row, _, _ = match.groups()
        start_column = 0
        for letter in start_letters:
            start_column = start_column * 26 + ord(letter) - 64
        start_column -= 1
        start_row = int(start_row) - 1
        while len(self.values) <= start_row + len(values) - 1:
            self.values.append([])
        for row_offset, incoming in enumerate(values):
            row = self.values[start_row + row_offset]
            needed = start_column + len(incoming)
            if len(row) < needed:
                row.extend([""] * (needed - len(row)))
            row[start_column:needed] = list(incoming)


class FakeWorkbook:
    def __init__(self, sheets):
        self.sheets = sheets
        self.worksheet_calls = 0

    def worksheet(self, title):
        self.worksheet_calls += 1
        if title not in self.sheets:
            raise WorksheetNotFound(title)
        return self.sheets[title]

    def add_worksheet(self, title, rows, cols):
        worksheet = FakeWorksheet()
        self.sheets[title] = worksheet
        return worksheet


def sample_feed(final=True, home_runs=1, plate_appearances=4):
    return {
        "gameData": {
            "status": {
                "abstractGameState": "Final" if final else "Live",
                "codedGameState": "F" if final else "I",
                "detailedState": "Final" if final else "In Progress",
            },
            "teams": {
                "away": {"abbreviation": "KC", "name": "Kansas City Royals"},
                "home": {"abbreviation": "LAD", "name": "Los Angeles Dodgers"},
            },
        },
        "liveData": {
            "linescore": {"teams": {"away": {"runs": 2}, "home": {"runs": 6}}},
            "boxscore": {
                "teams": {
                    "away": {
                        "players": {
                            "ID669203": {
                                "person": {"id": 669203, "fullName": "Tarik Skubal"},
                                "stats": {"pitching": {"inningsPitched": "7.0", "strikeOuts": 9}},
                            }
                        }
                    },
                    "home": {
                        "players": {
                            "ID660271": {
                                "person": {"id": 660271, "fullName": "Shohei Ohtani"},
                                "stats": {"batting": {
                                    "plateAppearances": plate_appearances, "atBats": 4, "hits": 2,
                                    "doubles": 1, "triples": 0, "homeRuns": home_runs,
                                    "rbi": 3,
                                }},
                            },
                            "ID518692": {
                                "person": {"id": 518692, "fullName": "Freddie Freeman"},
                                "stats": {"batting": {
                                    "plateAppearances": 4, "atBats": 4, "hits": 2,
                                    "doubles": 0, "triples": 0, "homeRuns": 0,
                                    "rbi": 1,
                                }},
                            }
                        }
                    },
                }
            },
        },
    }


class ResultsTrackerUnitTest(unittest.TestCase):
    def test_worksheet_lookup_is_cached(self):
        workbook = FakeWorkbook({"Example": FakeWorksheet([["A"]])})
        first = tracker.worksheet_by_title(workbook, "Example")
        second = tracker.worksheet_by_title(workbook, "Example")
        self.assertIs(first, second)
        self.assertEqual(workbook.worksheet_calls, 1)

    def test_quota_retry_recovers_from_temporary_429(self):
        calls = []

        def operation():
            calls.append(True)
            if len(calls) < 3:
                raise RuntimeError("APIError: [429] quota exceeded")
            return "ok"

        with patch.object(tracker.time, "sleep") as sleep:
            self.assertEqual(tracker.quota_retry(operation), "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_unchanged_game_status_does_not_write(self):
        worksheet = FakeWorksheet([tracker.GAME_HEADERS, [""] * len(tracker.GAME_HEADERS)])
        record = {"Game Status": "Pre-Game"}
        changed = tracker.update_game_status_if_changed(
            worksheet, tracker.GAME_HEADERS, 2, record, "Pre-Game"
        )
        self.assertFalse(changed)
        self.assertEqual(worksheet.update_calls, 0)

    def test_changed_game_status_writes_once(self):
        worksheet = FakeWorksheet([tracker.GAME_HEADERS, [""] * len(tracker.GAME_HEADERS)])
        record = {"Game Status": "Scheduled"}
        changed = tracker.update_game_status_if_changed(
            worksheet, tracker.GAME_HEADERS, 2, record, "Pre-Game"
        )
        self.assertTrue(changed)
        self.assertEqual(worksheet.update_calls, 1)

    def test_final_score(self):
        away, home, away_runs, home_runs, winner = tracker.final_score(sample_feed())
        self.assertEqual((away, home, away_runs, home_runs, winner), ("KC", "LAD", 2, 6, "LAD"))

    def test_player_match_is_scoped_to_side(self):
        player = tracker.find_player_boxscore(sample_feed(), "Shohei Ohtani", "Home")
        self.assertEqual(player["person"]["id"], 660271)
        self.assertIsNone(tracker.find_player_boxscore(sample_feed(), "Shohei Ohtani", "Away"))

    def test_ids_are_deterministic(self):
        record = {
            "Date": "2026-08-10", "GamePk": "823918", "Player": "Shohei Ohtani",
            "Model Version": "Automated V16.2 - Eastern Slate Schedule Fix",
        }
        self.assertEqual(tracker.hr_prediction_id(record), tracker.hr_prediction_id(record))
        self.assertIn("823918", tracker.hr_prediction_id(record))

    def test_player_prop_boxscore_values(self):
        hitter = tracker.find_player_boxscore_by_id(sample_feed(), 660271, "Home")
        hit_values = tracker.player_prop_boxscore_values(hitter, "Hits")
        self.assertEqual(hit_values["Hits"], 2)
        self.assertEqual(hit_values["Total Bases"], 6)
        self.assertEqual(hit_values["RBIs"], 3)
        self.assertTrue(hit_values["Appeared"])

        pitcher = tracker.find_player_boxscore_by_id(sample_feed(), 669203, "Away")
        strikeout_values = tracker.player_prop_boxscore_values(pitcher, "Strikeouts")
        self.assertEqual(strikeout_values["Strikeouts"], 9)
        self.assertTrue(strikeout_values["Appeared"])

    def test_prop_prediction_id_uses_source_id(self):
        record = {"Prediction ID": "published-prop-id"}
        self.assertEqual(tracker.prop_prediction_id(record), "published-prop-id")

    def test_rank_tiers_match_production(self):
        self.assertEqual(tracker.tier_from_rank(3), "Primary")
        self.assertEqual(tracker.tier_from_rank(6), "Secondary")
        self.assertEqual(tracker.tier_from_rank(9), "Longshot")

    def test_feed_cache_fetches_each_game_once(self):
        cache = {}
        with patch.object(tracker, "fetch_game_feed", return_value=sample_feed()) as fetch:
            tracker.cached_feed(cache, "823918")
            tracker.cached_feed(cache, "823918")
        fetch.assert_called_once_with("823918")

    def test_snapshot_is_idempotent_and_uses_published_cards(self):
        workbook = FakeWorkbook({
            "Game Email Summary": FakeWorksheet([
                ["Schedule Date Used", "2026-08-10"],
            ]),
            "Game Picks": FakeWorksheet([
                ["Date", "Model Version", "Rank", "GamePk", "Game", "Venue", "Projected Winner", "Opponent", "Confidence", "Win Probability", "Expected Margin", "Away Team", "Home Team", "Away Projected Runs", "Home Projected Runs", "Verified"],
                ["2026-08-10", "Game Picks V2.1.1", "1", "823918", "KC @ LAD", "Dodger Stadium", "LAD", "KC", "Elite", "95.2", "3.8", "KC", "LAD", "2.9", "6.7", "Yes"],
            ]),
            "Email Summary": FakeWorksheet([
                ["Model Version", "Automated V16.2"],
                ["Schedule Date Used", "2026-08-10"],
            ]),
            "Model Results": FakeWorksheet([
                ["Date", "Model Version", "Rank", "Group", "Confidence", "Player", "Team", "Opponent", "Opposing Pitcher", "Venue", "GamePk", "HomeAway", "Score", "Season HR", "Last7HR", "HardHit%", "100+MPH%", "FlyBall%", "PitcherVulnerability", "ParkFactor", "WeatherScore"],
                ["2026-08-10", "Automated V16.2", "1", "Group 1", "Elite Target", "Shohei Ohtani", "LAD", "KC", "Noah Cameron", "Dodger Stadium", "823918", "Home", "81.2", "40", "3", "52", "31", "48", "65", "105", "57"],
            ]),
        })

        self.assertEqual(tracker.snapshot_game_picks(workbook, "2026-08-10"), 1)
        self.assertEqual(tracker.snapshot_hr_picks(workbook, "2026-08-10"), 1)
        self.assertEqual(tracker.snapshot_game_picks(workbook, "2026-08-10"), 0)
        self.assertEqual(tracker.snapshot_hr_picks(workbook, "2026-08-10"), 0)
        self.assertEqual(len(workbook.sheets[tracker.GAME_TRACKING_TAB].values), 2)
        self.assertEqual(len(workbook.sheets[tracker.HR_TRACKING_TAB].values), 2)

    def test_player_prop_snapshot_is_idempotent(self):
        workbook = FakeWorkbook({
            "Player Props Email Summary": FakeWorksheet([
                ["Model Version", "Player Props V1.1"],
                ["Schedule Date Used", "2026-08-10"],
            ]),
            "Player Props": FakeWorksheet([
                ["Prediction ID", "Date", "Model Version", "Report Rank", "Report Section", "Player Type", "Player ID", "Player", "Team", "Opponent", "GamePk", "Game", "HomeAway", "Venue", "Prop Type", "Threshold", "Recommended Prop", "Projected Probability", "Probability Gate", "Projected Mean", "Prop Score", "Confidence"],
                ["prop-1", "2026-08-10", "Player Props V1.1", "1", "Top Props", "Hitter", "660271", "Shohei Ohtani", "LAD", "KC", "823918", "KC @ LAD", "Home", "Dodger Stadium", "Hits", "1", "Shohei Ohtani 1+ Hits", "73.0", "62", "1.12", "77.8", "Elite"],
            ]),
        })
        self.assertEqual(tracker.snapshot_player_props(workbook, "2026-08-10"), 1)
        self.assertEqual(tracker.snapshot_player_props(workbook, "2026-08-10"), 0)
        self.assertEqual(len(workbook.sheets[tracker.PROP_TRACKING_TAB].values), 2)

        graded = tracker.grade_player_prop_rows(workbook, {"823918": sample_feed()})
        self.assertEqual(graded, 1)
        result = tracker.rows_as_records(
            workbook.sheets[tracker.PROP_TRACKING_TAB].get_all_values()
        )[0]
        self.assertEqual(result["Result Status"], "Final")
        self.assertEqual(result["Actual Value"], 2)
        self.assertEqual(result["Hit Prop?"], "Yes")

    def test_delayed_best_card_does_not_block_other_tracking(self):
        workbook = FakeWorkbook({
            "Best Card Email Summary": FakeWorksheet([
                ["Model Version", "Best Card V1.1"],
                ["Schedule Date Used", "2026-08-09"],
            ]),
        })

        count, warning = tracker.snapshot_best_card_if_available(workbook, "2026-08-10")

        self.assertEqual(count, 0)
        self.assertIn("snapshot deferred", warning)
        self.assertIn("not fresh", warning)

    def test_best_card_snapshot_and_component_grading(self):
        best_card_headers = [
            "Prediction ID", "Date", "Model Version", "Card Rank", "GamePk", "Game",
            "Venue", "Projected Winner", "Win Probability", "HR Player", "HR Team",
            "HR Rank", "HR Candidate Source", "Prop 1 Player", "Prop 1 Type",
            "Prop 1 Threshold", "Prop 1 Pick", "Prop 1 Prediction ID", "Prop 2 Player",
            "Prop 2 Type", "Prop 2 Threshold", "Prop 2 Pick", "Prop 2 Prediction ID",
            "Stack Score",
        ]
        row = [
            "best-card-1", "2026-08-10", "Best Card V1.1", "1", "823918",
            "KC @ LAD", "Dodger Stadium", "LAD", "80.4", "Shohei Ohtani", "LAD",
            "1", "Published HR Target", "Freddie Freeman", "Hits", "2",
            "Freddie Freeman 2+ Hits", "prop-1", "Tarik Skubal", "Strikeouts", "7",
            "Tarik Skubal 7+ Strikeouts", "prop-2", "77.2",
        ]
        row_two = list(row)
        row_two[0], row_two[3] = "best-card-2", "2"
        row_three = list(row)
        row_three[0], row_three[3] = "best-card-3", "3"
        workbook = FakeWorkbook({
            "Best Card Email Summary": FakeWorksheet([
                ["Model Version", "Best Card V1.1"],
                ["Schedule Date Used", "2026-08-10"],
            ]),
            "Best Card": FakeWorksheet([best_card_headers, row, row_two, row_three]),
        })
        # Thin slates must archive and grade just as full three-stack slates do.
        for count in (1, 2):
            thin = FakeWorkbook({
                "Best Card Email Summary": workbook.sheets["Best Card Email Summary"],
                "Best Card": FakeWorksheet([best_card_headers] + [row, row_two][:count]),
            })
            self.assertEqual(tracker.snapshot_best_card(thin, "2026-08-10"), count)
            self.assertEqual(tracker.snapshot_best_card(thin, "2026-08-10"), 0)
            self.assertEqual(tracker.grade_best_card_rows(thin, {"823918": sample_feed()}), count)
        self.assertEqual(tracker.snapshot_best_card(workbook, "2026-08-10"), 3)
        self.assertEqual(tracker.snapshot_best_card(workbook, "2026-08-10"), 0)

        graded = tracker.grade_best_card_rows(workbook, {"823918": sample_feed()})
        self.assertEqual(graded, 3)
        result = tracker.rows_as_records(
            workbook.sheets[tracker.BEST_CARD_TRACKING_TAB].get_all_values()
        )[0]
        self.assertEqual(result["Winner Correct?"], "Yes")
        self.assertEqual(result["HR Hit?"], "Yes")
        self.assertEqual(result["Prop 1 Hit?"], "Yes")
        self.assertEqual(result["Prop 2 Hit?"], "Yes")
        self.assertEqual(result["Components Graded"], 4)
        self.assertEqual(result["Components Hit"], 4)
        self.assertEqual(result["Perfect Stack?"], "Yes")


if __name__ == "__main__":
    unittest.main()
