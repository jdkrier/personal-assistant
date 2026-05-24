import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from goose import compute_urgency, parse_llm_json, compute_free_blocks


# --- urgency formula ---

def test_urgency_due_today():
    assert compute_urgency(0) == 2.0

def test_urgency_due_tomorrow():
    assert compute_urgency(1) == 1.0

def test_urgency_due_next_week():
    result = compute_urgency(7)
    assert 0.14 < result < 0.15

def test_urgency_never_divides_by_zero():
    for days in [-5, -1, 0, 0.1, 0.5, 1, 7, 30]:
        result = compute_urgency(days)
        assert result > 0
        assert result <= 2.0


# --- LLM JSON parsing ---

def test_parse_clean_json():
    text = '{"headline": "Do the homework", "priority_task": "hw", "suggested_block": "2pm"}'
    result = parse_llm_json(text)
    assert result is not None
    assert result["headline"] == "Do the homework"

def test_parse_json_with_preamble():
    text = 'Sure! Here is the JSON:\n{"headline": "Do the homework", "priority_task": "hw", "suggested_block": "2pm"}'
    result = parse_llm_json(text)
    assert result is not None
    assert result["headline"] == "Do the homework"

def test_parse_invalid_returns_none():
    assert parse_llm_json("This is not JSON at all") is None

def test_parse_empty_returns_none():
    assert parse_llm_json("") is None


# --- free block computation ---

def test_free_blocks_no_events():
    blocks = compute_free_blocks([])
    assert len(blocks) == 1
    assert blocks[0]["hours"] == 14.0  # 8am to 10pm

def test_free_blocks_splits_around_event():
    events = [{"title": "Class", "start": "2026-05-22T10:00:00", "end": "2026-05-22T11:00:00"}]
    blocks = compute_free_blocks(events)
    assert len(blocks) == 2
    assert blocks[0]["hours"] == 2.0   # 8am–10am
    assert blocks[1]["hours"] == 11.0  # 11am–10pm

def test_free_blocks_all_day_event_ignored():
    events = [{"title": "Holiday", "start": "2026-05-22", "end": "2026-05-23"}]
    blocks = compute_free_blocks(events)
    assert len(blocks) == 1  # all-day has no "T", skipped → full day free

def test_free_blocks_overlapping_events_merged():
    events = [
        {"title": "A", "start": "2026-05-22T09:00:00", "end": "2026-05-22T10:30:00"},
        {"title": "B", "start": "2026-05-22T10:00:00", "end": "2026-05-22T11:00:00"},
    ]
    blocks = compute_free_blocks(events)
    # Should merge A and B into one 9–11 block, leaving 8–9 and 11–10pm free
    assert len(blocks) == 2
    assert blocks[0]["hours"] == 1.0   # 8am–9am
    assert blocks[1]["hours"] == 11.0  # 11am–10pm
