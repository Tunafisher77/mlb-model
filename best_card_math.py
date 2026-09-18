"""Pure selection helpers for the statistics-only MLB Best Card model."""

from __future__ import annotations

from typing import Any, Iterable


PROP_HISTORICAL_HIT_RATES = {
    "Hits": 70.8,
    "RBIs": 38.9,
    "Strikeouts": 18.2,
}


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalized_player_key(record: dict[str, Any]) -> str:
    name_key = "".join(ch.lower() for ch in str(record.get("Player", "")) if ch.isalnum())
    if name_key:
        return "name:" + name_key
    player_id = str(record.get("Player ID", "")).strip()
    return f"id:{player_id}" if player_id else ""


def select_distinct_props(
    props: Iterable[dict[str, Any]],
    excluded_player: dict[str, Any],
    count: int = 2,
) -> list[dict[str, Any]]:
    """Return the highest-scoring props from distinct players, excluding the HR hitter."""
    excluded_key = normalized_player_key(excluded_player)
    selected = []
    used = {excluded_key}
    ordered = sorted(
        props,
        key=lambda row: (
            PROP_HISTORICAL_HIT_RATES.get(str(row.get("Prop Type", "")).strip(), 0.0),
            row.get("Prop Candidate Source") == "Published Player Prop",
            number(row.get("Prop Score")),
            number(row.get("Projected Probability")),
            -number(row.get("Report Rank"), 9999),
        ),
        reverse=True,
    )
    for prop in ordered:
        player_key = normalized_player_key(prop)
        if not player_key or player_key in used:
            continue
        selected.append(prop)
        used.add(player_key)
        if len(selected) >= count:
            break
    return selected


def composite_stack_score(
    win_probability: Any,
    hr_score: Any,
    prop_one_score: Any,
    prop_two_score: Any,
) -> float:
    """35% Game, 25% HR, and 20% for each of two Player Props."""
    score = (
        number(win_probability) * 0.35
        + number(hr_score) * 0.25
        + number(prop_one_score) * 0.20
        + number(prop_two_score) * 0.20
    )
    return round(max(0.0, min(100.0, score)), 2)


def top_complete_stacks(stacks: Iterable[dict[str, Any]], count: int = 3) -> list[dict[str, Any]]:
    complete = [row for row in stacks if row.get("Complete")]
    return sorted(
        complete,
        key=lambda row: (
            number(row.get("Stack Score")),
            number(row.get("Win Probability")),
            number(row.get("HR Score")),
        ),
        reverse=True,
    )[:count]
