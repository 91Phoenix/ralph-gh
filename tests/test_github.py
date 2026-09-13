"""Seam: ralph_gh.github — the gh CLI runner and the shapes it parses."""

import json
import subprocess

import pytest

from ralph_gh.github import (FAILED, NONE, PENDING, SUCCESS, Gh, GhError,
                             GitHubClient)


class FakeRun:
    """Scripted `gh` process. Each call pops the next (rc, stdout, stderr)
    unless a matcher function decides the reply from argv."""

    def __init__(self, replies=None, matcher=None):
        self.replies = list(replies or [])
        self.matcher = matcher
        self.calls = []

    def __call__(self, argv, capture_output, text, input, timeout):
        self.calls.append((argv, input))
        if self.matcher:
            rc, out, err = self.matcher(argv, input)
        else:
            rc, out, err = self.replies.pop(0)
        return subprocess.CompletedProcess(argv, rc, out, err)


def gh_with(*replies, matcher=None):
    run = FakeRun(replies, matcher)
    return Gh(run=run), run


class TestGh:
    def test_get_params_become_fields(self):
        gh, run = gh_with((0, '{"a":1}', ""))
        assert gh.api("/repos/o/r/pulls", params={"state": "all", "per_page": 5}) == {"a": 1}
        argv, stdin = run.calls[0]
        assert argv[:4] == ["gh", "api", "-X", "GET"]
        assert "repos/o/r/pulls" in argv
        assert "-f" in argv and "state=all" in argv
        assert "-F" in argv and "per_page=5" in argv
        assert stdin is None

    def test_post_body_on_stdin(self):
        gh, run = gh_with((0, '{"number":7}', ""))
        gh.api("repos/o/r/pulls", "POST", body={"title": "t", "draft": True})
        argv, stdin = run.calls[0]
        assert "--input" in argv and "-" in argv
        assert json.loads(stdin) == {"title": "t", "draft": True}

    def test_paginate_flattens_slurped_pages(self):
        gh, run = gh_with((0, '[[{"id":1}],[{"id":2}]]', ""))
        assert gh.api("repos/o/r/issues", paginate=True) == [{"id": 1}, {"id": 2}]
        assert "--paginate" in run.calls[0][0] and "--slurp" in run.calls[0][0]

    def test_empty_response_is_none(self):
        gh, _ = gh_with((0, "", ""))
        assert gh.api("repos/o/r/issues/1/labels/x", "DELETE") is None

    def test_error_carries_status(self):
        gh, _ = gh_with((1, '{"message":"Not Found"}', "gh: Not Found (HTTP 404)"))
        with pytest.raises(GhError) as ei:
            gh.api("repos/o/r")
        assert ei.value.status == 404

    def test_timeout_is_gherror(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired("gh", 1)
        with pytest.raises(GhError):
            Gh(run=boom).api("user")

    def test_graphql_ok(self):
        gh, run = gh_with((0, '{"data":{"viewer":{"login":"me"}}}', ""))
        assert gh.graphql("query { viewer { login } }") == {"viewer": {"login": "me"}}
        assert json.loads(run.calls[0][1])["query"].startswith("query")

    def test_graphql_scope_error_flagged(self):
        body = json.dumps({"errors": [{"type": "INSUFFICIENT_SCOPES",
                                       "message": "needs read:project"}]})
        gh, _ = gh_with((1, body, "gh: needs read:project"))
        with pytest.raises(GhError) as ei:
            gh.graphql("query {}")
        assert ei.value.insufficient_scopes
        assert "read:project" in str(ei.value)


def client_for(matcher):
    run = FakeRun(matcher=matcher)
    return GitHubClient(Gh(run=run)), run


def route(table):
    """Match argv against substrings; reply with JSON."""
    def m(argv, stdin):
        joined = " ".join(argv)
        for needle, payload in table:
            if needle in joined:
                return 0, json.dumps(payload), ""
        return 1, "", "gh: Not Found (HTTP 404)"
    return m


class TestChecksStatus:
    def _client(self, runs, status):
        return client_for(route([
            ("check-runs", {"check_runs": runs}),
            ("/status", status)]))[0]

    def test_none_when_no_ci(self):
        assert self._client([], {"state": "pending", "total_count": 0}
                            ).checks_status("o/r", "abc") == NONE

    def test_success(self):
        c = self._client([{"status": "completed", "conclusion": "success"},
                          {"status": "completed", "conclusion": "skipped"}],
                         {"state": "success", "total_count": 1})
        assert c.checks_status("o/r", "abc") == SUCCESS

    def test_failure_wins_over_pending(self):
        c = self._client([{"status": "in_progress", "conclusion": None},
                          {"status": "completed", "conclusion": "failure"}],
                         {"state": "pending", "total_count": 0})
        assert c.checks_status("o/r", "abc") == FAILED

    def test_pending(self):
        c = self._client([{"status": "queued", "conclusion": None}],
                         {"state": "pending", "total_count": 0})
        assert c.checks_status("o/r", "abc") == PENDING

    def test_legacy_status_failure(self):
        c = self._client([], {"state": "failure", "total_count": 2})
        assert c.checks_status("o/r", "abc") == FAILED

    def test_api_error_reads_as_pending(self):
        c, _ = client_for(lambda a, s: (1, "", "gh: boom (HTTP 502)"))
        assert c.checks_status("o/r", "abc") == PENDING


class TestPullRequests:
    PRS = [{"number": 9, "state": "closed", "merged_at": "2026-01-01T00:00:00Z",
            "html_url": "u9"},
           {"number": 8, "state": "open", "merged_at": None, "html_url": "u8"},
           {"number": 7, "state": "closed", "merged_at": None, "html_url": "u7"}]

    def test_find_pr_by_state(self):
        c, run = client_for(route([("pulls", self.PRS)]))
        assert c.find_pr("o/r", "feature/issue-1", "OPEN") == ("OPEN", "8", "u8")
        assert c.find_pr("o/r", "feature/issue-1", "MERGED") == ("MERGED", "9", "u9")
        assert c.find_pr("o/r", "feature/issue-1", "CLOSED") == ("CLOSED", "7", "u7")
        assert "head=o:feature/issue-1" in " ".join(run.calls[0][0])

    def test_find_pr_none(self):
        c, _ = client_for(route([("pulls", [])]))
        assert c.find_pr("o/r", "b", "OPEN") is None

    def test_create_pr(self):
        c, run = client_for(route([("pulls", {"number": 3, "html_url": "u3"})]))
        assert c.create_pr("o/r", "b", "main", "t", "body", draft=True) == ("3", "u3")
        body = json.loads(run.calls[0][1])
        assert body == {"title": "t", "head": "b", "base": "main",
                        "body": "body", "draft": True}

    def test_create_pr_failure_is_none(self):
        c, _ = client_for(lambda a, s: (1, "", "gh: Unprocessable (HTTP 422)"))
        assert c.create_pr("o/r", "b", "main", "t", "body") is None

    def test_pr_info(self):
        c, _ = client_for(route([("pulls/3", {"number": 3, "state": "open",
                                              "draft": True, "node_id": "N",
                                              "head": {"sha": "abc"},
                                              "html_url": "u"})]))
        info = c.pr_info("o/r", "3")
        assert info["state"] == "OPEN" and info["draft"] and info["head_sha"] == "abc"

    def test_comment_count_sums_three_sources(self):
        c, _ = client_for(route([
            ("pulls/3/comments", [{"id": 1}, {"id": 2}]),
            ("issues/3/comments", [{"id": 3}]),
            ("pulls/3/reviews", [{"body": "LGTM"}, {"body": ""}])]))
        assert c.pr_comment_count("o/r", "3") == 4

    def test_comment_count_unknown_is_minus_one(self):
        c, _ = client_for(lambda a, s: (1, "", "gh: boom (HTTP 500)"))
        assert c.pr_comment_count("o/r", "3") == -1

    def test_comments_md_renders_ids_and_paths(self):
        c, _ = client_for(route([
            ("pulls/3/comments", [{"id": 11, "path": "a.py", "line": 4,
                                   "user": {"login": "rev"}, "body": "fix\nthis"}]),
            ("issues/3/comments", [{"id": 12, "user": {"login": "rev"}, "body": "hi"}]),
            ("pulls/3/reviews", [{"state": "COMMENTED", "body": "sum",
                                  "user": {"login": "rev"}}])]))
        md = c.pr_comments_md("o/r", "3")
        assert "id 11" in md and "`a.py` line 4" in md and "  this" in md
        assert "id 12" in md and "COMMENTED by @rev" in md
        assert "comments/<id>/replies" in md


class TestIssues:
    def test_issue_blockers_parse_repository_url(self):
        c, _ = client_for(route([("dependencies/blocked_by", [
            {"number": 4, "state": "open",
             "repository_url": "https://api.github.com/repos/o/r"},
            {"number": 5, "state": "closed",
             "repository_url": "https://api.github.com/repos/x/y"}])]))
        assert c.issue_blockers("o/r#1") == [("o/r#4", "OPEN"), ("x/y#5", "CLOSED")]

    def test_issue_blockers_endpoint_missing_is_empty(self):
        c, _ = client_for(lambda a, s: (1, "", "gh: Not Found (HTTP 404)"))
        assert c.issue_blockers("o/r#1") == []

    def test_label_remove_404_ok(self):
        c, _ = client_for(lambda a, s: (1, "", "gh: Not Found (HTTP 404)"))
        c.remove_label("o/r#1", "x")  # no raise

    def test_writes_hit_expected_paths(self):
        c, run = client_for(lambda a, s: (0, "{}", ""))
        c.comment("o/r#1", "hi")
        c.add_labels("o/r#1", ["needs-human"])
        c.assign("o/r#1", "me")
        c.unassign("o/r#1", "me")
        c.close_issue("o/r#1")
        paths = [a[0][4] for a in run.calls]
        assert paths == ["repos/o/r/issues/1/comments", "repos/o/r/issues/1/labels",
                         "repos/o/r/issues/1/assignees", "repos/o/r/issues/1/assignees",
                         "repos/o/r/issues/1"]
        assert json.loads(run.calls[4][1]) == {"state": "closed",
                                               "state_reason": "completed"}


PROJECT_PAGE = {"data": {"node": {"items": {
    "pageInfo": {"hasNextPage": False, "endCursor": None},
    "nodes": [
        {"id": "I1", "fieldValueByName": {"name": "Ready"},
         "content": {"__typename": "Issue", "id": "N1", "number": 1,
                     "title": "One", "body": "b", "state": "OPEN",
                     "url": "u1", "updatedAt": "1970-01-01T00:01:00Z",
                     "repository": {"nameWithOwner": "o/r"},
                     "labels": {"nodes": [{"name": "ready-for-agent"}]},
                     "assignees": {"nodes": []}}},
        {"id": "I2", "fieldValueByName": None,
         "content": {"__typename": "DraftIssue"}},
        {"id": "I3", "fieldValueByName": {"name": "Done"},
         "content": {"__typename": "PullRequest", "number": 5}},
    ]}}}}


class TestProject:
    def test_items_keep_only_issues(self):
        c, _ = client_for(lambda a, s: (0, json.dumps(PROJECT_PAGE), ""))
        items = c.project_items("P")
        assert len(items) == 1
        it = items[0]
        assert it["key"] == "o/r#1" and it["item_id"] == "I1"
        assert it["status"] == "Ready" and it["updated"] == 60
        assert it["labels"] == ["ready-for-agent"] and it["assignees"] == []

    def test_resolve_project_tries_user_then_org(self):
        calls = []

        def m(argv, stdin):
            q = json.loads(stdin)["query"]
            calls.append("user" if "user(login" in q else "org")
            if "user(login" in q:
                return 0, json.dumps({"data": {"user": {"projectV2": None}}}), ""
            return 0, json.dumps({"data": {"organization": {"projectV2": {
                "id": "P", "title": "T", "url": "U",
                "field": {"id": "F", "options": [{"id": "o1", "name": "Ready"}]}}}}}), ""
        c, _ = client_for(m)
        p = c.resolve_project("", "acme", 3)
        assert calls == ["user", "org"]
        assert p.id == "P" and p.owner_kind == "orgs"
        assert p.status_options == {"Ready": "o1"}

    def test_resolve_project_scope_error_propagates(self):
        body = json.dumps({"errors": [{"type": "INSUFFICIENT_SCOPES", "message": "m"}]})
        c, _ = client_for(lambda a, s: (1, body, ""))
        with pytest.raises(GhError) as ei:
            c.resolve_project("users", "x", 1)
        assert ei.value.insufficient_scopes

    def test_set_item_status_mutation_variables(self):
        c, run = client_for(lambda a, s: (0, '{"data":{}}', ""))
        c.set_item_status("P", "I", "F", "O")
        v = json.loads(run.calls[0][1])["variables"]
        assert v == {"project": "P", "item": "I", "field": "F", "option": "O"}
