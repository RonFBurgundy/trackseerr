"""Tests for release build information and release version guard infrastructure.

Validates that Dockerfile ships CHANGELOG.md and declares TRACKSEERR_COMMIT,
that .dockerignore does not exclude CHANGELOG.md, and that docker-publish.yml
configures build-args and gates release tags against pyproject.toml and CHANGELOG.md.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_dockerfile_copies_changelog_and_declares_commit() -> None:
    """Asserts the Dockerfile copies CHANGELOG.md and declares TRACKSEERR_COMMIT."""
    dockerfile_text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    # Assert CHANGELOG.md is copied alongside README.md in runtime stage
    assert re.search(
        r"COPY\s+pyproject\.toml\s+README\.md\s+CHANGELOG\.md\s+\./",
        dockerfile_text,
    ), "Dockerfile must copy CHANGELOG.md alongside README.md into /app"

    # Assert ARG TRACKSEERR_COMMIT and ENV TRACKSEERR_COMMIT are declared
    assert 'ARG TRACKSEERR_COMMIT=""' in dockerfile_text, "Dockerfile must declare ARG TRACKSEERR_COMMIT"
    assert "ENV TRACKSEERR_COMMIT=$TRACKSEERR_COMMIT" in dockerfile_text, (
        "Dockerfile must declare ENV TRACKSEERR_COMMIT=$TRACKSEERR_COMMIT"
    )

    # Assert layer order is cache-friendly: ARG/ENV placed after pip install
    pip_install_pos = dockerfile_text.find("RUN pip install")
    arg_pos = dockerfile_text.find('ARG TRACKSEERR_COMMIT=""')
    env_pos = dockerfile_text.find("ENV TRACKSEERR_COMMIT=$TRACKSEERR_COMMIT")

    assert pip_install_pos != -1, "Dockerfile must contain pip install step"
    assert arg_pos > pip_install_pos, "ARG TRACKSEERR_COMMIT must be declared after pip install for cache friendliness"
    assert env_pos > arg_pos, "ENV TRACKSEERR_COMMIT must be declared after ARG TRACKSEERR_COMMIT"


def test_dockerignore_does_not_exclude_changelog() -> None:
    """Asserts .dockerignore does not exclude CHANGELOG.md."""
    dockerignore_path = REPO_ROOT / ".dockerignore"
    if dockerignore_path.exists():
        lines = dockerignore_path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            assert not stripped.startswith("CHANGELOG"), f".dockerignore excludes changelog: {stripped}"
            assert not stripped.startswith("*.md"), f".dockerignore excludes all markdown files: {stripped}"


def test_docker_publish_workflow_declares_build_args_and_release_guard() -> None:
    """Asserts docker-publish.yml workflow passes TRACKSEERR_COMMIT build-arg and defines release guard."""
    workflow_path = REPO_ROOT / ".github" / "workflows" / "docker-publish.yml"
    assert workflow_path.exists(), "docker-publish.yml must exist"

    raw_text = workflow_path.read_text(encoding="utf-8")
    parsed: dict[str, Any] = yaml.safe_load(raw_text)

    publish_job = parsed.get("jobs", {}).get("publish", {})
    steps: list[dict[str, Any]] = publish_job.get("steps", [])
    assert steps, "publish job must have steps"

    # Find the Docker build step
    build_idx = -1
    build_step: dict[str, Any] = {}
    for idx, step in enumerate(steps):
        uses = step.get("uses", "")
        if "docker/build-push-action" in uses:
            build_idx = idx
            build_step = step
            break

    assert build_idx != -1, "docker/build-push-action step not found in publish job"

    # Assert build-args passes TRACKSEERR_COMMIT
    with_args = build_step.get("with", {})
    build_args = with_args.get("build-args", "")
    assert "TRACKSEERR_COMMIT=${{ github.sha }}" in build_args, (
        f"build-args must contain TRACKSEERR_COMMIT=${{{{ github.sha }}}}, got {build_args!r}"
    )

    # Find the release guard step before the build
    guard_steps = [
        s for s in steps[:build_idx]
        if "startsWith(github.ref, 'refs/tags/v')" in str(s.get("if", ""))
    ]
    assert len(guard_steps) == 1, "Expected exactly 1 release guard step before docker build"
    guard_step = guard_steps[0]

    guard_run = guard_step.get("run", "")
    assert "pyproject.toml" in guard_run, "Release guard must inspect pyproject.toml"
    assert "CHANGELOG.md" in guard_run, "Release guard must inspect CHANGELOG.md"


def test_release_guard_logic_execution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Validates the release guard logic for matching, mismatched, and missing changelog versions."""
    pyproject_file = tmp_path / "pyproject.toml"
    changelog_file = tmp_path / "CHANGELOG.md"

    pyproject_file.write_text(
        '[project]\nname = "trackseerr"\nversion = "2.1.0"\n',
        encoding="utf-8",
    )
    changelog_file.write_text(
        "# Changelog\n\n## [2.1.0] - 2026-10-08\n- Feature added\n",
        encoding="utf-8",
    )

    # Extract the python script from the workflow to execute the exact logic
    workflow_path = REPO_ROOT / ".github" / "workflows" / "docker-publish.yml"
    parsed = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    guard_step = next(
        s for s in parsed["jobs"]["publish"]["steps"]
        if "startsWith(github.ref, 'refs/tags/v')" in str(s.get("if", ""))
    )
    run_script = guard_step["run"]

    # 1. Matching tag, pyproject.toml, and changelog -> Success
    res = subprocess.run(
        [sys.executable, "-c", run_script.split("python3 -c '", 1)[1].rsplit("'", 1)[0]],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_REF": "refs/tags/v2.1.0"},
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Expected guard to pass, stderr: {res.stderr}"
    assert "Release guard passed" in res.stdout

    # 2. Mismatched tag -> Failure
    res_mismatch = subprocess.run(
        [sys.executable, "-c", run_script.split("python3 -c '", 1)[1].rsplit("'", 1)[0]],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_REF": "refs/tags/v2.0.0"},
        capture_output=True,
        text=True,
    )
    assert res_mismatch.returncode != 0, "Guard should fail when tag doesn't match pyproject.toml"

    # 3. Missing changelog heading -> Failure
    changelog_file.write_text(
        "# Changelog\n\n## [Unreleased]\n- Some feature\n",
        encoding="utf-8",
    )
    res_no_heading = subprocess.run(
        [sys.executable, "-c", run_script.split("python3 -c '", 1)[1].rsplit("'", 1)[0]],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_REF": "refs/tags/v2.1.0"},
        capture_output=True,
        text=True,
    )
    assert res_no_heading.returncode != 0, "Guard should fail when CHANGELOG.md lacks heading"
