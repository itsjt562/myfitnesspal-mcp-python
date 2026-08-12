"""
Pure-logic unit tests for mfp_mcp.server -- no network, no live MFP account.

Covers the classes of bugs found and fixed in this codebase: serving-unit
matching, quantity-to-servings math, date parsing, meal-name normalization,
and the country_code/carb math for custom foods. Run with:

    .venv/Scripts/python -m pytest tests/ -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from datetime import date

from mfp_mcp.server import (
    select_serving_size,
    _canonical_unit_tokens,
    parse_date,
    _serving_sizes,
    mfp_set_goals,
    SetGoalsInput,
    remove_food_entry,
)


# ---------------------------------------------------------------------------
# _canonical_unit_tokens / select_serving_size
# ---------------------------------------------------------------------------

def test_canonical_tokens_basic_synonyms():
    assert _canonical_unit_tokens("oz") == {"oz"}
    assert _canonical_unit_tokens("ounce") == {"oz"}
    assert _canonical_unit_tokens("ounces") == {"oz"}
    assert _canonical_unit_tokens("g") == {"g"}
    assert _canonical_unit_tokens("gram") == {"g"}
    assert _canonical_unit_tokens("kg") == {"kg"}
    assert _canonical_unit_tokens("kilogram") == {"kg"}


def test_canonical_tokens_strips_parenthetical_descriptors():
    # The exact string that caused the 2026-07 32oz bug: "oz" is a raw
    # substring of this, but it must NOT canonicalize to {"oz"}.
    assert "oz" not in _canonical_unit_tokens("container (8 oz ea.)")
    assert _canonical_unit_tokens("container (8 oz ea.)") == set()


def test_canonical_tokens_fl_oz_distinct_from_dry_oz():
    assert _canonical_unit_tokens("fl. oz") == {"fl_oz"}
    assert _canonical_unit_tokens("fl oz") == {"fl_oz"}
    assert _canonical_unit_tokens("fluid ounce") == {"fl_oz"}
    assert _canonical_unit_tokens("oz") == {"oz"}
    # No overlap between the two -- a dry-oz request must not match a
    # fluid-oz record or vice versa.
    assert not (_canonical_unit_tokens("fl oz") & _canonical_unit_tokens("oz"))


def test_select_serving_size_skips_container_wrapper_for_oz_request():
    """Regression test for the exact 2026-07 bug: '4 oz' of ground beef
    matched a 'container (8 oz ea.)' record via substring, logging 32oz."""
    food = {
        "id": "1",
        "serving_sizes": [
            {"value": 4.0, "unit": "ounce", "nutrition_multiplier": 4.0},
            {"value": 1.0, "unit": "container (8 oz ea.)", "nutrition_multiplier": 8.0},
            {"value": 1.0, "unit": "gram", "nutrition_multiplier": 0.0357},
            {"value": 1.0, "unit": "kilogram", "nutrition_multiplier": 35.7},
        ],
    }
    chosen = select_serving_size(food, "oz")
    assert chosen["unit"] == "ounce"
    assert chosen["value"] == 4.0


def test_select_serving_size_grams_prefers_gram_record():
    food = {
        "id": "1",
        "serving_sizes": [
            {"value": 1.0, "unit": "container (8 oz ea.)", "nutrition_multiplier": 8.0},
            {"value": 1.0, "unit": "gram", "nutrition_multiplier": 0.0357},
        ],
    }
    chosen = select_serving_size(food, "g")
    assert chosen["unit"] == "gram"


def test_select_serving_size_fl_oz_vs_dry_oz():
    food = {
        "id": "2",
        "serving_sizes": [
            {"value": 4.0, "unit": "fl. oz", "nutrition_multiplier": 4.0},
            {"value": 1.0, "unit": "ounce", "nutrition_multiplier": 1.0},
        ],
    }
    assert select_serving_size(food, "fl oz")["unit"] == "fl. oz"
    assert select_serving_size(food, "oz")["unit"] == "ounce"


def test_select_serving_size_falls_back_to_substring_for_unknown_units():
    """Descriptive units not in the canonical table (e.g. 'medium breast')
    still work via the old loose substring match."""
    food = {
        "id": "3",
        "serving_sizes": [
            {"value": 1.0, "unit": "medium breast", "nutrition_multiplier": 1.0},
            {"value": 1.0, "unit": "gram", "nutrition_multiplier": 0.01},
        ],
    }
    assert select_serving_size(food, "medium breast")["unit"] == "medium breast"


def test_select_serving_size_no_serving_sizes_raises():
    with pytest.raises(RuntimeError):
        select_serving_size({"id": "4", "serving_sizes": []}, "oz")


def test_select_serving_size_prefers_smallest_granularity_among_same_unit():
    """Regression test for the 2026-08 orange juice report: a food can
    declare the same canonical unit at multiple granularities (e.g. both
    '8.00 x fl oz' and '1.00 x fl oz'). Division math produces the correct
    TOTAL against either record, but matching the coarser one first turns
    '14 fl oz' into '1.75 servings of 8 fl oz' -- mathematically fine, but
    reads like a rounding error. Must prefer the finer-grained record so
    '14 fl oz' becomes a clean 14.0 servings of 1 fl oz instead."""
    food = {
        "id": "oj",
        "serving_sizes": [
            {"value": 8.0, "unit": "fl oz", "nutrition_multiplier": 8.0},
            {"value": 1.0, "unit": "fl oz", "nutrition_multiplier": 1.0},
        ],
    }
    chosen = select_serving_size(food, "fl oz")
    assert chosen["value"] == 1.0

    quantity = 14.0
    servings = quantity / float(chosen["value"])
    assert servings == 14.0  # clean, not 1.75


def test_select_serving_size_defaults_to_first_when_unit_omitted():
    food = {
        "id": "5",
        "serving_sizes": [
            {"value": 4.0, "unit": "ounce", "nutrition_multiplier": 4.0},
            {"value": 1.0, "unit": "gram", "nutrition_multiplier": 0.0357},
        ],
    }
    assert select_serving_size(food, None)["unit"] == "ounce"


# ---------------------------------------------------------------------------
# quantity -> servings math (the actual division logic in add_food_to_diary,
# re-derived here so it can be tested without a live client/session)
# ---------------------------------------------------------------------------

def _compute_servings(food, quantity, unit):
    is_serving_count = unit.strip().lower() in ("serving", "servings", "srv", "")
    serving_size = select_serving_size(food, None if is_serving_count else unit)
    servings = float(quantity)
    if not is_serving_count:
        base_value = float(serving_size["value"]) or 1.0
        servings = float(quantity) / base_value
    total_in_unit = servings * float(serving_size["value"])
    return serving_size, servings, total_in_unit


def test_quantity_math_invariant_across_record_value():
    """Whatever record gets matched, requested amount == logged amount --
    this must hold regardless of the record's own base value (1, 4, 30...)."""
    for record_value in (1.0, 3.0, 4.0, 8.0, 30.0):
        food = {
            "id": "x",
            "serving_sizes": [
                {"value": record_value, "unit": "ounce", "nutrition_multiplier": record_value},
            ],
        }
        _, _, total = _compute_servings(food, 8.0, "oz")
        assert total == pytest.approx(8.0), f"failed for record value={record_value}"


