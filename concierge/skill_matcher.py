# SPDX-License-Identifier: MIT
"""Skill matcher -- score bounties against a contributor's skill set and
recommend the best fits.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List

# Default skill tags.  Overridden at runtime if data/skill_tags.json exists.
# Supports two formats:
#   - Flat list:   {"python": ["python", "flask", ...]}
#   - Structured:  {"python": {"aliases": ["py"], "bounty_labels": ["python", "backend"]}}
SKILL_TAGS: Dict[str, List[str]] = {
    "python": ["python", "flask", "pytest", "pip", "django"],
    "rust": ["rust", "cargo", "crate"],
    "javascript": ["javascript", "node", "npm", "react", "typescript"],
    "docker": ["docker", "compose", "container", "dockerfile"],
    "ci-cd": ["ci", "cd", "github actions", "workflow", "lint"],
    "documentation": ["docs", "readme", "contributing", "translation", "guide"],
    "security": [
        "security", "audit", "vulnerability", "red team", "fuzz", "pentest",
    ],
    "social-media": ["star", "share", "tweet", "post", "upvote", "review"],
    "blockchain": [
        "blockchain", "token", "wallet", "attestation", "mining",
    ],
    "testing": ["test", "pytest", "coverage", "benchmark"],
}


def _normalise_tags(raw: Dict) -> Dict[str, List[str]]:
    """Normalise skill_tags.json into flat {skill: [keywords]} format.

    Accepts both structured (aliases + bounty_labels) and flat (list) formats.
    For structured entries, the keyword list is built from the skill name
    itself, its aliases, and its bounty_labels.
    """
    result: Dict[str, List[str]] = {}
    for skill, value in raw.items():
        if isinstance(value, list):
            result[skill] = value
        elif isinstance(value, dict):
            keywords = [skill]
            keywords.extend(value.get("aliases", []))
            keywords.extend(value.get("bounty_labels", []))
            # Deduplicate while preserving order
            seen = set()
            unique = []
            for kw in keywords:
                kw_lower = kw.lower()
                if kw_lower not in seen:
                    seen.add(kw_lower)
                    unique.append(kw_lower)
            result[skill] = unique
        else:
            result[skill] = [skill]
    return result


# Try to load the authoritative copy from data/skill_tags.json.
_DATA_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "skill_tags.json"
)
if os.path.isfile(_DATA_FILE):
    try:
        with open(_DATA_FILE, "r") as fh:
            _loaded = json.load(fh)
        if isinstance(_loaded, dict):
            SKILL_TAGS = _normalise_tags(_loaded)
    except (json.JSONDecodeError, OSError):
        pass  # fall back to built-in defaults


def _bounty_text(bounty: dict) -> str:
    """Combine all searchable text fields from a bounty into one string."""
    parts = [
        bounty.get("title", ""),
        bounty.get("body", ""),
        " ".join(bounty.get("labels", [])),
        bounty.get("difficulty", ""),
    ]
    return " ".join(parts).lower()


def match_skills(bounty: dict, skills: List[str]) -> float:
    """Score how well *bounty* matches the given *skills* (0.0 -- 1.0).

    *skills* is a list of skill category names (keys of ``SKILL_TAGS``),
    e.g. ``["python", "security"]``.
    """
    if not skills:
        return 0.0

    text = _bounty_text(bounty)
    if not text.strip():
        return 0.0

    matched = 0
    for skill in skills:
        keywords = SKILL_TAGS.get(skill.lower(), [skill.lower()])
        if any(kw in text for kw in keywords):
            matched += 1

    return matched / len(skills)


def recommend(
    bounties: List[dict],
    skills: List[str],
    limit: int = 10,
) -> List[dict]:
    """Return the top *limit* bounties matching *skills*, sorted by score.

    Each returned dict is the original bounty dict with an extra
    ``match_score`` key (float, 0.0--1.0).
    """
    scored = []
    for bounty in bounties:
        score = match_skills(bounty, skills)
        entry = dict(bounty)
        entry["match_score"] = score
        scored.append(entry)

    scored.sort(key=lambda b: b["match_score"], reverse=True)
    return scored[:limit]
