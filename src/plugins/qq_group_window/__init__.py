"""QQ ContextEngine plugin; other conversations keep ContextCompressor behavior."""
from __future__ import annotations

import copy
import json
import logging
import sys
from typing import Any

from agent.context_compressor import ContextCompressor
from .window import WindowSettings, is_real_user, qq_group_identity, select_window

logger = logging.getLogger(__name__)
def _config() -> dict[str, Any]:
    # The host resolves the active profile and merges defaults. Never read a fixed
    # global config path or copy this config into telemetry.
    from hermes_cli.config import load_config_readonly
    return load_config_readonly()


def _compressor_options(config: dict[str, Any]) -> dict[str, Any]:
    compression = config.get("compression") or {}
    model = config.get("model") or {}
    if not isinstance(model, dict):
        model = {}
    mapping = {
        "threshold": "threshold_percent", "target_ratio": "summary_target_ratio",
        "protect_first_n": "protect_first_n", "protect_last_n": "protect_last_n",
        "abort_on_summary_failure": "abort_on_summary_failure", "model_thresholds": "model_thresholds",
        "threshold_tokens": "threshold_tokens_cap", "proactive_prune_tokens": "proactive_prune_tokens",
        "proactive_prune_min_result_chars": "proactive_prune_min_result_chars",
        "proactive_prune_min_reclaim_tokens": "proactive_prune_min_reclaim_tokens",
        "min_tail_user_messages": "min_tail_user_messages", "tail_mode": "tail_mode",
    }
    options = {target: copy.deepcopy(compression[source]) for source, target in mapping.items()
               if source in compression}
    options.update(model=str(model.get("default") or ""), quiet_mode=True,
                   config_context_length=model.get("context_length"), max_tokens=model.get("max_tokens"),
                   custom_providers=copy.deepcopy(config.get("custom_providers") or None))
    return options


