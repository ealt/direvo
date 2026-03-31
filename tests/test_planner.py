import os
import stat
import textwrap
from pathlib import Path

import pytest

from direvo.planner import NullPlannerSession, PlannerError, SubprocessPlannerSession


def test_null_planner_session_is_noop() -> None:
    session = NullPlannerSession()
    session.start()
    session.notify_trial_completed(1)
    session.stop()


def test_subprocess_planner_session_receives_notifications(tmp_path: Path) -> None:
    output_path = tmp_path / "planner.log"
    script_path = tmp_path / "planner.py"
    script_path.write_text(
        textwrap.dedent(
            """
            import sys
            from pathlib import Path

            output = Path(sys.argv[1])
            for line in sys.stdin:
                with output.open("a", encoding="utf-8") as handle:
                    handle.write(line)
            """
        ),
        encoding="utf-8",
    )
    os.chmod(script_path, stat.S_IRWXU)

    session = SubprocessPlannerSession(
        command=f"python3 {script_path} {output_path}",
        planner_root=tmp_path,
        notify_template="Trial completed. ID: {trial_id}",
        startup_timeout_sec=1,
        user=None,
    )

    session.start()
    session.notify_trial_completed(3)
    session.notify_trial_completed(5)
    session.notify_error("Invalid proposal 8: invalid slug")
    session.stop()

    assert output_path.read_text(encoding="utf-8").splitlines() == [
        "Trial completed. ID: 3",
        "Trial completed. ID: 5",
        "Invalid proposal 8: invalid slug",
    ]


def test_subprocess_planner_session_fails_when_process_exits_immediately(tmp_path: Path) -> None:
    session = SubprocessPlannerSession(
        command="python3 -c 'import sys; sys.exit(1)'",
        planner_root=tmp_path,
        notify_template="Trial completed. ID: {trial_id}",
        startup_timeout_sec=1,
        user=None,
    )

    with pytest.raises(PlannerError, match="exited during startup"):
        session.start()


def test_subprocess_planner_session_runs_as_planner_user_when_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("pwd.getpwnam", lambda user: object())
    session = SubprocessPlannerSession(
        command="python3 planner.py --watch",
        planner_root=tmp_path,
        notify_template="Trial completed. ID: {trial_id}",
        startup_timeout_sec=1,
    )

    command = session._planner_command()

    assert command[:4] == ["su", "planner", "-s", "/bin/sh"]
    assert "cd" in command[-1]
    assert "python3 planner.py --watch" in command[-1]


def test_subprocess_planner_session_runs_directly_when_not_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    session = SubprocessPlannerSession(
        command="python3 planner.py --watch",
        planner_root=tmp_path,
        notify_template="Trial completed. ID: {trial_id}",
        startup_timeout_sec=1,
    )

    assert session._planner_command() == ["python3", "planner.py", "--watch"]


def test_subprocess_planner_session_runs_directly_when_planner_user_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("pwd.getpwnam", lambda user: (_ for _ in ()).throw(KeyError(user)))
    session = SubprocessPlannerSession(
        command="python3 planner.py --watch",
        planner_root=tmp_path,
        notify_template="Trial completed. ID: {trial_id}",
        startup_timeout_sec=1,
    )

    assert session._planner_command() == ["python3", "planner.py", "--watch"]
