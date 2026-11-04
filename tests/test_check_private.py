"""The privacy check's handling of the merge commit GitHub builds for a pull request."""

import importlib.util
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "check_private", ROOT / "tools/check_private.py"
)
check_private = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_private)

NOREPLY = "1+someone@users.noreply.github.com"
PRIVATE = "someone" + "@" + "example.org"  # stands in for a real address


def git(repo, *args, email=NOREPLY, committer=None):
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": committer or email,
        "HOME": str(repo),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a").write_text("a")
    git(tmp_path, "add", "a")
    git(tmp_path, "commit", "-q", "-m", "one")
    git(tmp_path, "checkout", "-q", "-b", "side")
    (tmp_path / "b").write_text("b")
    git(tmp_path, "add", "b")
    git(tmp_path, "commit", "-q", "-m", "two")
    git(tmp_path, "checkout", "-q", "main")
    monkeypatch.setattr(check_private, "ROOT", tmp_path)
    return tmp_path


PR_REF = "refs/pull/1/merge"


def in_pull_request(monkeypatch, ref=PR_REF):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REF", ref)


def merge(repo, email):
    git(repo, "merge", "-q", "--no-ff", "-m", "merge", "side", email=email,
        committer="noreply@github.com")  # fmt: skip


def test_ordinary_history_is_checked(repo):
    assert check_private.commit_emails() == {NOREPLY}


def test_a_private_email_in_a_branch_commit_is_found(repo):
    (repo / "c").write_text("c")
    git(repo, "add", "c")
    git(repo, "commit", "-q", "-m", "three", email=PRIVATE)
    assert PRIVATE in check_private.commit_emails()


def test_the_pull_request_merge_commit_is_skipped_only_in_a_pull_request(
    repo, monkeypatch
):
    merge(repo, PRIVATE)  # what GitHub builds: the opener's email as author
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    assert PRIVATE in check_private.commit_emails()  # not CI at all
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    assert PRIVATE in check_private.commit_emails()  # a push to main
    in_pull_request(monkeypatch, ref="refs/heads/feature")
    assert (
        PRIVATE in check_private.commit_emails()
    )  # a pull request, but not the merge ref
    in_pull_request(monkeypatch)
    assert check_private.commit_emails() == {NOREPLY}


def test_a_real_merge_commit_further_back_is_still_checked(repo, monkeypatch):
    merge(repo, PRIVATE)
    (repo / "d").write_text("d")
    git(repo, "add", "d")
    git(repo, "commit", "-q", "-m", "after")  # HEAD is now an ordinary commit
    in_pull_request(monkeypatch)
    assert PRIVATE in check_private.commit_emails()


def test_on_the_merge_ref_head_is_skipped_whatever_it_is(repo, monkeypatch):
    (repo / "c").write_text("c")
    git(repo, "add", "c")
    git(repo, "commit", "-q", "-m", "three", email=PRIVATE)
    in_pull_request(monkeypatch)
    # the ref says "merge commit", but HEAD is skipped regardless: that is why CI must
    # check out the merge ref and nothing else. Here HEAD is the private commit.
    assert PRIVATE not in check_private.commit_emails()
    assert check_private.commit_emails() == {NOREPLY}


def test_a_shallow_checkout_of_the_merge_commit_is_handled(repo, monkeypatch, tmp_path):
    """actions/checkout fetches depth 1 by default: the merge commit shows no
    parents there, which is what defeated detecting it by its parents."""
    merge(repo, PRIVATE)
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(clone)], check=True
    )
    monkeypatch.setattr(check_private, "ROOT", clone)
    in_pull_request(monkeypatch)
    assert PRIVATE not in check_private.commit_emails()
    monkeypatch.delenv("GITHUB_EVENT_NAME")
    assert PRIVATE in check_private.commit_emails()
