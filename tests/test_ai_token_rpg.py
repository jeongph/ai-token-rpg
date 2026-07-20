import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import pytest

import ai_token_rpg as rpg


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def usage(**values):
    record = rpg.empty_usage()
    record.update(values)
    return record


def test_parse_date_uses_configured_timezone():
    assert rpg.parse_date("2026-07-17T16:00:00Z", "Asia/Seoul") == "2026-07-18"
    assert rpg.parse_date("bad", "Asia/Seoul") is None


def test_legacy_store_migrates_to_claude_provider():
    legacy = {
        "tz": "Asia/Seoul",
        "days": {
            "2026-06-13": {
                "input": 10,
                "output": 20,
                "cache_creation": 30,
                "cache_read": 40,
                "records": 1,
                "cost": 1.25,
            }
        },
    }
    store = rpg.normalize_store(legacy)
    assert store["tracked_since"] == "2026-06-13"
    assert store["days"]["2026-06-13"]["claude"]["cache_read"] == 40
    assert rpg.provider_tokens("claude", store["days"]["2026-06-13"]["claude"]) == 100


def test_merge_is_monotonic_when_old_logs_disappear():
    old = {
        "days": {"2026-07-20": {"codex": usage(input=100, output=20, records=2)}}
    }
    partial_rescan = {
        "days": {"2026-07-20": {"codex": usage(input=60, output=10, records=1)}}
    }
    merged = rpg.merge_stores(old, partial_rescan)
    record = merged["days"]["2026-07-20"]["codex"]
    assert record["input"] == 100
    assert record["output"] == 20
    assert record["records"] == 2


def test_aggregate_claude_deduplicates_and_counts_cache(tmp_path):
    rows = [
        {
            "timestamp": "2026-07-20T01:00:00Z",
            "requestId": "request-1",
            "message": {
                "id": "message-1",
                "model": "claude-opus-4-8",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 30,
                        "ephemeral_1h_input_tokens": 40,
                    },
                    "cache_read_input_tokens": 50,
                },
            },
        }
    ]
    write_jsonl(tmp_path / "project" / "session.jsonl", rows + rows)
    days = rpg.aggregate_claude(tmp_path)
    record = days["2026-07-20"]
    assert record["input"] == 10
    assert record["output"] == 20
    assert record["cache_creation"] == 70
    assert record["cache_read"] == 50
    assert record["records"] == 1
    assert record["cost"] > 0


def codex_rows():
    return [
        {
            "timestamp": "2026-07-19T14:00:00Z",
            "type": "session_meta",
            "payload": {"id": "session-1", "source": "cli"},
        },
        {
            "timestamp": "2026-07-19T14:01:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 80,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 120,
                    }
                },
            },
        },
        {
            "timestamp": "2026-07-19T16:01:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 160,
                        "cached_input_tokens": 100,
                        "output_tokens": 35,
                        "reasoning_output_tokens": 8,
                        "total_tokens": 195,
                    }
                },
            },
        },
    ]


def test_aggregate_codex_diffs_cumulative_and_deduplicates_archived_copy(tmp_path):
    rows = codex_rows()
    write_jsonl(tmp_path / "sessions" / "2026" / "07" / "session.jsonl", rows)
    write_jsonl(tmp_path / "archived_sessions" / "session-copy.jsonl", rows)

    days = rpg.aggregate_codex(tmp_path, "Asia/Seoul")
    first = days["2026-07-19"]
    second = days["2026-07-20"]

    assert first["input"] == 100
    assert first["output"] == 20
    assert second["input"] == 60
    assert second["output"] == 15
    assert first["cached_input"] + second["cached_input"] == 100
    assert first["reasoning_output"] + second["reasoning_output"] == 8
    assert first["records"] + second["records"] == 2
    # Cached input and reasoning output are detail fields, not additive totals.
    assert sum(rpg.provider_tokens("codex", value) for value in days.values()) == 195


def test_codex_parser_ignores_partial_and_non_usage_lines(tmp_path):
    path = tmp_path / "sessions" / "session.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "x"}})
        + "\n"
        + json.dumps({"type": "response_item", "payload": {"message": "private"}})
        + "\n{partial",
        encoding="utf-8",
    )
    assert rpg.aggregate_codex(tmp_path) == {}


def test_level_curve_matches_current_example():
    state = rpg.level_state(2_875_254_193)
    assert state["level"] == 54
    assert state["progress"] == pytest.approx(0.4787906, rel=1e-5)
    assert state["remaining"] == 123_888_453


