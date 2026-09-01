"""Shell-level checks for the database role password transport."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def test_role_passwords_travel_on_stdin_not_argv_or_disk(tmp_path):
    root = Path(__file__).parents[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "psql-calls.jsonl"
    fake_psql = fake_bin / "psql"
    fake_psql.write_text(
        """#!/usr/bin/env python3
import json
import os
import stat
import sys
from pathlib import Path

command_file = next(
    (arg.split('=', 1)[1] for arg in sys.argv[1:] if arg.startswith('--file=')),
    None,
)
if command_file is None:
    print('1')
else:
    path = Path(command_file)
    record = {
        'argv': sys.argv[1:],
        'stdin': sys.stdin.read().rstrip('\\n'),
        'command': path.read_text(),
        'command_file': command_file,
        'mode': stat.S_IMODE(path.stat().st_mode),
    }
    with Path(os.environ['PSQL_CALL_LOG']).open('a') as stream:
        stream.write(json.dumps(record) + '\\n')
"""
    )
    fake_psql.chmod(0o700)

    variables = (
        "SONDER_APP_DB_PASSWORD",
        "SONDER_IDENTITY_DB_PASSWORD",
        "SONDER_ERASURE_DB_PASSWORD",
        "SONDER_WORKER_DB_PASSWORD",
        "SONDER_MAINT_DB_PASSWORD",
        "SONDER_BOT_DB_PASSWORD",
    )
    passwords = {
        name: f"quote' and !@#$%^&*()[]{{}}:;,.?/{index}"
        for index, name in enumerate(variables)
    }
    env = {
        **os.environ,
        **passwords,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "PSQL_CALL_LOG": str(log),
        "TMPDIR": str(tmp_path),
    }
    subprocess.run(
        [str(root / "scripts" / "db-roles.sh")],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == len(passwords)
    assert {call["stdin"] for call in calls} == set(passwords.values())
    for call in calls:
        rendered_argv = " ".join(call["argv"])
        assert all(password not in rendered_argv for password in passwords.values())
        assert all(password not in call["command"] for password in passwords.values())
        assert call["command"].startswith("\\prompt pw\nALTER ROLE sonder_")
        assert call["mode"] == 0o600
        assert not Path(call["command_file"]).exists()
