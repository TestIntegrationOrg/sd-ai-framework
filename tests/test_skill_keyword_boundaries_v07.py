from __future__ import annotations

from pathlib import Path

import yaml

from sdai.skill_resolution import resolve_skills


def _init(root: Path) -> None:
    path = root / ".sdai" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("version: 1\noperating_mode: individual\n", encoding="utf-8")


def _agent(root: Path) -> None:
    path = root / ".sdai" / "agents" / "developer.agent.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
name: developer
description: Provider-neutral developer role.
capabilities: [coding]
skills: []
execution_mode: advisory
providers: {}
---

Implement approved behavior.
""",
        encoding="utf-8",
    )


def _skill(root: Path, name: str, keywords: list[str]) -> None:
    skill_root = root / ".agents" / "skills" / name
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text(
        f"""---
name: {name}
description: Boundary matching fixture.
---

# {name}

Use only when task keywords match.
""",
        encoding="utf-8",
    )
    (skill_root / "sdai.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "capabilities": ["coding"],
                "compatibility": {},
                "selection": {
                    "auto": True,
                    "capabilities": ["coding"],
                    "task_keywords": keywords,
                },
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )


def _selected(root: Path, task: str) -> tuple[str, ...]:
    return resolve_skills(
        root,
        agent_name="developer",
        capability="coding",
        task=task,
    ).selected


def test_single_token_keyword_does_not_match_prefix_or_suffix_substrings(tmp_path: Path) -> None:
    _init(tmp_path)
    _agent(tmp_path)
    _skill(tmp_path, "bug-discipline", ["bug"])

    assert _selected(tmp_path, "debug failing regression") == ()
    assert _selected(tmp_path, "investigate buggy behavior") == ()
    assert _selected(tmp_path, "fix a bug") == ("bug-discipline",)
    assert _selected(tmp_path, "BUG: request fails") == ("bug-discipline",)
    assert _selected(tmp_path, "bug-fix validation") == ("bug-discipline",)


def test_keyword_does_not_match_inside_longer_word(tmp_path: Path) -> None:
    _init(tmp_path)
    _agent(tmp_path)
    _skill(tmp_path, "api-discipline", ["api"])

    assert _selected(tmp_path, "review capital allocation") == ()
    assert _selected(tmp_path, "review the API contract") == ("api-discipline",)


def test_multi_word_phrase_requires_phrase_boundaries(tmp_path: Path) -> None:
    _init(tmp_path)
    _agent(tmp_path)
    _skill(tmp_path, "review-discipline", ["security review"])

    assert _selected(tmp_path, "perform a security review before release") == (
        "review-discipline",
    )
    assert _selected(tmp_path, "perform a security reviewer handoff") == ()


def test_unicode_casefolded_keyword_matching_is_deterministic(tmp_path: Path) -> None:
    _init(tmp_path)
    _agent(tmp_path)
    _skill(tmp_path, "unicode-discipline", ["café"])

    assert _selected(tmp_path, "review CAFÉ migration behavior") == (
        "unicode-discipline",
    )
    assert _selected(tmp_path, "review cafeteria migration") == ()


def test_explainability_reports_original_configured_keyword(tmp_path: Path) -> None:
    _init(tmp_path)
    _agent(tmp_path)
    _skill(tmp_path, "mixed-case", ["API Review"])

    report = resolve_skills(
        tmp_path,
        agent_name="developer",
        capability="coding",
        task="perform api review now",
    )
    decision = next(item for item in report.decisions if item.name == "mixed-case")

    assert report.selected == ("mixed-case",)
    assert decision.reasons == (
        "auto-selection enabled",
        "task keyword 'API Review' matched",
    )
