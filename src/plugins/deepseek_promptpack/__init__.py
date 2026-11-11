"""Archived prompt adaptation; the source/provenance files remain on disk.

Owner operating preferences now come from the full desktop AGENTS document in
qq_owner_policy. No old condensed policy is injected into ordinary QQ/Feishu.
"""
SECTION_ID = 'deepseek-promptpack.operating-specification'


def eligible(info):
    return False


def section(info):
    return ''


def register(ctx):
    # Existing frozen sessions pick up this removal after their next reset.
    return None
