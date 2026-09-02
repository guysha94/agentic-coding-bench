"""Hidden checks for the DevOps task.

Deliberately validated by parsing the files rather than by building images: requiring a
Docker daemon would make the task non-portable and much slower, and every requirement in
the prompt is statically checkable.
"""

import pathlib
import re
import subprocess

import pytest

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parent.parent


def read(name: str) -> str:
    path = ROOT / name
    assert path.exists(), f"{name} is missing"
    return path.read_text()


# ------------------------------------------------------------------- Dockerfile ------
def test_dockerfile_uses_a_slim_pinned_base():
    first = next(
        line for line in read("Dockerfile").splitlines() if line.strip().upper().startswith("FROM")
    )
    assert "slim" in first.lower() or "alpine" in first.lower(), (
        f"base image should be slim/alpine, got: {first.strip()}"
    )
    assert ":" in first, "base image must be pinned to a tag"


def test_dockerfile_sets_workdir():
    assert re.search(r"^\s*WORKDIR\s+\S+", read("Dockerfile"), re.MULTILINE)


def test_dockerfile_installs_dependencies_before_copying_source():
    lines = [line.strip() for line in read("Dockerfile").splitlines() if line.strip()]
    req_copy = next(
        (i for i, line in enumerate(lines)
         if line.upper().startswith("COPY") and "requirements" in line.lower()), None
    )
    install = next(
        (i for i, line in enumerate(lines)
         if line.upper().startswith("RUN") and "pip install" in line.lower()), None
    )
    src_copy = next(
        (i for i, line in enumerate(lines)
         if line.upper().startswith("COPY") and "requirements" not in line.lower()), None
    )
    assert req_copy is not None and install is not None and src_copy is not None, (
        "expected a requirements COPY, a pip install RUN, and a source COPY"
    )
    assert req_copy < install < src_copy, (
        "dependencies must be copied and installed before the application source, "
        "otherwise every source change busts the pip layer cache"
    )


def test_dockerfile_runs_as_non_root():
    content = read("Dockerfile")
    match = re.search(r"^\s*USER\s+(\S+)", content, re.MULTILINE)
    assert match, "no USER instruction: the container would run as root"
    assert match.group(1).lower() != "root"


def test_dockerfile_has_healthcheck_on_health_endpoint():
    content = read("Dockerfile")
    assert re.search(r"^\s*HEALTHCHECK", content, re.MULTILINE), "no HEALTHCHECK instruction"
    assert "/health" in content


def test_dockerfile_cmd_is_exec_form():
    match = re.search(r"^\s*CMD\s+(.+)$", read("Dockerfile"), re.MULTILINE)
    assert match, "no CMD instruction"
    assert match.group(1).strip().startswith("["), "CMD must use exec form (a JSON array)"


# ---------------------------------------------------------------- docker-compose -----
def test_compose_parses_and_maps_a_host_port():
    compose = yaml.safe_load(read("docker-compose.yml"))
    api = compose["services"]["api"]
    ports = [str(p) for p in api.get("ports", [])]
    assert ports, "api service exposes no ports"
    assert any(":" in p for p in ports), (
        f"ports must map host:container, got {ports}"
    )


def test_compose_has_no_worker_service():
    compose = yaml.safe_load(read("docker-compose.yml"))
    assert "worker" not in compose["services"], (
        "the worker service references a script that does not exist and should be removed"
    )


def test_compose_depends_on_is_valid_if_present():
    compose = yaml.safe_load(read("docker-compose.yml"))
    for name, service in compose["services"].items():
        dep = service.get("depends_on")
        if dep is not None:
            assert isinstance(dep, (list, dict)), (
                f"{name}.depends_on must be a list or mapping, got {type(dep).__name__}"
            )


# ------------------------------------------------------------------------ CI ---------
def test_ci_workflow_is_valid_and_modernised():
    workflow = yaml.safe_load(read(".github/workflows/ci.yml"))
    job = next(iter(workflow["jobs"].values()))
    steps = job["steps"]
    uses = [s.get("uses", "") for s in steps]

    assert not any(u.endswith("@v2") for u in uses), f"outdated action versions: {uses}"

    setup = next((s for s in steps if s.get("uses", "").startswith("actions/setup-python")), None)
    assert setup, "no setup-python step"
    version = str(setup.get("with", {}).get("python-version", ""))
    assert version.startswith("3.1") and version not in ("3.1",), (
        f"python-version {version!r} is wrong; 3.1 is not a modern Python"
    )


def test_ci_runs_the_real_test_path():
    content = read(".github/workflows/ci.yml")
    assert "pytest test/" not in content, "the `test/` directory does not exist here"
    assert "pytest" in content


# ----------------------------------------------------------------- health check ------
def test_healthcheck_script_is_syntactically_valid():
    result = subprocess.run(
        ["bash", "-n", str(ROOT / "scripts/healthcheck.sh")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_healthcheck_script_is_defensive_and_bounded():
    content = read("scripts/healthcheck.sh")
    assert "set -euo pipefail" in content, "missing `set -euo pipefail`"
    assert not re.search(r"^\s*while\s+true", content, re.MULTILINE), (
        "the script still loops forever; retries must be bounded"
    )
    assert re.search(r"\$\{PORT:-8080\}|PORT=\$\{PORT:-8080\}", content), (
        "PORT must default to 8080 when unset"
    )
    assert re.search(r"\bexit\s+1\b", content), "must exit non-zero on failure"


def test_healthcheck_fails_fast_when_nothing_is_listening():
    """With no server running, the script must terminate non-zero rather than hang."""
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/healthcheck.sh")],
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "PORT": "59999"},
    )
    assert result.returncode != 0, "script reported success with no server listening"
