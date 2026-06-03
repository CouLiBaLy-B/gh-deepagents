from gh_deepagent.github_client import IssueRef


def test_parse_issue_url():
    r = IssueRef.from_url("https://github.com/octocat/Hello-World/issues/42")
    assert r.owner == "octocat"
    assert r.repo == "Hello-World"
    assert r.number == 42
    assert r.full_name == "octocat/Hello-World"


def test_parse_bad_url():
    import pytest
    with pytest.raises(ValueError):
        IssueRef.from_url("https://example.com/foo")
