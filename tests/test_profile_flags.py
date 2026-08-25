from __future__ import annotations

import json
from pathlib import Path

import pytest

from hard_restart_claude_code import cli
from hard_restart_claude_code.cli import (
    EXIT_BAD_PROFILE_DIR,
    EXIT_CONTRADICTORY_FLAGS,
    EXIT_OK,
    main,
    print_result,
    result_as_dict,
)
from hard_restart_claude_code.restart import (
    ClaudeProcess,
    Effects,
    PROFILE_SOURCE_EXPLICIT,
    PROFILE_SOURCE_INFERRED,
    PROFILE_SOURCE_NONE,
    ProfileChoice,
    ProfileDirError,
    Result,
    hard_restart,
    validate_profile_dir,
)

EXPLICIT_DIR = r"D:\profiles\primary"
OBSERVED_DIR = r"D:\profiles\reserve"


def test_validate_accepts_an_absolute_directory(tmp_path):
    assert validate_profile_dir(str(tmp_path)) == str(tmp_path.resolve())


def test_validate_accepts_a_directory_that_does_not_exist_yet(tmp_path):
    missing = tmp_path / "not-created-yet"
    assert validate_profile_dir(str(missing)) == str(missing.resolve())


def test_validate_rejects_empty():
    with pytest.raises(ProfileDirError):
        validate_profile_dir("   ")


def test_validate_rejects_a_unc_path():
    # Electron would happily put live session credentials on a remote share.
    with pytest.raises(ProfileDirError):
        validate_profile_dir(r"\\attacker\share\profile")


def test_validate_rejects_a_forward_slash_unc_path():
    # resolve() turns this into the backslash UNC form, so checking only the
    # raw input let a remote path through to the launch.
    with pytest.raises(ProfileDirError):
        validate_profile_dir("//server/share/profile")


def test_validate_rejects_a_device_path():
    with pytest.raises(ProfileDirError):
        validate_profile_dir(r"\\?\C:\profile")


def test_validate_rejects_a_relative_path():
    with pytest.raises(ProfileDirError):
        validate_profile_dir("profiles" + chr(92) + "primary")


def test_validate_rejects_a_drive_relative_path():
    # "D:x" resolves against the current directory OF DRIVE D, which the caller
    # cannot see, so the launched dir would depend on invisible state.
    with pytest.raises(ProfileDirError):
        validate_profile_dir("D:primary")


def test_validate_rejects_a_value_that_looks_like_a_flag():
    with pytest.raises(ProfileDirError):
        validate_profile_dir("--user-data-dir=C:" + chr(92) + "elsewhere")


def test_validate_rejects_control_characters():
    with pytest.raises(ProfileDirError):
        validate_profile_dir("C:" + chr(92) + "p" + chr(0) + "rofile")
    with pytest.raises(ProfileDirError):
        validate_profile_dir("C:" + chr(92) + "p" + chr(10) + "rofile")


def test_validate_rejects_a_file(tmp_path):
    target = tmp_path / "a-file.txt"
    target.write_text("")
    with pytest.raises(ProfileDirError):
        validate_profile_dir(str(target))


def _restart(profile: ProfileChoice, tmp_path, observed: str | None = OBSERVED_DIR):
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    launched = []
    result = hard_restart(
        exe,
        profile=profile,
        effects=Effects(
            finder=lambda: [ClaudeProcess(pid=1, profile_dir=observed)],
            killer=lambda _pids: None,
            launcher=lambda _e, dir_: launched.append(dir_),
            sleeper=lambda _s: None,
        ),
    )
    return result, launched


def test_explicit_profile_wins_over_the_running_one(tmp_path):
    result, launched = _restart(ProfileChoice(explicit=EXPLICIT_DIR), tmp_path)
    assert launched == [EXPLICIT_DIR]
    assert result.launch_profile_dir == EXPLICIT_DIR
    assert result.profile_source == PROFILE_SOURCE_EXPLICIT
    # What was running is still reported - it says what got killed.
    assert result.profile_dir == OBSERVED_DIR


def test_no_profile_launches_bare_even_when_one_was_running(tmp_path):
    result, launched = _restart(ProfileChoice(bare=True), tmp_path)
    assert launched == [None]
    assert result.launch_profile_dir is None
    assert result.profile_source == PROFILE_SOURCE_NONE


def test_omitting_the_flag_still_preserves_the_running_profile(tmp_path):
    result, launched = _restart(ProfileChoice(), tmp_path)
    assert launched == [OBSERVED_DIR]
    assert result.launch_profile_dir == OBSERVED_DIR
    assert result.profile_source == PROFILE_SOURCE_INFERRED


