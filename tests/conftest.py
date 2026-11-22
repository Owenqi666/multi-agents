"""Suite-wide pytest setup."""

import os


def pytest_configure():
    # Git hands its hooks variables such as GIT_DIR and GIT_INDEX_FILE, and a
    # commit made from a linked worktree runs this suite through pre-commit. Any
    # fixture calling plain `git` would then act on the repository being
    # committed to instead of its own scratch one, so the session starts
    # detached from whichever git process launched it.
    for name in [name for name in os.environ if name.startswith("GIT_")]:
        del os.environ[name]
