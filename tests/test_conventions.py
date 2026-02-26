"""Conventions Acceptance Tests

These tests validate that conventions.py is correct, complete, and immutable.
The conventions file is the bedrock social contract of the distro - filenames,
paths, and naming standards that all tools agree on.

Exit criteria verified:
1. All canonical constants have correct pinned values
2. All filename/directory constants are non-empty strings
3. BACKUP_EXCLUDE includes KEYS_FILENAME (security: never backup keys)
4. BACKUP_INCLUDE includes required config files
5. Module contains NO functions and NO classes (pure constants)
6. Every constant referenced by other modules (server, watchdog, service) exists
7. BACKUP_INCLUDE and BACKUP_EXCLUDE are disjoint (no contradictions)
"""

import inspect
import types

from amplifier_distro import conventions


class TestCanonicalValues:
    """Verify every canonical constant has the correct pinned value.

    Antagonist note: Each test pins exactly one constant. If any value
    changes without updating these tests, it will fail. This IS the
    social contract.
    """

    def test_amplifier_home(self):
        assert conventions.AMPLIFIER_HOME == "~/.amplifier"

    def test_memory_dir(self):
        assert conventions.MEMORY_DIR == "memory"

    def test_memory_store_filename(self):
        assert conventions.MEMORY_STORE_FILENAME == "memory-store.yaml"

    def test_work_log_filename(self):
        assert conventions.WORK_LOG_FILENAME == "work-log.yaml"

    def test_transcript_filename(self):
        assert conventions.TRANSCRIPT_FILENAME == "transcript.jsonl"

    def test_keys_filename(self):
        assert conventions.KEYS_FILENAME == "keys.env"

    def test_settings_filename(self):
        assert conventions.SETTINGS_FILENAME == "settings.yaml"

    def test_server_dir(self):
        assert conventions.SERVER_DIR == "server"

    def test_server_socket(self):
        assert conventions.SERVER_SOCKET == "server.sock"

    def test_server_pid_file(self):
        assert conventions.SERVER_PID_FILE == "server.pid"

    def test_server_default_port(self):
        assert conventions.SERVER_DEFAULT_PORT == 8400

    def test_watchdog_pid_file(self):
        assert conventions.WATCHDOG_PID_FILE == "watchdog.pid"

    def test_watchdog_log_file(self):
        assert conventions.WATCHDOG_LOG_FILE == "watchdog.log"

    def test_service_name(self):
        assert conventions.SERVICE_NAME == "amplifier-distro"

    def test_launchd_label(self):
        assert conventions.LAUNCHD_LABEL == "com.amplifier.distro"

    def test_backup_repo_pattern(self):
        assert conventions.BACKUP_REPO_PATTERN == "{github_handle}/amplifier-backup"


class TestStringConstants:
    """Verify all filename and directory constants are non-empty strings.

    Antagonist note: This catches accidental `= ""` or `= None` assignments.
    Every constant that represents a filename or directory must be a
    non-empty string.
    """

    FILENAME_CONSTANTS = [
        "MEMORY_STORE_FILENAME",
        "WORK_LOG_FILENAME",
        "TRANSCRIPT_FILENAME",
        "KEYS_FILENAME",
        "SETTINGS_FILENAME",
        "SERVER_SOCKET",
        "SERVER_PID_FILE",
        "WATCHDOG_PID_FILE",
        "WATCHDOG_LOG_FILE",
        "SERVICE_NAME",
        "LAUNCHD_LABEL",
    ]

    DIRECTORY_CONSTANTS = [
        "MEMORY_DIR",
        "SERVER_DIR",
    ]

    def test_all_filename_constants_are_nonempty_strings(self):
        for name in self.FILENAME_CONSTANTS:
            value = getattr(conventions, name)
            assert isinstance(value, str), f"{name} should be str, got {type(value)}"
            assert len(value) > 0, f"{name} should not be empty"

    def test_all_directory_constants_are_nonempty_strings(self):
        for name in self.DIRECTORY_CONSTANTS:
            value = getattr(conventions, name)
            assert isinstance(value, str), f"{name} should be str, got {type(value)}"
            assert len(value) > 0, f"{name} should not be empty"


