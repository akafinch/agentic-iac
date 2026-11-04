"""The runner's reading of the pipeline: how it tells the verdict on a revised commit from what it saw
before the push.

These drive agent/runner.py's own wait_for_pipeline against a stand-in for the forge's API, so no
forge, pipeline or model runs. Importing the runner imports CrewAI, which takes some seconds.
"""

import time

import agent.runner as runner


def numbered(policy):
    """What the pipeline leaves on each commit it plans and checks, latest per context, numbered as
    Gitea's API numbers them: from 1 on every commit (Gitea v28.2.0, services/convert/status.go)."""
    return [
        {"id": 3, "context": "atlantis/plan: shards/demo/default", "status": "success"},
        {"id": 4, "context": "atlantis/plan", "status": "success"},
        {"id": 6, "context": "atlantis/policy_check", "status": policy},
    ]


def comment(number, body):
    return {"id": number, "user": {"login": runner.PIPELINE_USER}, "body": body}


class Forge:
    """The few API calls the runner makes on its change, #1, answered from memory."""

    def __init__(self, head, statuses, comments):
        self.head, self.statuses, self.comments, self.state = head, statuses, comments, "open"

    def __call__(self, method, path, body=None):
        if (method, path) == ("GET", "/pulls/1"):
            return {"number": 1, "state": self.state, "head": {"sha": self.head}}
        if method == "GET" and path.startswith("/commits/"):
            return {"statuses": self.statuses.get(path.split("/")[2], [])}
        if (method, path) == ("GET", "/issues/1/comments"):
            return self.comments
        if (method, path) == ("POST", "/issues/1/comments"):
            self.comments.append(comment(max(c["id"] for c in self.comments) + 1, body["body"]))
            return {}
        if (method, path) == ("PATCH", "/pulls/1"):
            self.state = body["state"]
            return {}
        raise AssertionError(f"unexpected forge call: {method} {path}")


def test_a_revision_is_judged_though_its_statuses_reuse_the_first_commits_numbers(monkeypatch):
    # The first commit: policy blocked it, and the runner saw that.
    forge = Forge(
        head="first",
        statuses={"first": numbered("failure")},
        comments=[comment(101, "Ran Plan for dir: shards/demo workspace: default"),
                  comment(102, 'BLOCK: linode_firewall.web rule "ssh" admits SSH (TCP 22) from 0.0.0.0/0')],
    )
    run = runner.Run("run-abc123", "demo", "Add a firewall")
    run.number = 1
    monkeypatch.setattr(runner, "forge", forge)
    monkeypatch.setattr(runner, "RUN", run)
    # A regression then fails in a second, not after fifteen minutes.
    monkeypatch.setattr(runner, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(runner, "WAIT_SECONDS", 1)

    # The revision's push: what the runner knew just before it, as propose_change records it.
    run.seen, run.acted_at, run.action = runner.snapshot(), time.monotonic(), "the push"
    _, statuses, _ = runner.look()
    assert runner.fresh(statuses, "atlantis/policy_check", run.seen) is None  # nothing new yet

    # The pipeline plans the revised commit and policy passes it: the same statuses, the same numbers.
    forge.head = "revised"
    forge.statuses["revised"] = numbered("success")
    forge.comments += [comment(103, "Ran Plan for dir: shards/demo workspace: default"),
                       comment(104, "Ran Policy Check for dir: shards/demo workspace: default")]

    result = runner.wait_for_pipeline.func()
    assert result.startswith("PASSED"), result
    assert forge.state == "open"
