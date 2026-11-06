"""The runner's reading of the pipeline and of the request: how it tells the verdict on a revised
commit from what it saw before the push, how it frees a lock as it stops, and what triage lets the
agent do.

These drive agent/runner.py's own functions against stand-ins for the forge's API and the model, so
no forge, pipeline or model runs. Importing the runner imports CrewAI, which takes some seconds.
"""

import io
import json
import time

import pytest

import agent.runner as runner

LOCKED_BY_1 = (
    "This project is currently locked by an unapplied plan from pull #1. To continue, delete the lock "
    "from #1 or apply that plan and merge the pull request."
)
# Atlantis v0.49.0's answer to an unlock that deleted a lock (server/events/unlock_command_runner.go).
UNLOCKED = "All Atlantis locks for this PR have been unlocked and plans discarded"


def numbered(policy):
    """What the pipeline leaves on each commit it plans and checks, latest per context, numbered as
    Gitea's API numbers them: from 1 on every commit (Gitea v28.2.0, services/convert/status.go)."""
    return [
        {"id": 3, "context": "atlantis/plan: shards/demo/default", "status": "success"},
        {"id": 4, "context": "atlantis/plan", "status": "success"},
        {"id": 6, "context": "atlantis/policy_check", "status": policy},
    ]


def comment(number, body, login=runner.PIPELINE_USER):
    return {"id": number, "user": {"login": login}, "body": body}


def change(number, head, state="open"):
    return {"number": number, "state": state, "merged": False, "head": {"sha": head}}


class Forge:
    """The forge API calls the runner makes, answered from memory: each change's state and head
    commit, each commit's statuses and each change's comments. `pipeline(number, body)` answers a
    comment the run posts, as Atlantis would."""

    def __init__(self, pulls, statuses, comments, pipeline=None):
        self.pulls, self.statuses, self.comments = pulls, statuses, comments
        self.pipeline = pipeline or (lambda number, body: None)
        self.writes = []  # every write the run made, in order: (method, path, body)

    def __call__(self, method, path, body=None):
        _, kind, key = path.split("/")[:3]
        if method == "GET":
            if kind == "pulls":
                return self.pulls[int(key)]
            if kind == "commits":
                return {"statuses": self.statuses.get(key, [])}
            if kind == "issues":
                return self.comments[int(key)]
        self.writes.append((method, path, body))
        if (method, kind) == ("POST", "issues"):
            self.say(int(key), body["body"], login="agent")
            self.pipeline(int(key), body["body"])
            return {}
        if (method, kind) == ("PATCH", "pulls"):
            self.pulls[int(key)]["state"] = body["state"]
            return {}
        raise AssertionError(f"unexpected forge call: {method} {path}")

    def say(self, number, body, login=runner.PIPELINE_USER):
        """A new comment on change #number, numbered after every comment so far."""
        newest = max((c["id"] for comments in self.comments.values() for c in comments), default=100)
        self.comments[number].append(comment(newest + 1, body, login))


def start(monkeypatch, forge, number, request="Add a firewall"):
    """Make this run run-abc123 on shards/demo, with change #number, against the stand-in forge, and
    shorten its waits, so a regression fails in a second, not after fifteen minutes."""
    run = runner.Run("run-abc123", "demo", request)
    run.number = number
    monkeypatch.setattr(runner, "forge", forge)
    monkeypatch.setattr(runner, "RUN", run)
    monkeypatch.setattr(runner, "EVENTS", io.StringIO())
    for name, seconds in [("POLL_SECONDS", 0.01), ("RETRY_SECONDS", 0.01), ("WAIT_SECONDS", 1)]:
        monkeypatch.setattr(runner, name, seconds)
    return run


def events():
    return [json.loads(line) for line in runner.EVENTS.getvalue().splitlines()]


def test_a_revision_is_judged_though_its_statuses_reuse_the_first_commits_numbers(monkeypatch):
    # The first commit: policy blocked it, and the runner saw that.
    forge = Forge(
        pulls={1: change(1, "first")},
        statuses={"first": numbered("failure")},
        comments={1: [comment(101, "Ran Plan for dir: shards/demo workspace: default"),
                      comment(102, 'BLOCK: linode_firewall.web rule "ssh" admits SSH (TCP 22) from 0.0.0.0/0')]},
    )
    run = start(monkeypatch, forge, number=1)

    # The revision's push: what the runner knew just before it, as propose_change records it.
    run.seen, run.acted_at, run.action = runner.snapshot(), time.monotonic(), "the push"
    _, statuses, _ = runner.look()
    assert runner.fresh(statuses, "atlantis/policy_check", run.seen) is None  # nothing new yet

    # The pipeline plans the revised commit and policy passes it: the same statuses, the same numbers.
    forge.pulls[1]["head"]["sha"] = "revised"
    forge.statuses["revised"] = numbered("success")
    forge.say(1, "Ran Plan for dir: shards/demo workspace: default")
    forge.say(1, "Ran Policy Check for dir: shards/demo workspace: default")

    result = runner.wait_for_pipeline.func()
    assert result.startswith("PASSED"), result
    assert forge.pulls[1]["state"] == "open"


