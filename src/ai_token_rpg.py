#!/usr/bin/env python3
"""Turn local Claude Code and Codex token usage into an RPG-style Gist card.

Only timestamps, model identifiers, and token usage counters are retained.
Prompt, response, tool, and project contents are never written to the store.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9+ always has zoneinfo
    ZoneInfo = None

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


SCHEMA_VERSION = 1
DEFAULT_TZ = "Asia/Seoul"
MAX_LEVEL = 200
MAX_TOKENS = 1_000_000_000_000
LEVEL_EXPONENT = 4.5
GAUGE_SIZE = 10
PROVIDERS = ("claude", "codex")

# USD per 1M tokens: (input, output). Prefix matching; list-price estimate only.
CLAUDE_PRICING = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

HERE = Path(__file__).resolve().parent
DEFAULT_DATA_PATH = HERE / "data.json"
DEFAULT_CARD_PATH = HERE / "card.txt"
DEFAULT_LAST_PUSH_PATH = HERE / ".last_push"
DEFAULT_LOCK_PATH = HERE / ".token-rpg.lock"

COMPONENTS = (
    "input",
    "output",
    "cache_creation",
    "cache_read",
    "cached_input",
    "reasoning_output",
    "records",
)


def safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def safe_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def empty_usage() -> dict[str, Any]:
    return {
        "input": 0,
        "output": 0,
        "cache_creation": 0,
        "cache_read": 0,
        "cached_input": 0,
        "reasoning_output": 0,
        "records": 0,
        "cost": 0.0,
    }


def normalize_usage(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result = empty_usage()
    for key in COMPONENTS:
        result[key] = safe_int(source.get(key))
    result["cost"] = safe_float(source.get("cost"))
    return result


def provider_tokens(provider: str, usage: dict[str, Any]) -> int:
    total = safe_int(usage.get("input")) + safe_int(usage.get("output"))
    if provider == "claude":
        total += safe_int(usage.get("cache_creation"))
        total += safe_int(usage.get("cache_read"))
    return total


def empty_store(tz_name: str = DEFAULT_TZ) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tz": tz_name,
        "tracked_since": None,
        "days": {},
    }


def normalize_store(value: Any, tz_name: str = DEFAULT_TZ) -> dict[str, Any]:
    """Normalize current stores and migrate ai-usage-card's legacy day shape."""
    result = empty_store(tz_name)
    if not isinstance(value, dict):
        return result

    source_days = value.get("days")
    if not isinstance(source_days, dict):
        return result

    for day, raw_day in source_days.items():
        if not isinstance(day, str) or not isinstance(raw_day, dict):
            continue
        providers: dict[str, Any] = {}
        if any(provider in raw_day for provider in PROVIDERS):
            for provider in PROVIDERS:
                if isinstance(raw_day.get(provider), dict):
                    providers[provider] = normalize_usage(raw_day[provider])
        elif any(key in raw_day for key in ("input", "output", "cache_creation", "cache_read")):
            # Legacy ai-usage-card data was Claude-only.
            providers["claude"] = normalize_usage(raw_day)
        if providers:
            result["days"][day] = providers

    result["tz"] = str(value.get("tz") or tz_name)
    result["schema_version"] = SCHEMA_VERSION
    refresh_metadata(result)
    return result


def merge_usage(left: Any, right: Any) -> dict[str, Any]:
    """Monotonic merge protects history after source JSONL cleanup."""
    a = normalize_usage(left)
    b = normalize_usage(right)
    merged = empty_usage()
    for key in COMPONENTS:
        merged[key] = max(a[key], b[key])
    merged["cost"] = max(a["cost"], b["cost"])
    return merged


def merge_stores(*stores: Any, tz_name: str = DEFAULT_TZ) -> dict[str, Any]:
    merged = empty_store(tz_name)
    for raw_store in stores:
        store = normalize_store(raw_store, tz_name)
        for day, providers in store["days"].items():
            target = merged["days"].setdefault(day, {})
            for provider, usage in providers.items():
                target[provider] = merge_usage(target.get(provider), usage)
    refresh_metadata(merged)
    return merged


def refresh_metadata(store: dict[str, Any]) -> None:
    active = [
        day
        for day, providers in store.get("days", {}).items()
        if isinstance(providers, dict)
        and any(provider_tokens(p, normalize_usage(u)) > 0 for p, u in providers.items())
    ]
    store["tracked_since"] = min(active) if active else None


def store_from_provider(provider: str, days: dict[str, dict[str, Any]], tz_name: str) -> dict[str, Any]:
    store = empty_store(tz_name)
    for day, usage in days.items():
        store["days"][day] = {provider: normalize_usage(usage)}
    refresh_metadata(store)
    return store