class TestBackupSecurity:
    """Verify backup lists enforce security invariants.

    Antagonist note: The BACKUP_EXCLUDE list MUST contain KEYS_FILENAME.
    If someone removes it, credentials could be backed up to GitHub.
    This is a security-critical assertion.
    """

    def test_backup_exclude_contains_keys(self):
        """KEYS_FILENAME must be excluded from backups (security: never backup keys)."""
        assert conventions.KEYS_FILENAME in conventions.BACKUP_EXCLUDE

    def test_backup_include_contains_memory_dir(self):
        assert conventions.MEMORY_DIR in conventions.BACKUP_INCLUDE

    def test_backup_include_contains_settings(self):
        assert conventions.SETTINGS_FILENAME in conventions.BACKUP_INCLUDE

    def test_backup_include_and_exclude_are_disjoint(self):
        """No item should appear in both include and exclude lists."""
        overlap = set(conventions.BACKUP_INCLUDE) & set(conventions.BACKUP_EXCLUDE)
        assert not overlap, f"Items in both INCLUDE and EXCLUDE: {overlap}"


class TestModulePurity:
    """Verify the conventions module is pure constants - no functions, no classes.

    Antagonist note: conventions.py must remain a pure data file. Functions
    and classes introduce behavior that could diverge from the contract.
    If you need logic, put it in a different module.
    """

    def test_no_functions_defined(self):
        """Module must have zero function definitions."""
        functions = [
            name
            for name, obj in inspect.getmembers(conventions)
            if inspect.isfunction(obj) and obj.__module__ == conventions.__name__
        ]
        assert functions == [], f"Unexpected functions in conventions: {functions}"

    def test_no_classes_defined(self):
        """Module must have zero class definitions."""
        classes = [
            name
            for name, obj in inspect.getmembers(conventions)
            if inspect.isclass(obj) and obj.__module__ == conventions.__name__
        ]
        assert classes == [], f"Unexpected classes in conventions: {classes}"

    def test_all_public_names_are_data_not_callables(self):
        """Every public name must be str, int, list, or dict - no callables.

        Antagonist note: This catches accidentally defined lambdas,
        imported functions, or other non-constant objects.
        """
        allowed_types = (str, int, list, dict)
        for name in dir(conventions):
            if name.startswith("_"):
                continue
            obj = getattr(conventions, name)
            # Skip imported modules (e.g., if someone adds `import os`)
            if isinstance(obj, types.ModuleType):
                continue
            assert isinstance(obj, allowed_types), (
                f"{name} is {type(obj).__name__}, expected one of "
                f"(str, int, list, dict)"
            )


class TestCrossModuleReferences:
    """Verify every constant referenced by other modules actually exists.

    Antagonist note: server/cli.py, watchdog.py, and service.py depend
    on conventions having specific constants. If a constant is renamed or
    removed, these tests catch the breakage before runtime.
    """

    def test_constants_used_by_server(self):
        """server/cli.py uses SERVER_DEFAULT_PORT as its default."""
        assert hasattr(conventions, "SERVER_DEFAULT_PORT")
        assert conventions.SERVER_DEFAULT_PORT == 8400

    def test_backup_repo_pattern_has_placeholder(self):
        """BACKUP_REPO_PATTERN is used with .format(github_handle=...)."""
        assert hasattr(conventions, "BACKUP_REPO_PATTERN")
        assert "{github_handle}" in conventions.BACKUP_REPO_PATTERN

    def test_constants_used_by_watchdog(self):
        """server/watchdog.py uses WATCHDOG_PID_FILE and WATCHDOG_LOG_FILE."""
        assert hasattr(conventions, "WATCHDOG_PID_FILE")
        assert hasattr(conventions, "WATCHDOG_LOG_FILE")

    def test_constants_used_by_service(self):
        """service.py uses SERVICE_NAME and LAUNCHD_LABEL."""
        assert hasattr(conventions, "SERVICE_NAME")
        assert hasattr(conventions, "LAUNCHD_LABEL")