def test_a_stopping_run_frees_its_lock_before_it_closes_its_change(monkeypatch):
    # Change #1's plan stands, though policy blocked it, so the change holds shards/demo's lock.
    def pipeline(number, body):
        if body == "atlantis unlock":
            forge.say(number, UNLOCKED)

    forge = Forge(pulls={1: change(1, "first")}, statuses={"first": numbered("failure")},
                  comments={1: []}, pipeline=pipeline)
    start(monkeypatch, forge, number=1)

    runner.close("the agent stopped after policy blocked the plan")

    assert [(method, body) for method, _, body in forge.writes] == [
        ("POST", {"body": "atlantis unlock"}),
        ("POST", {"body": "Closing this change: the agent stopped after policy blocked the plan. (Agent run run-abc123.)"}),
        ("PATCH", {"state": "closed"}),
    ]


def test_a_lock_its_closed_holder_kept_ends_the_wait_instead_of_looping(monkeypatch):
    # Change #1 planned, then was closed by hand, unmerged: Atlantis on Gitea keeps its lock, so the
    # pipeline refuses every plan of change #2 for it, as live run 2 saw.
    plans = []

    def pipeline(number, body):
        if (number, body) == (2, "atlantis plan"):
            refuse()

    def refuse():
        plans.append(len(plans) + 1)
        forge.statuses["second"].append({"id": len(plans), "context": "atlantis/plan", "status": "failure"})
        forge.say(2, LOCKED_BY_1)

    forge = Forge(
        pulls={1: change(1, "first", state="closed"), 2: change(2, "second")},
        statuses={"second": []},
        comments={1: [], 2: []},
        pipeline=pipeline,
    )
    run = start(monkeypatch, forge, number=2)
    run.seen, run.acted_at, run.action = runner.snapshot(), time.monotonic(), "the push"
    refuse()  # the autoplan of change #2's first commit

    result = runner.wait_for_pipeline.func()

    assert run.ended == ("change #1 is closed, yet still holds shards/demo's lock: comment `atlantis unlock` "
                         "on change #1, then ask again"), result
    assert len(plans) == 3  # the autoplan, then two asked for: not a loop
    assert forge.pulls[2]["state"] == "closed"
    assert all(path.startswith("/issues/2/") or path == "/pulls/2" for _, path, _ in forge.writes)


class Model:
    """A stand-in for the model: the one reply it gives, or the error it raises."""

    def __init__(self, reply):
        self.reply = reply

    def call(self, messages):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


SEVERAL = ["Create a NodeBalancer", "Create a 2GB Linode in its region", "Connect the Linode to it"]


@pytest.mark.parametrize("reply, sorted_as", [
    ('{"kind": "question", "changes": []}', ("question", [])),
    ('{"kind": "change", "changes": []}', ("change", [])),
    ("```json\n" + json.dumps({"kind": "several", "changes": SEVERAL}) + "\n```", ("several", SEVERAL)),
    ('{"kind": "several", "changes": ["Create a NodeBalancer"]}', ("change", [])),
    ('{"kind": "several", "changes": "Create a NodeBalancer, then a Linode"}', ("change", [])),
    ('{"kind": "delete everything", "changes": []}', ("unsorted", [])),
    ("It asks for a firewall.", ("unsorted", [])),
    (TimeoutError("the model did not answer"), ("unsorted", [])),
])
def test_triage_sorts_a_request_or_leaves_it_a_change(reply, sorted_as):
    assert runner.triage(Model(reply), "the reader's request") == sorted_as


def test_triage_can_only_narrow_the_agent():
    def names(kind):
        return {tool.name for tool in runner.TOOLS[kind]}

    assert names("question") == {"read_repo", "read_cloud"}
    assert names("unsorted") == names("change")
    assert names("question") < names("change")


def test_several_changes_end_the_run_before_any_change(monkeypatch):
    def nothing(method, path, body=None):
        raise AssertionError(f"the forge was called: {method} {path}")

    start(monkeypatch, nothing, number=None, request="Create a NodeBalancer, then a Linode, then connect them")

    assert runner.sort(Model(json.dumps({"kind": "several", "changes": SEVERAL}))) is None

    triaged, answer, done = events()
    assert triaged == {"kind": "triage", "sorted_as": "several",
                       "text": "Triage: 3 changes, so the run ends here: one change per request."}
    assert answer["kind"] == "answer" and all(f"{n}. {text}" in answer["text"] for n, text in enumerate(SEVERAL, 1))
    assert done == {"kind": "done", "outcome": "several changes", "text": "The agent proposed no change: one change per request."}
