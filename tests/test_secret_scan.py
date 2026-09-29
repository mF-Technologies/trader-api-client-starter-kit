import subprocess

from detect_secrets.core.scan import scan_file
from detect_secrets.settings import default_settings

INTERNAL_ONLY_PATHS = {
    "AGENTS.md",
    "SUPPORT.md",
    "compatibility.yaml",
    ".github/workflows/live-read-only.yml",
}


def test_tracked_files_do_not_contain_unallowlisted_secrets() -> None:
    filenames = subprocess.check_output(
        ["git", "ls-files"], text=True, encoding="utf-8"
    ).splitlines()

    with default_settings():
        findings = [
            f"{filename}:{secret.line_number}:{secret.type}"
            for filename in filenames
            for secret in scan_file(filename)
        ]

    assert findings == []


def test_tracked_tree_excludes_internal_only_paths() -> None:
    filenames = subprocess.check_output(
        ["git", "ls-files"], text=True, encoding="utf-8"
    ).splitlines()

    assert not INTERNAL_ONLY_PATHS.intersection(filenames)
