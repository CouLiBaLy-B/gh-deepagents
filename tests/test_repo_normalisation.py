"""Tests for github_client.normalize_repo_full_name()."""
from __future__ import annotations

import pytest

from gh_deepagent.github_client import normalize_repo_full_name


@pytest.mark.parametrize("inp,expected", [
    ("octo/Hello-World",                                          "octo/Hello-World"),
    ("https://github.com/octo/Hello-World",                       "octo/Hello-World"),
    ("https://github.com/octo/Hello-World.git",                   "octo/Hello-World"),
    ("https://github.com/octo/Hello-World/",                      "octo/Hello-World"),
    ("http://github.com/octo/Hello-World",                        "octo/Hello-World"),
    ("git@github.com:octo/Hello-World.git",                       "octo/Hello-World"),
    ("  https://github.com/octo/Hello-World   ",                  "octo/Hello-World"),
    # User pastes the URL twice (the bug from the screenshot)
    ("https://github.com/https://github.com/octo/Hello-World.git", "octo/Hello-World"),
    # With basic auth in the URL
    ("https://x-access-token:xxx@github.com/octo/Hello-World.git", "octo/Hello-World"),
    # Repo names with dots and hyphens
    ("octo/my.repo-2",                                            "octo/my.repo-2"),
    ("CouLiBaLy-B/Unsupervised-Learning",                         "CouLiBaLy-B/Unsupervised-Learning"),
])
def test_normalises(inp, expected):
    assert normalize_repo_full_name(inp) == expected


@pytest.mark.parametrize("bad", [
    "",
    None,
    "not a repo",
    "http://example.com/foo/bar",
    "/some/path",
    "owner_only",
])
def test_rejects(bad):
    with pytest.raises((ValueError, TypeError)):
        normalize_repo_full_name(bad)
