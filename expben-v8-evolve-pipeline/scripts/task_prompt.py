#!/usr/bin/env python3
"""Shared preparation of the agent-visible prompt and README.

Both are shipped inside every materialized image, so the rules live in one place
and every build path imports them:

* ``/testbed/problem/prompt`` -- the upstream prompt, minus anything that
  describes the pre-evolve bug or admits the task is synthetic.
* ``/testbed/README.md`` -- byte-identical to the official test-set images apart
  from the bug id, so the scoring prose an agent reads matches the grader.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
README_TEMPLATE = TEMPLATE_DIR / "task-image-README.md"

# The overlay an earlier revision appended to the prompt.  It names the evolve
# direction and tells the agent the source is already mutated, which is exactly
# what the shipped image must not say.
EVOLUTION_OVERLAY_MARKER = "--- Phase-2 evolution overlay ---"
BUG_DESCRIPTION_MARKER = "Bug Description:"
PATCH_MARKER = "Patch Diff:\n"
PATCH_SECTION_END = "\n\n====="


def patch_section(prompt: str) -> str | None:
    """Return the prompt's Patch Diff body, or None if it has no such section."""
    prefix, separator, suffix = prompt.partition(PATCH_MARKER)
    if not separator:
        return None
    end = suffix.find(PATCH_SECTION_END)
    if end < 0:
        return None
    return suffix[:end]


def replace_patch_section(prompt: str, patch: str) -> str | None:
    """Rewrite the Patch Diff section with ``patch``, or return None.

    Some source images ship a short stub prompt with no ``Patch Diff:`` section
    at all (for example cybergen_v8_cr_0d66b1b8ce70).  Callers keep the original
    prompt in that case instead of aborting the batch.

    The section has to describe the *evolved* source, so the argument is always
    /testbed/patch.diff rather than whatever the incoming prompt carried: an
    older materialization left the pre-evolve fix in there, which leaves the
    agent reading a patch that no longer matches the tree it ships with.
    """
    prefix, separator, suffix = prompt.partition(PATCH_MARKER)
    if not separator:
        return None
    end = suffix.find(PATCH_SECTION_END)
    if end < 0:
        return None
    if not patch.endswith("\n"):
        patch += "\n"
    return prefix + PATCH_MARKER + patch + suffix[end:]


def strip_evolution_overlay(prompt: str) -> str:
    """Drop a trailing phase-two overlay section, if the prompt carries one."""
    index = prompt.find(EVOLUTION_OVERLAY_MARKER)
    if index < 0:
        return prompt
    return prompt[:index].rstrip("\n") + "\n"


def blank_bug_description(prompt: str) -> str:
    """Empty the Bug Description body.

    The prompt's own copy describes the *original* upstream bug, and the evolve
    moved the vulnerable code, so shipping it points the agent at a bug that is
    no longer there.  task.json gets the same treatment; this is the prompt's.
    """
    marker = f"{BUG_DESCRIPTION_MARKER}\n"
    prefix, separator, suffix = prompt.partition(marker)
    if not separator:
        return prompt
    lines = suffix.split("\n")
    index = 0
    while index < len(lines) and lines[index].strip() and not lines[index].startswith("="):
        index += 1
    return prefix + marker + "\n".join(lines[index:])


def normalize_prompt(prompt: str) -> str:
    return blank_bug_description(strip_evolution_overlay(prompt))


def render_readme(problem_id: str) -> str:
    """Render the shipped /testbed/README.md.

    Byte-identical to what the official test-set images ship (for example
    exploitbench_v8_cve_2024_0517_0002:v3-official-16cap), apart from the bug id
    on the title line.  Keep templates/task-image-README.md in sync with that
    image rather than editing the text here: the scoring prose in it is what an
    agent reads to decide when to stop.
    """
    template = README_TEMPLATE.read_text(encoding="utf-8")
    return template.replace("{{BUG_ID}}", problem_id)