def timezone(tz_name: str):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    return None


def parse_date(timestamp: Any, tz_name: str = DEFAULT_TZ) -> str | None:
    if not isinstance(timestamp, str) or len(timestamp) < 10:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return parsed.astimezone(timezone(tz_name)).date().isoformat()
    except (TypeError, ValueError):
        return None


def claude_rates(model: Any) -> tuple[float, float] | None:
    if not isinstance(model, str):
        return None
    for prefix, rates in CLAUDE_PRICING.items():
        if model.startswith(prefix):
            return rates
    return None


def claude_cache_creation(usage: dict[str, Any]) -> tuple[int, int]:
    nested = usage.get("cache_creation")
    nested = nested if isinstance(nested, dict) else {}
    five_minutes = safe_int(nested.get("ephemeral_5m_input_tokens"))
    one_hour = safe_int(nested.get("ephemeral_1h_input_tokens"))
    if not (five_minutes or one_hour):
        five_minutes = safe_int(usage.get("cache_creation_input_tokens"))
    return five_minutes, one_hour


def claude_record_cost(model: Any, usage: dict[str, Any]) -> float:
    rates = claude_rates(model)
    if rates is None:
        return 0.0
    input_rate, output_rate = rates
    cache_5m, cache_1h = claude_cache_creation(usage)
    return (
        safe_int(usage.get("input_tokens")) * input_rate
        + safe_int(usage.get("output_tokens")) * output_rate
        + cache_5m * input_rate * 1.25
        + cache_1h * input_rate * 2.0
        + safe_int(usage.get("cache_read_input_tokens")) * input_rate * 0.10
    ) / 1_000_000.0


def add_claude_usage(
    days: dict[str, dict[str, Any]],
    timestamp: Any,
    usage: dict[str, Any],
    model: Any,
    tz_name: str,
) -> None:
    day = parse_date(timestamp, tz_name)
    if day is None:
        return
    record = days.setdefault(day, empty_usage())
    cache_5m, cache_1h = claude_cache_creation(usage)
    record["input"] += safe_int(usage.get("input_tokens"))
    record["output"] += safe_int(usage.get("output_tokens"))
    record["cache_creation"] += cache_5m + cache_1h
    record["cache_read"] += safe_int(usage.get("cache_read_input_tokens"))
    record["cost"] += claude_record_cost(model, usage)
    record["records"] += 1


