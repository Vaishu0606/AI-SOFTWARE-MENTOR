"""
Controlled test execution — Phase 11.

The **only** module in ``app/`` that may start a subprocess, and the reason it
exists is narrow: a verified result in this system means *a runner exited with a
code*, and a runner is a program.  Everything else in Phase 11 is deterministic
string and file work.

What "controlled" means here, precisely
----------------------------------------
Six properties, each of which is a separate test:

1. **The command is a constant.**  The runner is always
   ``<the server's own interpreter> -m pytest`` with a fixed argument list.  No
   part of it comes from the request, the proposal, the test plan, the model or
   the repository.  There is no parameter through which a caller could name a
   binary, a flag or an argument.
2. **No shell, ever.**  ``subprocess.run`` with ``shell=False`` and an argument
   *list*.  A shell would re-introduce quoting, metacharacters and PATH
   resolution, all three of which are injection surfaces that a fixed prefix does
   not otherwise have.
3. **The targets are validated against the repository, not trusted.**  Each one
   must be repository-relative, must resolve to a real file inside the run's own
   root through :func:`app.analysis.fix_proposer.resolve_safe_source_path`, and
   must not be a secret file.  A test *path* is repository content, and
   repository content is untrusted input.
4. **The repository's pytest configuration cannot add options.**  ``-o
   addopts=`` overrides whatever ``pytest.ini``/``pyproject.toml``/``tox.ini``
   says, so a repository cannot smuggle ``--pdb``, a plugin load or a coverage
   target into the invocation.  Without this, "the command is a constant" would be
   false in the only sense that matters.
5. **The environment is built from an allowlist, not inherited.**  Nothing the
   server process holds is passed through except OS-level necessities.  In
   particular ``PYTEST_ADDOPTS``, ``PYTHONPATH`` and every credential-bearing
   variable are dropped: the first two would change the invocation, and the rest
   have no business in a child process.
6. **It is bounded.**  A hard wall-clock timeout, and captured output truncated
   to a fixed size before it can reach a stored result, the event log or the API.

And what it is *not*
-------------------
This is **not** an operating-system sandbox.  Running a Python test suite means
running Python code that lives in the repository, including its ``conftest.py``,
and no amount of argument validation changes that.  Pretending otherwise would
be the most dangerous kind of wrong in a security control, so the limitation is
stated in this module, in the run's own limitations list, and in
``docs/architecture.md``: the executor controls *what* is run and *how* it is
invoked, and the operating system controls the blast radius.

The suite Phase 11 runs is the one that already existed.  It writes no test source
(Phase 10's decision, unchanged), which is why a green run here means "no
regression" and not "the diagnosed defect is fixed" unless a pre-existing test
happens to discriminate.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from app.analysis.fix_proposer import (
    SourceUnavailableError,
    UnsafeSourcePathError,
    resolve_safe_source_path,
)
from app.models.verification import (
    MAX_CAPTURED_LINE_CHARS,
    MAX_CAPTURED_OUTPUT_CHARS,
    TestRunResult,
)

logger = logging.getLogger(__name__)


# ── The allowlisted command ───────────────────────────────────────────────────

#: Arguments that precede the test targets.  Fixed, exhaustive, and the reason
#: ``-o addopts=`` is in it.
_FIXED_ARGS: tuple[str, ...] = (
    "-m",
    "pytest",
    "-q",
    "--no-header",
    # Do not write a cache directory into the analysed repository.
    "-p",
    "no:cacheprovider",
    # Ignore whatever the repository's own pytest config puts in ``addopts``.
    "-o",
    "addopts=",
)

#: Environment variables forwarded to the child.  Everything here is an OS-level
#: necessity; note what is *absent* — ``PYTEST_ADDOPTS`` and ``PYTHONPATH`` would
#: both change what the runner does, and no credential variable belongs in a child
#: process at all.
_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
    }
)

#: Forced into the child's environment regardless of what the server holds.
_ENV_OVERRIDES: dict[str, str] = {
    # Third-party pytest plugins are not loaded implicitly.  A plugin is arbitrary
    # code, and the repository may declare one.
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    # No ``__pycache__`` directories written into the analysed repository.
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
}

#: pytest's exit codes, named.  Only the ones that mean something different from
#: "the tests did not pass" are named here; the rest are all "failed".
EXIT_OK = 0
EXIT_NOTESTSCOLLECTED = 5

#: ``1 failed, 2 passed in 0.12s`` → the counts, wherever they appear.
_COUNT_RE = re.compile(
    r"\b(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b"
)


class TestExecutionBlocked(Exception):
    """The runner could not be attempted, or was pointed at nothing runnable.

    Distinct from a failing run: nothing was concluded about the change, so
    reporting it as ``FAILED`` would be inventing a result.  The verification
    stage turns this into ``BLOCKED``.
    """


class ControlledTestExecutor:
    """Runs a repository's *existing* test files under a fixed, bounded command.

    Not a sandbox — see the module docstring.  Every collaborator is injectable so
    the argument construction, the environment construction and the path
    validation can each be tested without starting a process.
    """

    def __init__(
        self,
        *,
        interpreter: str | None = None,
        default_timeout_seconds: float = 120.0,
        max_targets: int = 10,
        runner=subprocess.run,
    ) -> None:
        """
        Args:
            interpreter: The program to invoke.  Defaults to the running
                interpreter, so the runner is always the one this server can
                actually find pytest with.  Injectable only so tests can assert
                the argument vector; a caller cannot route this to a shell.
            default_timeout_seconds: Wall-clock limit.  The server supplies its own
                configured value.
            max_targets: Ceiling on how many files one invocation may name.  Bounds
                the argument vector and the wall clock.
            runner: ``subprocess.run``-shaped callable.  Injected in tests; the
                production value is always ``shell=False``-compatible.
        """
        self._interpreter = interpreter or sys.executable
        self._default_timeout = default_timeout_seconds
        self._max_targets = max_targets
        self._runner = runner

    # ── Command construction ─────────────────────────────────────────────

    def build_command(self, targets: list[str]) -> list[str]:
        """Return the exact argument vector that will be executed.

        Separate from :meth:`run` so the command can be asserted on directly.  The
        only variable part is the validated target list, which is appended last so
        a target can never be mistaken for a flag.
        """
        if not targets:
            raise TestExecutionBlocked(
                "No test target was requested, so no runner was started."
            )
        return [self._interpreter, *_FIXED_ARGS, *targets]

    def build_environment(self) -> dict[str, str]:
        """Return the child's environment, built from the allowlist."""
        env = {
            name: value
            for name, value in os.environ.items()
            if name in _ENV_ALLOWLIST
        }
        env.update(_ENV_OVERRIDES)
        return env

    # ── Path validation ──────────────────────────────────────────────────

    def validate_targets(self, root: Path, targets: list[str]) -> list[str]:
        """Check every target against ``root`` and return them unchanged.

        Raises :class:`TestExecutionBlocked` rather than
        :class:`UnsafeSourcePathError` for a bad target, because from here the
        only meaningful answer is "we will not run that".
        """
        if not targets:
            raise TestExecutionBlocked(
                "No runnable test file was identified, so no test was executed."
            )
        if len(targets) > self._max_targets:
            raise TestExecutionBlocked(
                f"The plan named {len(targets)} test file(s) and the executor "
                f"accepts at most {self._max_targets}. Narrow the plan rather "
                f"than raise the ceiling automatically."
            )

        seen: list[str] = []
        for target in targets:
            try:
                resolve_safe_source_path(root, target)
            except (UnsafeSourcePathError, SourceUnavailableError) as exc:
                raise TestExecutionBlocked(
                    f"Test target {target!r} is not a readable file inside the "
                    f"repository and was not run: {exc}"
                ) from exc
            if target not in seen:
                seen.append(target)
        return seen

    # ── Execution ────────────────────────────────────────────────────────

    def run(
        self,
        *,
        root: Path,
        targets: list[str],
        timeout_seconds: float | None = None,
    ) -> TestRunResult:
        """Run the validated ``targets`` and return what happened.

        Returns a :class:`TestRunResult` for every outcome *except* the ones that
        mean the runner could not be started at all, which raise
        :class:`TestExecutionBlocked` — the caller must not be able to read an
        ``exit_code is None`` result as "ran and passed nothing".
        """
        validated = self.validate_targets(root, targets)
        command = self.build_command(validated)
        timeout = (
            self._default_timeout if timeout_seconds is None else timeout_seconds
        )
        if timeout <= 0:
            raise TestExecutionBlocked(
                f"The verification timeout is {timeout}s, so no runner was "
                f"started. A run that cannot be bounded is not run."
            )

        logger.info(
            "Running controlled tests in %s: %s", root, " ".join(command)
        )

        started = time.monotonic()
        timed_out = False
        try:
            completed = self._runner(
                command,
                cwd=str(root),
                env=self.build_environment(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
            )
            exit_code: int | None = int(completed.returncode)
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = None
            stdout = _as_text(exc.stdout)
            stderr = _as_text(exc.stderr)
            logger.warning(
                "Controlled test run in %s exceeded its %ss limit", root, timeout
            )
        except OSError as exc:
            # The interpreter could not be started at all.  Nothing was executed,
            # so nothing may be concluded.
            raise TestExecutionBlocked(
                f"The test runner could not be started "
                f"({type(exc).__name__}: {exc.strerror or exc}), so no test was "
                f"executed and nothing was verified."
            ) from exc
        duration = time.monotonic() - started

        stdout_excerpt, stdout_truncated = _bound(stdout)
        stderr_excerpt, stderr_truncated = _bound(stderr)
        tests_run, passed, failed = _parse_counts(stdout)

        return TestRunResult(
            command=list(command),
            exit_code=exit_code,
            duration_seconds=round(duration, 3),
            timed_out=timed_out,
            stdout_excerpt=stdout_excerpt,
            stderr_excerpt=stderr_excerpt,
            output_truncated=stdout_truncated or stderr_truncated,
            targets=list(validated),
            tests_requested=len(validated),
            tests_run=tests_run,
            passed=passed,
            failed=failed,
        )


# ── Output bounding ──────────────────────────────────────────────────────────


def _as_text(value: object) -> str:
    """Coerce whatever a killed process left behind into text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _bound(text: str) -> tuple[str, bool]:
    """Truncate ``text`` to the capture cap, longest lines first.

    Per-line capping comes first because a single pathological line (a giant
    ``repr``, a minified blob) is the usual way an 8 KB budget gets blown.  The
    tail is kept when truncating, because a test runner puts its summary last and
    the summary is the part worth reading.
    """
    lines: list[str] = []
    over_budget = False
    for line in text.splitlines():
        if len(line) > MAX_CAPTURED_LINE_CHARS:
            line = line[:MAX_CAPTURED_LINE_CHARS] + " …[line truncated]"
            over_budget = True
        lines.append(line)
    joined = "\n".join(lines)
    if len(joined) <= MAX_CAPTURED_OUTPUT_CHARS:
        return joined, over_budget
    keep = MAX_CAPTURED_OUTPUT_CHARS // 2
    return (
        joined[:keep]
        + f"\n…[{len(joined) - 2 * keep} characters omitted]…\n"
        + joined[-keep:],
        True,
    )


def _parse_counts(stdout: str) -> tuple[int | None, int | None, int | None]:
    """Read the runner's own counts off its last summary line.

    Returns ``(None, None, None)`` when there is no parseable summary, which is
    the honest answer: "the runner exited 0 and printed no counts" is a different
    claim from "0 tests ran" and is reported as the former.
    """
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not text or " in " not in text:
            continue
        matches = _COUNT_RE.findall(text)
        if not matches:
            continue
        passed = 0
        failed = 0
        for count_text, label in matches:
            count = int(count_text)
            if label == "passed":
                passed += count
            elif label in ("failed", "error", "errors"):
                failed += count
        return passed + failed, passed, failed
    return None, None, None


#: A ready-to-use executor.  Stateless apart from configuration.
DEFAULT_TEST_EXECUTOR = ControlledTestExecutor()


__all__ = [
    "DEFAULT_TEST_EXECUTOR",
    "EXIT_NOTESTSCOLLECTED",
    "EXIT_OK",
    "ControlledTestExecutor",
    "TestExecutionBlocked",
]
