"""DockerSandbox, tested with a fake `docker` command (no real Docker needed)."""
import asyncio
import subprocess
from types import SimpleNamespace

from aisew.sandbox.docker import IMAGE, DockerSandbox, unsupported_packages

FILES = [("app/models.py", "x = 1\n"), ("tests/test_a.py", "def test_a():\n    assert True\n"),
         ("requirements.txt", "sqlalchemy\npytest\n")]


class FakeDocker:
    """Records every docker call. `script` maps the docker sub-command to (returncode, stdout)."""

    def __init__(self, script=None, timeout_on=None):
        self.calls: list[list[str]] = []
        self.script = {"info": (0, ""), "image": (0, ""), "run": (0, "1 passed in 0.1s"),
                       "rm": (0, ""), "build": (0, "")}
        self.script.update(script or {})
        self.timeout_on = timeout_on

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        sub = cmd[1]
        if sub == self.timeout_on:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))
        code, out = self.script[sub]
        return SimpleNamespace(returncode=code, stdout=out, stderr="")


def sandbox(fake, docker_on_path=True, **kw):
    return DockerSandbox(timeout=7, run=fake, which=lambda name: "docker" if docker_on_path else None, **kw)


def run_cmd(fake):
    return next(c for c in fake.calls if c[1] == "run")


def go(sb, files=FILES):
    return asyncio.run(sb.run_tests(files))


def test_a_passing_run():
    fake = FakeDocker()
    result = go(sandbox(fake))
    assert result.ran and result.passed and "1 passed" in result.output


def test_a_failing_run_returns_the_output():
    fake = FakeDocker({"run": (1, "E   SyntaxError: invalid syntax\n1 failed")})
    result = go(sandbox(fake))
    assert result.ran and result.passed is False and "SyntaxError" in result.output


def test_no_tests_collected_counts_as_a_failure():
    result = go(sandbox(FakeDocker({"run": (5, "no tests ran")})))
    assert result.ran and result.passed is False


def test_the_container_is_locked_down():
    fake = FakeDocker()
    go(sandbox(fake))
    cmd = run_cmd(fake)
    joined = " ".join(cmd)
    assert cmd[cmd.index("--network") + 1] == "none"
    assert cmd[cmd.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in cmd and "--read-only" in cmd and "--rm" in cmd
    assert cmd[cmd.index("--memory") + 1] == "768m" and cmd[cmd.index("--pids-limit") + 1] == "128"
    assert cmd[cmd.index("--cpus") + 1] == "1"
    assert IMAGE in cmd and cmd[-1] == "no:cacheprovider"
    assert "--privileged" not in joined and "--network host" not in joined and "docker.sock" not in joined
    mount = cmd[cmd.index("--mount") + 1]
    assert mount.startswith("type=bind,source=") and mount.endswith("target=/work")
    assert "aisew" not in mount.split("source=")[1].split(",")[0].replace("aisew-sandbox-", "")  # a temp folder only


def test_only_a_temporary_folder_is_mounted_and_it_is_deleted_afterwards(tmp_path):
    seen = {}

    def run(cmd, **kw):
        if cmd[1] == "run":
            folder = cmd[cmd.index("--mount") + 1].split("source=")[1].split(",")[0]
            seen["files"] = sorted(p.name for p in __import__("pathlib").Path(folder).rglob("*") if p.is_file())
            seen["folder"] = folder
        return FakeDocker()(cmd, **kw)

    go(sandbox(run))
    assert seen["files"] == ["models.py", "requirements.txt", "test_a.py"]
    assert not __import__("os").path.exists(seen["folder"])


def test_unsafe_paths_never_reach_the_sandbox_folder():
    seen = {}

    def run(cmd, **kw):
        if cmd[1] == "run":
            import pathlib
            folder = pathlib.Path(cmd[cmd.index("--mount") + 1].split("source=")[1].split(",")[0])
            seen["inside"] = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())
            seen["escaped"] = (folder.parent / "evil.py").exists()
        return FakeDocker()(cmd, **kw)

    go(sandbox(run), FILES + [("../evil.py", "x = 1\n"), ("/etc/evil.py", "x = 1\n"), ("run.exe", "binary")])
    assert not seen["escaped"] and "evil.py" not in " ".join(seen["inside"]) and "run.exe" not in seen["inside"]


def test_a_timeout_removes_the_container():
    fake = FakeDocker(timeout_on="run")
    result = go(sandbox(fake))
    assert result.ran and result.passed is False and "did not finish within 7 seconds" in result.output
    rm = next(c for c in fake.calls if c[1] == "rm")
    assert rm[:3] == ["docker", "rm", "-f"] and rm[3].startswith("aisew-")
    assert rm[3] == run_cmd(fake)[run_cmd(fake).index("--name") + 1]


def test_docker_missing_means_not_run():
    result = go(sandbox(FakeDocker(), docker_on_path=False))
    assert result.ran is False and "not installed or not running" in result.reason


def test_docker_installed_but_not_started_means_not_run():
    result = go(sandbox(FakeDocker({"info": (1, "")})))
    assert result.ran is False and "not installed or not running" in result.reason


def test_a_missing_image_is_built_once_and_then_used():
    fake = FakeDocker({"image": (1, "")})
    sb = sandbox(fake)
    # the build succeeds, so afterwards the image exists
    original = fake.script["build"]
    fake.script["build"] = (0, "")
    result = go(sb)
    build = next(c for c in fake.calls if c[1] == "build")
    assert build[2:4] == ["-t", IMAGE] and result.ran and original == (0, "")


def test_a_failed_image_build_means_not_run_with_the_reason():
    fake = FakeDocker({"image": (1, ""), "build": (1, "pull access denied")})
    result = go(sandbox(fake))
    assert result.ran is False and "Could not build the sandbox image" in result.reason
    assert not any(c[1] == "run" for c in fake.calls)


def test_a_missing_package_is_explained_in_the_output():
    files = [("tests/test_a.py", "def test_a():\n    pass\n"), ("requirements.txt", "sqlalchemy\nrequests==2.0\nNumPy\n")]
    result = go(sandbox(FakeDocker({"run": (1, "ModuleNotFoundError: requests")})), files)
    assert "requests" in result.output and "numpy" in result.output and "not available in the sandbox" in result.output


def test_unsupported_packages_parsing():
    assert unsupported_packages("sqlalchemy>=2\npytest\n# c\n\nFastAPI[all]\npytest_asyncio\n") == []
    assert unsupported_packages("requests\nPillow==10\nuvicorn") == ["requests", "pillow", "uvicorn"]


def test_status_and_build_report_state():
    fake = FakeDocker({"image": (1, "")})
    sb = sandbox(fake)
    status = asyncio.run(sb.status())
    assert status == {"mode": "docker", "docker_available": True, "image": IMAGE,
                      "image_ready": False, "building": False, "last_error": ""}
    fake.script["image"] = (0, "")
    after = asyncio.run(sb.build())
    assert after["image_ready"] is True and after["building"] is False


def test_status_without_docker():
    status = asyncio.run(sandbox(FakeDocker(), docker_on_path=False).status())
    assert status["docker_available"] is False and status["image_ready"] is False