def aggregate_claude(root: str | Path, tz_name: str = DEFAULT_TZ) -> dict[str, dict[str, Any]]:
    days: dict[str, dict[str, Any]] = {}
    seen: set[tuple[Any, Any]] = set()
    pattern = str(Path(root).expanduser() / "**" / "*.jsonl")
    for filename in glob.glob(pattern, recursive=True):
        try:
            with open(filename, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(item, dict):
                        continue
                    message = item.get("message")
                    if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                        continue
                    message_id = message.get("id")
                    if message_id is not None:
                        key = (message_id, item.get("requestId"))
                        if key in seen:
                            continue
                        seen.add(key)
                    add_claude_usage(
                        days,
                        item.get("timestamp"),
                        message["usage"],
                        message.get("model"),
                        tz_name,
                    )
        except OSError as error:
            print(f"Claude log read error {filename}: {error}", file=sys.stderr)
    return days


def codex_usage_values(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    return {
        "input": safe_int(raw.get("input_tokens")),
        "output": safe_int(raw.get("output_tokens")),
        "cached_input": safe_int(raw.get("cached_input_tokens")),
        "reasoning_output": safe_int(raw.get("reasoning_output_tokens")),
    }


def codex_session_snapshots(filename: str) -> tuple[str, list[dict[str, Any]]]:
    session_id: str | None = None
    snapshots: list[dict[str, Any]] = []
    try:
        with open(filename, encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(item, dict):
                    continue
                payload = item.get("payload")
                if item.get("type") == "session_meta" and isinstance(payload, dict):
                    candidate = payload.get("id")
                    if isinstance(candidate, str) and candidate:
                        session_id = candidate
                if item.get("type") != "event_msg" or not isinstance(payload, dict):
                    continue
                if payload.get("type") != "token_count" or not isinstance(payload.get("info"), dict):
                    continue
                values = codex_usage_values(payload["info"].get("total_token_usage"))
                timestamp = item.get("timestamp")
                if values is not None and isinstance(timestamp, str):
                    snapshots.append({"timestamp": timestamp, **values})
    except OSError as error:
        print(f"Codex log read error {filename}: {error}", file=sys.stderr)
    return session_id or str(Path(filename).resolve()), snapshots


def codex_log_files(codex_home: str | Path) -> Iterable[str]:
    home = Path(codex_home).expanduser()
    for directory in (home / "sessions", home / "archived_sessions"):
        yield from glob.glob(str(directory / "**" / "*.jsonl"), recursive=True)


def aggregate_codex(codex_home: str | Path, tz_name: str = DEFAULT_TZ) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for filename in codex_log_files(codex_home):
        session_id, snapshots = codex_session_snapshots(filename)
        grouped.setdefault(session_id, []).extend(snapshots)

    days: dict[str, dict[str, Any]] = {}
    for snapshots in grouped.values():
        unique: dict[tuple[Any, ...], dict[str, Any]] = {}
        for snapshot in snapshots:
            key = (
                snapshot["timestamp"],
                snapshot["input"],
                snapshot["output"],
                snapshot["cached_input"],
                snapshot["reasoning_output"],
            )
            unique[key] = snapshot

        previous = {"input": 0, "output": 0, "cached_input": 0, "reasoning_output": 0}
        for snapshot in sorted(unique.values(), key=lambda value: value["timestamp"]):
            current = {key: snapshot[key] for key in previous}
            reset = any(current[key] < previous[key] for key in previous)
            delta = current if reset else {key: current[key] - previous[key] for key in previous}
            previous = current
            if delta["input"] + delta["output"] <= 0:
                continue
            day = parse_date(snapshot["timestamp"], tz_name)
            if day is None:
                continue
            record = days.setdefault(day, empty_usage())
            for key, value in delta.items():
                record[key] += value
            record["records"] += 1
    return days


def human(number: float | int) -> str:
    value = float(number)
    for unit, divisor in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if value >= divisor:
            return f"{value / divisor:.1f}{unit}"
    return str(int(value))


def level_threshold(level: int) -> float:
    if level <= 0:
        return 0.0
    return MAX_TOKENS * math.pow(level / MAX_LEVEL, LEVEL_EXPONENT)


def level_state(total_tokens: int) -> dict[str, Any]:
    if total_tokens >= MAX_TOKENS:
        return {
            "level": MAX_LEVEL,
            "progress": 1.0,
            "remaining": 0,
            "maxed": True,
        }
    raw_level = MAX_LEVEL * math.pow(max(0, total_tokens) / MAX_TOKENS, 1 / LEVEL_EXPONENT)
    level = max(1, min(MAX_LEVEL - 1, math.floor(raw_level)))
    start = 0.0 if level == 1 else level_threshold(level)
    end = level_threshold(level + 1)
    progress = 0.0 if end <= start else (total_tokens - start) / (end - start)
    progress = max(0.0, min(1.0, progress))
    return {
        "level": level,
        "progress": progress,
        "remaining": max(0, math.ceil(end - total_tokens)),
        "maxed": False,
    }


def gauge_symbol(level: int) -> str:
    if level <= 19:
        return "🟩"
    if level <= 39:
        return "🟦"
    if level <= 69:
        return "🟪"
    if level <= 99:
        return "🟨"
    if level <= 149:
        return "🟧"
    if level <= 199:
        return "🟥"
    return "💠"


def render_gauge(level_info: dict[str, Any]) -> str:
    if level_info["maxed"]:
        return "MAX " + "💠" * GAUGE_SIZE + f" {human(MAX_TOKENS)} XP"
    filled = min(GAUGE_SIZE, max(0, int(level_info["progress"] * GAUGE_SIZE + 0.5)))
    bar = gauge_symbol(level_info["level"]) * filled + "⬛" * (GAUGE_SIZE - filled)
    percent = level_info["progress"] * 100
    return (
        f"XP {bar} {percent:.1f}% · "
        f"{human(level_info['remaining'])} to Lv.{level_info['level'] + 1}"
    )


def provider_totals(store: dict[str, Any]) -> dict[str, int]:
    totals = {provider: 0 for provider in PROVIDERS}
    for providers in store.get("days", {}).values():
        if not isinstance(providers, dict):
            continue
        for provider in PROVIDERS:
            totals[provider] += provider_tokens(provider, normalize_usage(providers.get(provider)))
    return totals


def character_class(level: int, claude_share: float, codex_share: float) -> str:
    if level >= MAX_LEVEL:
        return "Token Ascendant"
    if claude_share >= 0.65:
        path = (
            (19, "Prompt Scribe"),
            (39, "Context Weaver"),
            (69, "Context Mage"),
            (99, "Reasoning Sage"),
            (149, "Oracle"),
            (199, "Cosmic Oracle"),
        )
    elif codex_share >= 0.65:
        path = (
            (19, "Code Scout"),
            (39, "Code Smith"),
            (69, "Code Knight"),
            (99, "Repo Architect"),
            (149, "Forge Master"),
            (199, "Code Sovereign"),
        )
    else:
        path = (
            (19, "Dual Novice"),
            (39, "Hybrid Adept"),
            (69, "Dual Caster"),
            (99, "AI Orchestrator"),
            (149, "Singularity Engineer"),
            (199, "Agent Overlord"),
        )
    return next(name for upper, name in path if level <= upper)


def combined_day_total(providers: Any) -> int:
    if not isinstance(providers, dict):
        return 0
    return sum(provider_tokens(provider, normalize_usage(providers.get(provider))) for provider in PROVIDERS)


def format_created(day: str | None) -> str:
    if not day:
        return "unknown"
    try:
        return datetime.fromisoformat(day).strftime("%b %d, %Y").replace(" 0", " ")
    except ValueError:
        return day


def render_card(store: dict[str, Any], today: date, updated: str) -> str:
    totals_by_day = {
        day: combined_day_total(providers)
        for day, providers in store.get("days", {}).items()
    }
    totals_by_day = {day: total for day, total in totals_by_day.items() if total > 0}
    providers = provider_totals(store)
    grand_total = sum(providers.values())
    total_cost = sum(
        normalize_usage(usage).get("cost", 0.0)
        for day in store.get("days", {}).values()
        if isinstance(day, dict)
        for usage in day.values()
    )

    if grand_total <= 0:
        return "🧙 Lv.1/200 · Token Novice · created unknown\nXP ⬛⬛⬛⬛⬛⬛⬛⬛⬛⬛ 0.0%\n📊 no usage data yet\n"

    level_info = level_state(grand_total)
    claude_share = providers["claude"] / grand_total
    codex_share = providers["codex"] / grand_total
    title = character_class(level_info["level"], claude_share, codex_share)
    level_label = "Lv.200 MAX" if level_info["maxed"] else f"Lv.{level_info['level']}/200"

    peak_day = max(totals_by_day, key=totals_by_day.get)
    peak_label = datetime.fromisoformat(peak_day).strftime("%b %d").replace(" 0", " ")
    today_tokens = totals_by_day.get(today.isoformat(), 0)

    return "\n".join(
        [
            f"🧙 {level_label} · {title} · created {format_created(store.get('tracked_since'))}",
            render_gauge(level_info),
            f"📊 {human(grand_total)} tokens · ~${total_cost:,.2f}",
            f"🔥 today {human(today_tokens)} · peak {human(totals_by_day[peak_day])} ({peak_label})",
            (
                f"🤖 Claude {human(providers['claude'])} ({claude_share * 100:.1f}%) · "
                f"Codex {human(providers['codex'])} ({codex_share * 100:.1f}%)"
            ),
            f"🕐 updated {updated}",
        ]
    ) + "\n"


def load_json_file(path: str | Path, tz_name: str = DEFAULT_TZ) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            return normalize_store(json.load(handle), tz_name)
    except (OSError, json.JSONDecodeError, TypeError):
        return empty_store(tz_name)


def atomic_write(path: str | Path, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def save_store(store: dict[str, Any], path: str | Path) -> None:
    atomic_write(path, json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


@contextmanager
def file_lock(path: str | Path):
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def normalize_gist_id(value: str | None) -> str:
    candidate = (value or "").strip().rstrip("/")
    return candidate.rsplit("/", 1)[-1] if candidate else ""


def gh_api(method: str, endpoint: str, payload: str | None = None) -> str:
    command = ["gh", "api"]
    if method != "GET":
        command.extend(["-X", method])
    command.append(endpoint)
    if payload is not None:
        command.extend(["--input", "-"])
    completed = subprocess.run(
        command,
        input=payload,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip()[:500])
    return completed.stdout


def fetch_gist_store(gist_id: str, data_filename: str, tz_name: str) -> dict[str, Any] | None:
    response = json.loads(gh_api("GET", f"/gists/{gist_id}"))
    files = response.get("files") if isinstance(response, dict) else None
    if not isinstance(files, dict) or not isinstance(files.get(data_filename), dict):
        return None
    content = files[data_filename].get("content")
    if not isinstance(content, str):
        return None
    return normalize_store(json.loads(content), tz_name)


def push_gist(
    gist_id: str,
    card_filename: str,
    data_filename: str,
    card: str,
    store: dict[str, Any],
) -> None:
    payload = json.dumps(
        {
            "files": {
                card_filename: {"content": card},
                data_filename: {
                    "content": json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                },
            }
        },
        ensure_ascii=False,
    )
    gh_api("PATCH", f"/gists/{gist_id}", payload)


def push_due(path: str | Path, now: datetime, interval_minutes: int) -> bool:
    try:
        previous = datetime.fromisoformat(Path(path).read_text(encoding="utf-8").strip())
        if previous.tzinfo is None and now.tzinfo is not None:
            previous = previous.replace(tzinfo=now.tzinfo)
        return now - previous >= timedelta(minutes=max(0, interval_minutes))
    except (OSError, ValueError, TypeError):
        return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render an RPG card from Claude and Codex token usage")
    parser.add_argument("--push", action="store_true", help="sync aggregate JSON and card to a Gist")
    parser.add_argument("--force", action="store_true", help="bypass the Gist push interval")
    parser.add_argument("--quiet", action="store_true", help="do not print the card")
    parser.add_argument("--gist-id", default=os.environ.get("AI_TOKEN_RPG_GIST_ID", ""))
    parser.add_argument("--tz", default=os.environ.get("AI_TOKEN_RPG_TZ", DEFAULT_TZ))
    parser.add_argument("--claude-root", default=os.environ.get("CLAUDE_LOG_ROOT", "~/.claude/projects"))
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", "~/.codex"))
    parser.add_argument("--data-path", default=str(DEFAULT_DATA_PATH))
    parser.add_argument("--card-path", default=str(DEFAULT_CARD_PATH))
    parser.add_argument("--last-push-path", default=str(DEFAULT_LAST_PUSH_PATH))
    parser.add_argument("--lock-path", default=str(DEFAULT_LOCK_PATH))
    parser.add_argument("--import-legacy", help="merge a legacy ai-usage-card data.json")
    parser.add_argument(
        "--push-interval-minutes",
        type=int,
        default=int(os.environ.get("AI_TOKEN_RPG_PUSH_INTERVAL_MINUTES", "30")),
    )
    parser.add_argument(
        "--card-filename",
        default=os.environ.get("AI_TOKEN_RPG_CARD_FILENAME", "ai-token-rpg"),
    )
    parser.add_argument(
        "--data-filename",
        default=os.environ.get("AI_TOKEN_RPG_DATA_FILENAME", "ai-token-rpg-data.json"),
    )
    return parser


def run(args: argparse.Namespace) -> tuple[str, str | None]:
    tz = timezone(args.tz)
    now = datetime.now(tz)
    gist_id = normalize_gist_id(args.gist_id)

    with file_lock(args.lock_path):
        local = load_json_file(args.data_path, args.tz)
        legacy = load_json_file(args.import_legacy, args.tz) if args.import_legacy else None
        claude = store_from_provider("claude", aggregate_claude(args.claude_root, args.tz), args.tz)
        codex = store_from_provider("codex", aggregate_codex(args.codex_home, args.tz), args.tz)
        store = merge_stores(local, legacy, claude, codex, tz_name=args.tz)
        store["updated_at"] = now.isoformat(timespec="seconds")
        save_store(store, args.data_path)

        updated = now.strftime("%Y-%m-%d %H:%M %Z")
        card = render_card(store, now.date(), updated)
        atomic_write(args.card_path, card)

        status: str | None = None
        should_push = args.push and (args.force or push_due(args.last_push_path, now, args.push_interval_minutes))
        if args.push and not gist_id:
            status = "skipped (AI_TOKEN_RPG_GIST_ID not set)"
        elif should_push:
            try:
                remote = fetch_gist_store(gist_id, args.data_filename, args.tz)
                if remote is not None:
                    store = merge_stores(remote, store, tz_name=args.tz)
                    store["updated_at"] = now.isoformat(timespec="seconds")
                    save_store(store, args.data_path)
                    card = render_card(store, now.date(), updated)
                    atomic_write(args.card_path, card)
                push_gist(gist_id, args.card_filename, args.data_filename, card, store)
                atomic_write(args.last_push_path, now.isoformat(timespec="seconds"))
                status = "pushed"
            except (RuntimeError, json.JSONDecodeError, OSError) as error:
                status = f"push failed: {error}"
        elif args.push:
            status = "skipped (push interval)"
        return card, status


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    card, status = run(args)
    if status and (not args.quiet or status.startswith("push failed")):
        print(status, file=sys.stderr)
    if not args.quiet:
        sys.stdout.write(card)
    return 0 if not status or not status.startswith("push failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
