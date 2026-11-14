"""Trackmania display text helpers."""

import re


def plain_name(name):
    """Remove ManiaPlanet formatting while preserving escaped dollar signs."""
    return re.sub(
        r"\$\$|\$(?:[0-9a-f]{3}|[lhp](?:\[[^\]]*\])?|[bgimnostuwz<>])",
        lambda match: "$" if match.group() == "$$" else "",
        name, flags=re.IGNORECASE,
    )
