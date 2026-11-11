"""Stable QQ group response style; no model calls, history edits or token caps.

Install after qq_control.install_client_adapter() and qq_window_bridge.install().
This module appends a fixed policy to the complete original overlay, preserving
its rules, personality and trusted identity. Private chats and other adapters
remain byte-for-byte unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


GROUP_REPLY_POLICY = (
    "\n\nQQ 群聊回复风格（仅影响表达长度）：\n"
    "保持原有人设语气；既有群规、安全规则、可信权限和刷题题解边界优先，本规则不授予任何权限。"
    "普通群聊默认用 1～3 句话，约 80～150 个中文字符；能一句说清就更短，不为凑字数扩写。"
    "先给结论，仅补必要的一句原因或操作；不要重复题问、复述上下文、额外总结、客套寒暄，默认不分多段。"
    "用户明确要求详细、展开、步骤、完整代码或报告，或技术问题确实需要更多内容时，按需完整回答；"
    "上述字数只是普通聊天的默认目标，不是硬截断上限，不能省掉关键条件、必要步骤或截断代码与公式。"
    "要求详细也不能改变既有权限、群规或提前泄露刷题解法。"
    "reactive 结构化模式只将本风格用于 reply_text，仍完整遵守原 JSON 协议及所有字段要求；"
    "原本应 should_reply=false 时继续不回复，不得为了短答改成 true。"
    "不改变工具使用、工具结果的必要准确性、刷题判定标准或内部推理。"
)


def wrap_overlay(original: Callable[..., str]) -> Callable[..., str]:
    """Append a constant group-only policy after the existing complete overlay."""
    def overlay(args: Mapping[str, Any], prompt: Any) -> str:
        result = original(args, prompt)
        if (
            args.get("adapter_name") == "onebotv11"
            and args.get("is_private") is False
            and args.get("group_id")
        ):
            return result + GROUP_REPLY_POLICY
        return result
    return overlay


def install(control: Any = None) -> None:
    """Idempotent integration; lazy import keeps isolated tests dependency-free."""
    if control is None:
        import qq_control as control
    if getattr(control, "_qq_concise_policy_installed", False):
        return
    if not getattr(getattr(control, "hermes_client", None), "_qq_control_installed", False):
        raise RuntimeError("install qq_control client adapter before qq_concise_policy")
    control.overlay = wrap_overlay(control.overlay)
    control._qq_concise_policy_installed = True
