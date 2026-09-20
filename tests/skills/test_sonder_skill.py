"""The sonder skill ships its own regression suite (scripts/selftest.py, stdlib only,
runs in a temp dir, never touches real dossiers). Run it under pytest so CI sees it."""
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[2] / "skills" / "relationships" / "sonder"


def test_skill_layout():
    assert (SKILL / "SKILL.md").exists()
    assert (SKILL / "references" / "method.md").exists()
    assert (SKILL / "references" / "target-format.md").exists()
    for script in ("record.py", "check.py", "selftest.py"):
        assert (SKILL / "scripts" / script).exists()


def test_selftest_passes(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SKILL / "scripts" / "selftest.py")],
        capture_output=True, text=True, cwd=tmp_path,
        env={"PATH": "", "HOME": str(tmp_path), "ROMANCE_TARGETS": str(tmp_path / "targets")},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
