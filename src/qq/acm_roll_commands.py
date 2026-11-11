"""Dependency-free /roll argument parsing for external Hermes bridges.

This module performs no I/O. Copying it beside a bridge does not require
installing the ACM web application or expose login/session material.
"""
import re

USAGE = ('用法：/roll [800～3500 分数] [题型表达式]；例如 /roll dp&博弈论、'
         '/roll 1800 dp|博弈论。省略分数沿用本聊天上次分数或网页训练目标。')


def valid_score(value):
    return type(value) is int and 800 <= value <= 3500


def parse_roll(argument):
    """Return explicit score (or None), topic text (or None).

    & and comma mean intersection, | means union; & binds before |.
    Semantic topic aliases remain the authenticated server's responsibility.
    """
    if not isinstance(argument, str) or len(argument) > 655:
        raise ValueError(USAGE)
    text = argument.strip()
    parts = text.split(None, 1)
    score = None
    if parts and re.fullmatch(r'[+-]?\d+(?:\.\d+)?', parts[0]):
        if not re.fullmatch(r'[0-9]{3,4}', parts[0]) or not valid_score(int(parts[0])):
            raise ValueError(USAGE)
        score = int(parts[0])
        topic = parts[1].strip() if len(parts) > 1 else None
    else:
        topic = text or None
    if topic is not None:
        if len(topic) > 647 or any(symbol in topic for symbol in '()（）'):
            raise ValueError(USAGE)
        terms = re.split('[&|,，]', topic)
        if len(terms) > 8 or any(not 1 <= len(term.strip()) <= 80 for term in terms):
            raise ValueError(USAGE)
    return score, topic


def select_score(previous=None, training=None):
    """Use only a valid score from the current scope or authenticated settings."""
    if valid_score(previous):
        return previous
    if isinstance(training, dict):
        goal = training.get('goal_rating')
        if valid_score(goal):
            return goal
        low, high = training.get('minimum'), training.get('maximum')
        if valid_score(low) and valid_score(high) and low <= high:
            return (low + high) // 2
    raise ValueError('没有本聊天的有效历史分数，网页训练目标或区间也未有效配置；'
                     '请先设置训练目标，或明确输入 /roll 1800 [题型]。')