class QQGroupWindow(ContextCompressor):
    """Complete-round batched windows for authenticated QQ bridge group routes.

    Selection changes only the outgoing request; the original history stays in
    the Hermes database. No summary, prune, or micro-compaction runs for a group.
    The configured built-in compressor remains active on every other surface.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        config = _config() if config is None else config
        super().__init__(**_compressor_options(config))
        context = config.get("context") or {}
        self.window_settings = WindowSettings.from_config(context.get("qq_group_window"))
        self._qq_group = False
        self._platform = ""
        self._window_stats: dict[str, int] = {}
        self._window_usage: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "qq_group_window"

    def clone_for_agent(self) -> "QQGroupWindow":
        # The registered template has no live state. Reconstruct to pick up the
        # active profile's latest compression settings without copying DB locks.
        return type(self)()

    def _session_is_group(self, session_id: str, conversation_id: Any) -> bool:
        if self._platform != "api_server":
            return False
        if qq_group_identity(session_id) or qq_group_identity(conversation_id):
            return True
        db = getattr(self, "_session_db", None)
        getter = getattr(db, "get_session", None)
        current, seen = session_id, set()
        for _ in range(16):
            if not current or current in seen or not callable(getter):
                break
            seen.add(current)
            try:
                row = getter(current)
            except Exception:
                break
            if not isinstance(row, dict):
                break
            if (qq_group_identity(row.get("session_key")) or qq_group_identity(row.get("gateway_session_key"))
                    or qq_group_identity(row.get("id"))):
                return True
            current = row.get("parent_session_id")
        return False

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        self._platform = str(kwargs.get("platform") or "")
        super().on_session_start(session_id, **kwargs)
        self._qq_group = self._session_is_group(session_id, kwargs.get("conversation_id"))
        self._window_stats = {}
        self._window_usage = {}

    def on_session_reset(self) -> None:
        super().on_session_reset()
        self._window_stats = {}
        self._window_usage = {}

    @staticmethod
    def _real_user(message: dict[str, Any]) -> bool:
        if not is_real_user(message):
            return False
        # Use the already-loaded compressor's summary predicate. Importing the
        # entire conversation loop here would unnecessarily bootstrap the runtime
        # merely to classify a row (and can initialise a fresh profile in probes).
        if "agent.conversation_loop" in sys.modules:
            return not ContextCompressor._is_synthetic_compression_user_turn(message)
        # Direct loader probes run before a conversation loop exists. The pure
        # predicate already rejects provenance flags and common runtime markers.
        return True

    def select_context(self, request_messages: list[dict[str, Any]], *,
                       conversation_messages: list[dict[str, Any]] | None = None,
                       incoming_message: dict[str, Any] | None = None,
                       budget_tokens: int = 0) -> list[dict[str, Any]] | None:
        if not self._qq_group:
            return super().select_context(request_messages, conversation_messages=conversation_messages,
                                          incoming_message=incoming_message, budget_tokens=budget_tokens)
        # Request sanitizers can remove provenance-only keys. Reattach only the
        # boolean real-user classification by ordered message identity, never by
        # a user-provided marker or a global content lookup (duplicates are valid).
        source_users = [m for m in conversation_messages or () if m.get("role") == "user"]
        cursor, flags = 0, []
        for message in request_messages:
            flag = self._real_user(message)
            if message.get("role") == "user":
                for index in range(cursor, len(source_users)):
                    if source_users[index].get("content") == message.get("content"):
                        flag = self._real_user(source_users[index])
                        cursor = index + 1
                        break
            flags.append(flag)
        selected, self._window_stats = select_window(request_messages, self.window_settings, user_flags=flags)
        return selected

    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        return False if self._qq_group else super().should_compress(prompt_tokens)

    def should_compress_info(self, prompt_tokens: int | None = None) -> tuple[bool, str | None]:
        return (False, None) if self._qq_group else super().should_compress_info(prompt_tokens)

    def should_compress_preflight(self, messages: list[dict[str, Any]]) -> bool:
        return False if self._qq_group else super().should_compress_preflight(messages)

    def should_defer_preflight_to_real_usage(self, rough_tokens: int) -> bool:
        return True if self._qq_group else super().should_defer_preflight_to_real_usage(rough_tokens)

    def prune_tool_results_only(self, messages: list[dict[str, Any]], current_tokens: int | None = None):
        return (messages, 0) if self._qq_group else super().prune_tool_results_only(messages, current_tokens)

    def _micro_compact(self, messages: list[dict[str, Any]], *, turn_session_id: str | None = None):
        return messages if self._qq_group else super()._micro_compact(messages, turn_session_id=turn_session_id)

    def compress(self, messages: list[dict[str, Any]], current_tokens: int | None = None,
                 focus_topic: str | None = None, force: bool = False, memory_context: str = "",
                 bypass_cooldown: bool = False) -> list[dict[str, Any]]:
        if self._qq_group:
            # Never commit a request-only window into the durable transcript.
            # A current round larger than the provider window remains whole;
            # the host's bounded overflow path reports it instead of corrupting
            # tool pairs, truncating the task, or making paid summary calls.
            return messages
        return super().compress(messages, current_tokens=current_tokens, focus_topic=focus_topic,
                                force=force, memory_context=memory_context, bypass_cooldown=bypass_cooldown)

    def on_turn_complete(self, messages: list[dict[str, Any]], usage: dict[str, Any] | None = None,
                         **kwargs: Any) -> None:
        super().on_turn_complete(messages, usage=usage, **kwargs)
        if self._qq_group:
            self._window_usage = {key: max(0, int((usage or {}).get(key) or 0))
                                  for key in ("input_tokens", "output_tokens", "cache_read_tokens", "prompt_tokens")}
            # Numeric observability only: no chat identifiers, message bodies,
            # API credentials, tool arguments, or model-generated text.
            logger.info("QQ context window telemetry %s", json.dumps(
                {**self._window_stats, **self._window_usage}, sort_keys=True))

    def get_status(self) -> dict[str, Any]:
        status = super().get_status()
        status["qq_group_window"] = {"active": self._qq_group, **self._window_stats, **self._window_usage}
        return status


def register(ctx):
    ctx.register_context_engine(QQGroupWindow())
