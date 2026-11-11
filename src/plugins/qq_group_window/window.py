"""Deterministic, request-only selection of complete conversational rounds.

No durable text store and no model calls. Replaying all rounds to find the last
batch boundary makes the selection survive agent recreation and process restarts.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable


_GROUP_ID = re.compile(r"hermes-onebotv11\+group\+[0-9]+(?:\+[0-9]+)?(?:-g[0-9]+)?\Z")
_GROUP_ROUTE = re.compile(r"agent:[^:]+:api_server:dm:(hermes-onebotv11\+group\+[0-9]+(?:\+[0-9]+)?(?:-g[0-9]+)?)\Z")
_MEMORY_ROUTE = re.compile(r"agent:[^:]+:nonebot-onebotv11:group:[0-9]+\Z")


def qq_group_identity(value: Any) -> bool:
    """Only known bridge-generated group routes; never inspect message text."""
    return isinstance(value, str) and bool(
        _GROUP_ID.fullmatch(value) or _GROUP_ROUTE.fullmatch(value) or _MEMORY_ROUTE.fullmatch(value))


@dataclass(frozen=True)
class WindowSettings:
    max_turns: int = 12
    retain_turns: int = 6
    max_chars: int = 24_000
    target_chars: int = 12_000

    @classmethod
    def from_config(cls, config: Any) -> "WindowSettings":
        values = config if isinstance(config, dict) else {}
        def integer(key: str, default: int, floor: int, ceiling: int) -> int:
            value = values.get(key, default)
            if isinstance(value, bool):
                return default
            try:
                return max(floor, min(int(value), ceiling))
            except (TypeError, ValueError, OverflowError):
                return default
        turns = integer("max_turns", 12, 2, 100)
        chars = integer("max_chars", 24_000, 1_000, 500_000)
        return cls(turns, integer("retain_turns", 6, 1, turns - 1), chars,
                   integer("target_chars", chars // 2, 500, chars - 1))


_SYNTHETIC_FLAGS = (
    "_todo_snapshot_synthetic", "_empty_recovery_synthetic", "_verification_stop_synthetic",
    "_pre_verify_synthetic", "_dropped_toolcall_nudge", "_is_context_summary",
    "_compressed_summary", "_is_summary",
)
_SYNTHETIC_PREFIXES = (
    "[System: Your previous response was truncated", "[System: The previous response was cut off",
    "[System: Your previous tool call", "[Your active task list was preserved across context compression]",
    "[IMPORTANT: Background process ",
)


def is_real_user(message: dict[str, Any]) -> bool:
    """Fallback for standalone use; the engine also uses Hermes' own predicate."""
    if message.get("role") != "user" or any(message.get(k) for k in _SYNTHETIC_FLAGS):
        return False
    source = message.get("source")
    if isinstance(source, str) and source in {"synthetic", "system", "compression", "tool"}:
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip()) and not content.lstrip().startswith(_SYNTHETIC_PREFIXES)
    return bool(content)


def serialized_chars(message: dict[str, Any]) -> int:
    return len(json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str))


def _close_tool_boundary(messages: list[dict[str, Any]], cut: int, starts: list[int]) -> int:
    """A rare interleaved user steer cannot split an assistant/tool protocol pair."""
    origins = {}
    for index, message in enumerate(messages):
        for call in message.get("tool_calls") or ():
            if isinstance(call, dict) and call.get("id"):
                origins[call["id"]] = index
    while cut:
        required = cut
        for message in messages[cut:]:
            if message.get("role") == "tool":
                origin = origins.get(message.get("tool_call_id"), cut)
                required = min(required, origin)
        if required == cut:
            return cut
        # Keep the complete enclosing older round, rather than only the call row.
        cut = max((start for start in starts if start <= required), default=0)
    return cut


def select_window(
    messages: list[dict[str, Any]], settings: WindowSettings = WindowSettings(),
    *, real_user: Callable[[dict[str, Any]], bool] = is_real_user,
    user_flags: list[bool] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Return unchanged message objects in a new list; never mutate any input.

    max_turns is a high-water mark: round 13 starts a new 6-round batch;
    rounds 14..19 append to that exact prefix. The character limit also uses
    hysteresis and removes whole rounds. An oversized current round stays whole.
    """
    starts = [i for i, message in enumerate(messages)
              if (user_flags[i] if user_flags is not None else real_user(message))]
    if not starts:
        return list(messages), {"total_turns": 0, "retained_turns": 0, "dropped_turns": 0,
                                "history_chars": 0, "over_budget_current_turn": 0}
    ends = starts[1:] + [len(messages)]
    sizes = [sum(serialized_chars(m) for m in messages[start:end])
             for start, end in zip(starts, ends)]
    anchor, running = 0, 0
    for current, size in enumerate(sizes):
        running += size
        if current - anchor + 1 > settings.max_turns:
            new_anchor = current - settings.retain_turns + 1
            running -= sum(sizes[anchor:new_anchor])
            anchor = new_anchor
        if running > settings.max_chars:
            while anchor < current and running > settings.target_chars:
                running -= sizes[anchor]
                anchor += 1
    # Keep the complete fixed system/developer prefix. Old summaries / scaffolding
    # before the first genuine turn remain only until a real window cut is made.
    prefix_end = 0
    while prefix_end < len(messages) and messages[prefix_end].get("role") in {"system", "developer"}:
        prefix_end += 1
    cut = _close_tool_boundary(messages, starts[anchor], starts) if anchor else prefix_end
    selected = messages[:prefix_end] + messages[cut:]
    actual_dropped = sum(start < cut for start in starts)
    return selected, {"total_turns": len(starts), "retained_turns": len(starts) - actual_dropped,
                      "dropped_turns": actual_dropped,
                      "history_chars": sum(serialized_chars(m) for m in messages[cut:]),
                      "over_budget_current_turn": int(sizes[-1] > settings.max_chars)}