def test_quantity_math_grams():
    food = {"id": "x", "serving_sizes": [{"value": 1.0, "unit": "gram", "nutrition_multiplier": 0.01}]}
    _, _, total = _compute_servings(food, 300.0, "g")
    assert total == pytest.approx(300.0)


def test_quantity_math_serving_count_mode_is_raw_passthrough():
    food = {"id": "x", "serving_sizes": [{"value": 1.0, "unit": "serving", "nutrition_multiplier": 1.0}]}
    _, servings, _ = _compute_servings(food, 2.5, "serving")
    assert servings == 2.5


def test_quantity_math_never_divides_by_zero():
    """A malformed record with value=0 must not raise ZeroDivisionError."""
    food = {"id": "x", "serving_sizes": [{"value": 0.0, "unit": "ounce", "nutrition_multiplier": 0.0}]}
    _, servings, _ = _compute_servings(food, 4.0, "oz")
    assert servings == 4.0  # falls back to treating base_value as 1.0


# ---------------------------------------------------------------------------
# parse_date
# ---------------------------------------------------------------------------

def test_parse_date_none_returns_today():
    assert parse_date(None) == date.today()


def test_parse_date_valid_string():
    assert parse_date("2026-07-30") == date(2026, 7, 30)


def test_parse_date_invalid_string_raises():
    with pytest.raises(ValueError):
        parse_date("07/30/2026")


# ---------------------------------------------------------------------------
# meal-name normalization (same logic as mfp_add_food_to_diary's wrapper)
# ---------------------------------------------------------------------------

def _normalize_meal(raw: str) -> str:
    meal = raw.strip().title()
    if meal.lower() == "snack":
        meal = "Snacks"
    return meal


def test_meal_name_multiword_custom_names_title_cased():
    assert _normalize_meal("pre workout") == "Pre Workout"
    assert _normalize_meal("intra workout") == "Intra Workout"