def test_omitting_the_flag_is_not_the_same_as_no_profile(tmp_path):
    inferred, _ = _restart(ProfileChoice(), tmp_path)
    bare, _ = _restart(ProfileChoice(bare=True), tmp_path)
    assert inferred.launch_profile_dir != bare.launch_profile_dir


def test_profile_dir_with_no_launch_is_refused(capsys):
    code = main(["--profile-dir", EXPLICIT_DIR, "--no-launch"])
    assert code == EXIT_CONTRADICTORY_FLAGS
    assert "--no-launch" in capsys.readouterr().err


def test_invalid_profile_dir_exits_without_touching_anything(capsys):
    code = main(["--profile-dir", r"\\attacker\share\profile"])
    assert code == EXIT_BAD_PROFILE_DIR
    assert "UNC" in capsys.readouterr().err


def test_profile_dir_and_no_profile_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        main(["--profile-dir", EXPLICIT_DIR, "--no-profile"])


def test_json_error_is_machine_readable(capsys):
    code = main(["--profile-dir", r"\\attacker\share\profile", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == code == EXIT_BAD_PROFILE_DIR
    assert payload["error"]


def test_a_choice_cannot_be_explicit_and_bare_at_once():
    with pytest.raises(ProfileDirError):
        ProfileChoice(explicit=EXPLICIT_DIR, bare=True)


def test_library_allows_no_launch_with_an_explicit_dir(tmp_path):
    # The CLI refuses this pairing; the library reports what WOULD have been
    # launched and launches nothing. Pinned so the meaning is deliberate.
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    launched = []
    result = hard_restart(
        exe,
        no_launch=True,
        profile=ProfileChoice(explicit=EXPLICIT_DIR),
        effects=Effects(
            finder=lambda: [ClaudeProcess(pid=1, profile_dir=OBSERVED_DIR)],
            killer=lambda _pids: None,
            launcher=lambda _e, dir_: launched.append(dir_),
            sleeper=lambda _s: None,
        ),
    )
    assert launched == []
    assert result.launched is False
    assert result.launch_profile_dir == EXPLICIT_DIR


def _canned(**overrides) -> Result:
    fields = dict(
        killed=[10, 11],
        launched=True,
        exe=Path("claude.exe"),
        profile_dir=OBSERVED_DIR,
        profile_conflict=False,
        launch_profile_dir=OBSERVED_DIR,
        profile_source=PROFILE_SOURCE_INFERRED,
    )
    fields.update(overrides)
    return Result(**fields)


# account-swap parses this exact line out of hrcc's stdout. DESIGN.md names it
# as the seam a cosmetic reword silently breaks, so pin it until --json has
# actually replaced it.
def test_matched_pids_line_is_stable_across_profile_modes(capsys):
    for result in (
        _canned(),
        _canned(launch_profile_dir=EXPLICIT_DIR, profile_source=PROFILE_SOURCE_EXPLICIT),
        _canned(launch_profile_dir=None, profile_source=PROFILE_SOURCE_NONE),
    ):
        for dry_run in (False, True):
            print_result(result, dry_run=dry_run)
            first = capsys.readouterr().out.splitlines()[0]
            assert first == "matched pids: 10, 11"


def test_dry_run_does_not_claim_anything_was_killed(capsys):
    result = _canned(
        profile_conflict=True,
        launch_profile_dir=EXPLICIT_DIR,
        profile_source=PROFILE_SOURCE_EXPLICIT,
    )
    print_result(result, dry_run=True)
    captured = capsys.readouterr()
    assert "would use" in captured.out
    assert "were all killed" not in captured.err
    assert "would all be killed" in captured.err


def test_json_success_goes_to_stdout(capsys, monkeypatch, tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    monkeypatch.setattr(cli, "hard_restart", lambda *_a, **_k: _canned(exe=exe))
    code = main(["--exe", str(exe), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert payload["killed"] == [10, 11]
    assert payload["profile_source"] == PROFILE_SOURCE_INFERRED


def test_result_as_dict_reports_both_profiles():
    result = Result(
        killed=[1],
        launched=True,
        exe=Path("claude.exe"),
        profile_dir=OBSERVED_DIR,
        profile_conflict=True,
        launch_profile_dir=EXPLICIT_DIR,
        profile_source=PROFILE_SOURCE_EXPLICIT,
    )
    payload = result_as_dict(result, dry_run=False)
    assert payload["observed_profile"] == OBSERVED_DIR
    assert payload["launch_profile_dir"] == EXPLICIT_DIR
    assert payload["profile_source"] == PROFILE_SOURCE_EXPLICIT
    assert payload["observed_profile_conflict"] is True
    assert json.loads(json.dumps(payload)) == payload
