"""Installed build identity must reach every runtime version consumer."""

from importlib import metadata
from pathlib import Path
import runpy
import os
import subprocess

import pytest

import pullarr


INIT = Path(pullarr.__file__)


@pytest.mark.parametrize("installed", ["0.2.1", "0.3.0rc1", "0.2.0+g" + "a" * 40, "0.2.0+local"])
def test_runtime_and_user_agent_use_installed_metadata(monkeypatch, installed):
    def version(name):
        assert name == "pullarr"
        return installed

    monkeypatch.setattr(metadata, "version", version)
    # Container runtime variables cannot impersonate a different installed build.
    monkeypatch.setenv("PULLARR_BUILD_VERSION", "9.9.9")
    namespace = runpy.run_path(str(INIT))
    assert namespace["__version__"] == installed
    assert namespace["USER_AGENT"] == f"Pullarr/{installed} (comic library automation)"


def test_source_only_runtime_is_explicitly_local(monkeypatch):
    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing)
    namespace = runpy.run_path(str(INIT))
    source_version = Path(__file__).resolve().parents[1] / "VERSION"
    assert namespace["__version__"] == source_version.read_text().strip()
    assert namespace["__version__"].endswith("+local")


def test_api_and_app_share_installed_version():
    from pullarr.api import system
    from pullarr import main

    assert pullarr.__version__ == metadata.version("pullarr")
    assert main.app.version == main.__version__ == system.__version__ == pullarr.__version__
    assert pullarr.USER_AGENT.startswith(f"Pullarr/{pullarr.__version__} ")


@pytest.mark.parametrize("ref,tag", [
    ("refs/heads/main", None), ("refs/heads/v0.2.1", None),
    ("refs/tags/v0.2.1", "0.2.1"), ("refs/tags/0.3.0rc1", "0.3.0rc1"),
])
def test_image_workflow_resolves_supported_forgejo_refs(ref, tag):
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".forgejo/workflows/build-image.yml").read_text()
    start = workflow.index('BUILD_VERSION=')
    end = workflow.index('esac', start) + len('esac')
    script = workflow[start:end] + '\nprintf "%s" "$BUILD_VERSION"'
    sha = "a" * 40
    result = subprocess.run(
        ["sh", "-eu", "-c", script], cwd=root,
        env={"PATH": os.defpath, "GITHUB_REF": ref, "GITHUB_SHA": sha},
        capture_output=True, text=True, check=True,
    )
    base = (root / "backend/VERSION").read_text().strip().removesuffix("+local")
    assert result.stdout == (tag or f"{base}+g{sha}")
