"""Exit codes and exceptions shared by every command."""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_WAITING_FOR_CLAUDE = 3  # an input file was written; Claude must write the matching output file
EXIT_NEEDS_USER = 4  # paused for the user: eligibility blocker, CAPTCHA, unknown required field, ...
EXIT_NOT_BUILT = 5  # the command exists but its milestone is not built yet


class HelperError(Exception):
    """An expected failure with a message meant for the user. Exits with EXIT_ERROR."""

    exit_code = EXIT_ERROR


class WaitingForClaude(HelperError):
    """helper wrote an input file and needs Claude (this session) to write the output file."""

    exit_code = EXIT_WAITING_FOR_CLAUDE

    def __init__(self, run_id: str, input_file: str, output_file: str, note: str = "") -> None:
        self.run_id = run_id
        self.input_file = input_file
        self.output_file = output_file
        msg = (
            f"waiting for Claude: read {input_file} and write {output_file}, "
            f"then run `helper continue {run_id}`."
        )
        if note:
            msg += f"\n{note}"
        super().__init__(msg)


class NeedsUser(HelperError):
    """The run paused because only the user can decide or act."""

    exit_code = EXIT_NEEDS_USER


class NotBuiltYet(HelperError):
    exit_code = EXIT_NOT_BUILT
