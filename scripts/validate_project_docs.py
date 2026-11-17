#!/usr/bin/env python3
"""Validate project documentation structure, budgets, links, and source references."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote


MANIFEST = Path("scripts/doc-budgets.manifest.json")
SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".py"}
EXCLUDED_PARTS = {"vendor", "generated", "fixtures", "snapshots", ".git"}
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
DOC_REFERENCE = re.compile(r"(?P<path>docs/[A-Za-z0-9_./-]+\.md)(?:#(?P<fragment>[A-Za-z0-9_%-]+))?")


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _excluded(path: Path, root: Path) -> bool:
    parts = path.relative_to(root).parts
    if any(part in EXCLUDED_PARTS for part in parts):
        return True
    return len(parts) >= 2 and parts[:2] == ("docs", "agent-notes")


def _slug(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text.strip().casefold())
    text = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)
    return re.sub(r"[ _]+", "-", text).strip("-")


def _headings(path: Path) -> set[str]:
    headings: set[str] = set()
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        explicit = re.search(r'<a\s+id=["\']([^"\']+)["\']', line)
        if explicit:
            headings.add(explicit.group(1))
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not match:
            continue
        base = _slug(match.group(1))
        count = counts.get(base, 0)
        counts[base] = count + 1
        headings.add(base if count == 0 else f"{base}-{count}")
    return headings


def _check_paragraph_lines(path: Path, root: Path) -> list[str]:
    errors: list[str] = []
    previous_prose = False
    in_fence = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            previous_prose = False
            continue
        structural = (
            not stripped
            or in_fence
            or stripped.startswith(("#", "- ", "* ", "+ ", "> ", "|"))
            or re.match(r"^\d+[.)]\s", stripped) is not None
            or re.match(r"^[-:| ]{3,}$", stripped) is not None
        )
        prose = not structural
        if prose and previous_prose:
            errors.append(
                f"{_relative(path, root)}:{number - 1}: paragraph must use one physical line"
            )
        previous_prose = prose
    return errors


def _check_fragment(target: Path, fragment: str, label: str) -> str | None:
    decoded = unquote(fragment).casefold()
    if decoded not in {heading.casefold() for heading in _headings(target)}:
        return f"{label}: heading fragment does not exist: #{fragment}"
    return None


def _check_markdown_links(path: Path, root: Path) -> list[str]:
    errors: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for match in MARKDOWN_LINK.finditer(line):
            raw = match.group(1).strip().split(maxsplit=1)[0].strip("<>")
            if not raw or raw.startswith(("http://", "https://", "mailto:")):
                continue
            target_text, separator, fragment = raw.partition("#")
            target = path if not target_text else (path.parent / unquote(target_text)).resolve()
            label = f"{_relative(path, root)}:{number}"
            try:
                target.relative_to(root.resolve())
            except ValueError:
                errors.append(f"{label}: relative Markdown link escapes the repository")
                continue
            if not target.is_file():
                errors.append(f"{label}: Markdown link target does not exist: {target_text}")
                continue
            if separator and target.suffix.casefold() == ".md":
                if error := _check_fragment(target, fragment, label):
                    errors.append(error)
    return errors


def _source_paths(root: Path):
    if (root / ".git").exists():
        result = subprocess.run(
            ("git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"),
            cwd=root,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return (root / item.decode("utf-8") for item in result.stdout.split(b"\0") if item)
    return root.rglob("*")


def _check_source_references(root: Path) -> list[str]:
    errors: list[str] = []
    for path in _source_paths(root):
        try:
            is_source = path.is_file() and path.suffix.casefold() in SOURCE_SUFFIXES
        except OSError:
            continue
        if not is_source or _excluded(path, root):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for match in DOC_REFERENCE.finditer(line):
                target = root / match.group("path")
                label = f"{_relative(path, root)}:{number}"
                if not target.is_file():
                    errors.append(f"{label}: documentation reference does not exist: {match.group('path')}")
                elif match.group("fragment"):
                    if error := _check_fragment(target, match.group("fragment"), label):
                        errors.append(error)
    return errors


def validate(root: Path) -> tuple[list[str], int]:
    errors: list[str] = []
    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        return [f"{MANIFEST.as_posix()}: budget manifest does not exist"], 0
    try:
        budgets = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        return [f"{MANIFEST.as_posix()}: invalid budget manifest: {error}"], 0
    if not isinstance(budgets, dict):
        return [f"{MANIFEST.as_posix()}: expected an object mapping paths to budgets"], 0

    for relative_path, budget in budgets.items():
        path = root / relative_path
        if not path.is_file():
            errors.append(f"{relative_path}: budgeted file does not exist")
            continue
        if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
            errors.append(f"{relative_path}: budget must be a positive integer")
            continue
        words = len(re.findall(r"\S+", path.read_text(encoding="utf-8")))
        if words > budget:
            errors.append(f"{relative_path}: word budget exceeded ({words} > {budget})")

    project_markdown_files = {
        path for path in (root / "docs").rglob("*.md")
        if path.is_file() and not _excluded(path, root)
    } if (root / "docs").is_dir() else set()
    readme = root / "README.md"
    if readme.is_file():
        project_markdown_files.add(readme)
    for path in sorted(project_markdown_files):
        relative_path = _relative(path, root)
        if relative_path not in budgets:
            errors.append(f"{relative_path}: project document has no word budget")

    markdown_files = set(project_markdown_files)
    markdown_files.update(
        path
        for relative_path in budgets
        if (path := root / relative_path).suffix.casefold() == ".md"
        and path.is_file()
        and not _excluded(path, root)
    )
    for path in sorted(markdown_files):
        errors.extend(_check_paragraph_lines(path, root))
        errors.extend(_check_markdown_links(path, root))
    errors.extend(_check_source_references(root))
    return errors, len(markdown_files)


def main() -> int:
    errors, checked = validate(Path.cwd().resolve())
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"{checked} documentation file(s) checked; all conform")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
