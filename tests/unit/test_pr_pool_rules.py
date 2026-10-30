"""Slice B4's PR documents (ADR-0003 §1-§2 and the second 2026-10-29 addendum)."""

from __future__ import annotations

from provenance.ingest.pr_pool import Artifact, description_text, pr_document

SHA = "ab" * 20


def doc(kind, pr, artifact_id, text, *, bot=False):
    artifact = Artifact(kind, pr, artifact_id, text, bot)
    return pr_document(artifact, commit_sha=SHA, repo_slug="apache/airflow")


def test_a_description_is_its_title_and_body_without_html_comments():
    text = description_text("Fix alpha", "Because.<!-- describe\nyour change -->\nDone.")
    assert text == "Fix alpha\n\nBecause.\nDone."


def test_each_kind_has_its_path_and_its_evidence():
    described = doc("pull_request", 11, None, "Fix alpha\n\nBecause.")
    review = doc("review_comment", 11, 99, "Why not two?")
    talk = doc("issue_comment", 11, 5, "Agreed.")
    assert (described.path, review.path, talk.path) == (
        "pull/11",
        "pull/11/review/99",
        "pull/11/comment/5",
    )
    assert described.evidence == (("pull_request", "11", "self"), ("commit", SHA, "linked"))
    assert review.evidence == (
        ("review_comment", "99", "self"),
        ("pull_request", "11", "linked"),
        ("commit", SHA, "linked"),
    )
    assert talk.evidence[0] == ("issue_comment", "5", "self")


def test_bot_written_and_blank_text_is_never_a_document():
    assert doc("issue_comment", 11, 2, "Congratulations!", bot=True) is None
    assert doc("pull_request", 12, None, "Bump x", bot=True) is None
    assert doc("issue_comment", 11, 3, "  \n\t ") is None


def test_the_same_text_is_the_same_document():
    first, again = doc("review_comment", 11, 99, "Why?"), doc("review_comment", 11, 99, "Why?")
    assert first.chunk_id == again.chunk_id
    assert doc("review_comment", 11, 99, "Why not?").chunk_id != first.chunk_id
