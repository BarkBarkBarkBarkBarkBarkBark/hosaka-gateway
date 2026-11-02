import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

spec = importlib.util.spec_from_file_location("gateway_install", Path(__file__).parents[1] / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_launcher_handles_spaces_and_forwards_arguments(tmp_path):
    prefix = tmp_path / "prefix with spaces"
    core = tmp_path / "core with spaces"
    core.mkdir()
    installer.launcher(prefix, "example", Path(sys.executable), "json.tool", core)
    result = subprocess.run([prefix / "bin/example", "--help"], cwd="/", capture_output=True, text=True)
    assert result.returncode == 0
    assert "--sort-keys" in result.stdout


def test_launcher_preserves_unrelated_files_and_symlinks(tmp_path):
    directory = tmp_path / "bin"
    directory.mkdir()
    target = directory / "example"
    target.write_text("not a Hosaka command")
    with pytest.raises(ValueError, match="unrelated"):
        installer.launcher(tmp_path, "example", Path(sys.executable), "json.tool", tmp_path)
    assert target.read_text() == "not a Hosaka command"
    target.unlink()
    target.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="unrelated"):
        installer.launcher(tmp_path, "example", Path(sys.executable), "json.tool", tmp_path)