def test_meal_name_snack_singular_normalized_to_snacks():
    assert _normalize_meal("snack") == "Snacks"
    assert _normalize_meal("Snack") == "Snacks"


def test_meal_name_stock_defaults_unchanged():
    assert _normalize_meal("dinner") == "Dinner"
    assert _normalize_meal("BREAKFAST") == "Breakfast"


# ---------------------------------------------------------------------------
# custom-food serving-size wrapper (the container-wrapper generator that
# _every_ custom food gets, which is exactly the shape that caused the
# oz-vs-container bug -- verifying the wrapper itself is unambiguous)
# ---------------------------------------------------------------------------

def test_custom_food_serving_sizes_primary_and_container_wrapper():
    sizes = _serving_sizes(100, "g")
    assert sizes[0]["value"] == 100
    assert sizes[0]["unit"] == "g"
    assert sizes[1]["unit"] == "container (100 g ea.)"


def test_custom_food_container_wrapper_never_matches_gram_request():
    """The auto-generated container wrapper must never satisfy a plain 'g'
    or 'oz' request -- select_serving_size must always prefer the primary
    serving record."""
    food = {"id": "custom", "serving_sizes": _serving_sizes(100, "g")}
    assert select_serving_size(food, "g")["unit"] == "g"


# ---------------------------------------------------------------------------
# mfp_set_goals -- mocked client, no live account writes.
#
# client.set_new_goal()'s `energy` parameter is required with no default
# (verified live 2026-07: a protein-only call raised "missing 1 required
# positional argument: 'energy'"), contradicting this tool's own contract of
# "only updates the values provided". These tests run against a mocked
# client rather than the live account: MFP recalculates and rounds the
# entire goal set on every real write (confirmed live -- sending the exact
# same 4 values back still drifted calories/protein/carbs by a few units),
# so a live round-trip test would itself perturb the account's real
# nutrition goals every time the suite runs.
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def test_set_goals_protein_only_backfills_energy_from_current_goal():
    mock_client = MagicMock()
    mock_client.get_date.return_value.goals = {"calories": 1845.0}

    with patch("mfp_mcp.server.get_mfp_client", return_value=mock_client):
        result = _run(mfp_set_goals(SetGoalsInput(protein=0)))

    mock_client.set_new_goal.assert_called_once_with(energy=1845.0, protein=0)
    assert '"protein": 0' in result


def test_set_goals_calories_provided_skips_current_goal_lookup():
    mock_client = MagicMock()

    with patch("mfp_mcp.server.get_mfp_client", return_value=mock_client):
        _run(mfp_set_goals(SetGoalsInput(calories=2000, fat=60)))

    mock_client.get_date.assert_not_called()
    mock_client.set_new_goal.assert_called_once_with(energy=2000, fat=60)


def test_set_goals_no_fields_provided_errors_without_calling_client():
    mock_client = MagicMock()

    with patch("mfp_mcp.server.get_mfp_client", return_value=mock_client):
        result = _run(mfp_set_goals(SetGoalsInput()))

    assert "Error" in result
    mock_client.set_new_goal.assert_not_called()


# ---------------------------------------------------------------------------
# remove_food_entry -- entry_id id-space validation.
#
# mfp_add_food_to_diary returns a v2-API UUID as its entry_id.
# remove_food_entry's legacy /food/remove/{id} endpoint expects MFP's
# legacy numeric diary_entry_id (the id list_diary_entries scrapes) --
# a completely different id space. Verified live, 2026-08: passing the
# UUID through doesn't 404, it 302-redirects like any other path segment,
# which the old status-code-only check treated as a successful delete even
# though nothing was removed -- a false "success" that leaves the entry
# sitting in the diary. Must reject non-numeric ids before ever making the
# request.
# ---------------------------------------------------------------------------

def test_remove_food_entry_rejects_uuid_from_add_food_to_diary():
    mock_client = MagicMock()
    uuid_entry_id = "33a67353-741c-4829-865d-a366a1cdcded"

    with pytest.raises(RuntimeError, match="isn't a MyFitnessPal legacy diary entry id"):
        remove_food_entry(mock_client, uuid_entry_id)

    # must fail before ever touching the network
    mock_client.session.get.assert_not_called()
    mock_client.session.request.assert_not_called()


def test_remove_food_entry_accepts_legacy_numeric_id():
    mock_client = MagicMock()
    mock_client.session.get.return_value.text = (
        '<meta name="csrf-token" content="tok123">'
    )
    mock_client.session.request.return_value.status_code = 302

    remove_food_entry(mock_client, "12890177447")

    mock_client.session.request.assert_called_once()
    called_url = mock_client.session.request.call_args[0][1]
    assert called_url.endswith("food/remove/12890177447")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