def test_level_200_requires_one_trillion_tokens():
    almost = rpg.level_state(rpg.MAX_TOKENS - 1)
    maximum = rpg.level_state(rpg.MAX_TOKENS)
    assert almost["level"] == 199
    assert almost["maxed"] is False
    assert maximum == {"level": 200, "progress": 1.0, "remaining": 0, "maxed": True}
    assert rpg.render_gauge(maximum).startswith("MAX " + "💠" * 10)


@pytest.mark.parametrize(
    ("level", "symbol"),
    [(1, "🟩"), (20, "🟦"), (40, "🟪"), (70, "🟨"), (100, "🟧"), (150, "🟥"), (200, "💠")],
)
def test_gauge_color_changes_by_level(level, symbol):
    assert rpg.gauge_symbol(level) == symbol


@pytest.mark.parametrize(
    ("claude_share", "codex_share", "expected"),
    [(0.9, 0.1, "Context Mage"), (0.1, 0.9, "Code Knight"), (0.5, 0.5, "Dual Caster")],
)
def test_character_class_uses_provider_affinity(claude_share, codex_share, expected):
    assert rpg.character_class(54, claude_share, codex_share) == expected


def test_render_card_has_five_profile_lines_plus_updated():
    store = rpg.normalize_store(
        {
            "days": {
                "2026-02-03": {"codex": usage(input=1_500_000)},
                "2026-06-13": {"claude": usage(input=2_873_700_000, cost=3112.96)},
                "2026-07-20": {"claude": usage(input=16_505)},
            }
        }
    )
    card = rpg.render_card(store, date(2026, 7, 20), "2026-07-20 23:00 KST")
    lines = card.rstrip().split("\n")
    assert len(lines) == 6
    assert lines[0].startswith("🧙 Lv.54/200 · Context Mage · created Feb 3, 2026")
    assert lines[1].startswith("XP 🟪")
    assert lines[2].startswith("📊 2.9B tokens · ~$3,112.96")
    assert lines[3].startswith("🔥 today ")
    assert "Claude 2.9B" in lines[4] and "Codex 1.5M" in lines[4]
    assert lines[5] == "🕐 updated 2026-07-20 23:00 KST"


def test_normalize_gist_id_accepts_url():
    assert rpg.normalize_gist_id("https://gist.github.com/user/abc123/") == "abc123"
    assert rpg.normalize_gist_id("abc123") == "abc123"


def test_push_due_respects_interval(tmp_path):
    now = datetime.fromisoformat("2026-07-20T12:00:00+09:00")
    marker = tmp_path / ".last_push"
    marker.write_text((now - timedelta(minutes=10)).isoformat(), encoding="utf-8")
    assert rpg.push_due(marker, now, 30) is False
    assert rpg.push_due(marker, now, 5) is True


def test_fetch_gist_store_reads_aggregate_backup():
    remote = {"days": {"2026-07-20": {"codex": usage(input=123)}}}
    response = {"files": {"ai-token-rpg-data.json": {"content": json.dumps(remote)}}}
    with mock.patch("ai_token_rpg.gh_api", return_value=json.dumps(response)):
        store = rpg.fetch_gist_store("gist", "ai-token-rpg-data.json", "Asia/Seoul")
    assert store["days"]["2026-07-20"]["codex"]["input"] == 123


def test_run_writes_local_card_and_data_without_push(tmp_path):
    args = SimpleNamespace(
        tz="Asia/Seoul",
        gist_id="",
        lock_path=str(tmp_path / ".lock"),
        data_path=str(tmp_path / "data.json"),
        card_path=str(tmp_path / "card.txt"),
        last_push_path=str(tmp_path / ".last_push"),
        import_legacy=None,
        claude_root=str(tmp_path / "claude"),
        codex_home=str(tmp_path / "codex"),
        push=False,
        force=False,
        push_interval_minutes=30,
        card_filename="ai-token-rpg",
        data_filename="ai-token-rpg-data.json",
    )
    with mock.patch("ai_token_rpg.aggregate_claude", return_value={"2026-07-20": usage(input=100)}):
        card, status = rpg.run(args)
    assert status is None
    assert "tokens" in card
    assert (tmp_path / "data.json").exists()
    assert (tmp_path / "card.txt").read_text(encoding="utf-8") == card

