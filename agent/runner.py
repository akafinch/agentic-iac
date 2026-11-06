"""One agent run: a plain-language request becomes a change the pipeline plans, checks and applies.
First, one short model call sorts the request: a question gets an agent that can only read, and a
request for several changes ends there, listed, so each can come back as a request of its own.

agent/app.py starts this module as its own process for each request,
    python -m agent.runner <run-id> <shard>      (the request arrives on stdin)
and relays what it prints, one JSON event per line, to the page. The model holds no credential that
can change anything: its only write is propose_change, a commit on the run's own branch. The
pipeline (Atlantis, on the platform VM) plans that commit with its own credentials, policy judges the
saved plan, and only the pipeline applies it and merges the change. The shard is set at launch,
never by the model, and every wait happens inside a tool, so a waiting run calls no model.
"""

import base64
import json
import os
import re
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import namedtuple
from functools import wraps
from pathlib import Path

# CrewAI's anonymous telemetry and its tracing go off before CrewAI is imported, and the crew below
# keeps tracing and memory off, so a run reaches no host but the model's endpoint. This setup, with
# CrewAI 1.15.33 and GLM-5.3, was verified against Akamai Inference (beta) as of 2026-10-29.
# https://docs.crewai.com/en/telemetry
os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"
os.environ["CREWAI_TRACING_ENABLED"] = "false"

from crewai import LLM, Agent, Crew, Task  # noqa: E402
from crewai.events import LLMCallStartedEvent, crewai_event_bus  # noqa: E402
from crewai.tools import tool  # noqa: E402

MAIN = "main"  # the change repository's default branch
PIPELINE_USER = "pipeline"  # Atlantis's forge identity: only its comments carry the pipeline's verdicts
LINODE_API = "https://api.linode.com/v4"

MAX_REVISIONS = 2  # proposals after the first: verifier feedback helps once or not at all
POLL_SECONDS = 5
ANSWER_SECONDS = 2 * 60  # the pipeline must answer a push or an atlantis comment within this
WAIT_SECONDS = 15 * 60  # any one wait, from its start: behind another change, or for the reader's approval
RETRY_SECONDS = 30  # how long a command already running on the change gets to finish before asking again
CLIP = 6000  # characters of any one tool result the model reads

# What the pipeline says, in Atlantis v0.49.0's words:
# https://github.com/runatlantis/atlantis/blob/v0.49.0/server/events/project_locker.go,
# https://github.com/runatlantis/atlantis/blob/v0.49.0/server/events/working_dir_locker.go,
# https://github.com/runatlantis/atlantis/blob/v0.49.0/server/events/command_requirement_handler.go
LOCKED_BY = re.compile(r"locked by an unapplied plan from pull #(\d+)")
STILL_RUNNING = "Wait until the previous command is complete"
NOT_APPROVED = "All policies must pass for project before running apply"
VERDICT_LINE = re.compile(r"(?:BLOCK|ROUTE|REFUSED): .*")

# The pipeline's code check refuses these calls (gate/code/check.rego). OpenTofu evaluates function
# calls at validate too, which would run them on this VM, so validate refuses them first.
FILE_CALL = re.compile(r"[A-Za-z0-9_]*file[A-Za-z0-9_]*(\s|/\*[\s\S]*?\*/|(#|//)[^\n]*)*\(|templatestring")
FILENAME = re.compile(r"[a-z0-9][a-z0-9_-]*\.tf")
BAD_FILENAME = "{!r} is not a bare .tf file name like firewall.tf (lowercase letters, digits, - and _)."

# validate's stand-in for the pipeline's _pipeline.tf: the same provider requirement, served from this
# VM's local mirror (TF_CLI_CONFIG_FILE), and no backend, since the state stays with the pipeline.
# LINODE_PROVIDER_VERSION is the version both VMs' mirrors hold, set by the platform module.
PROVIDER = string.Template("""\
terraform {
  required_providers {
    linode = {
      source  = "linode/linode"
      version = "$version"
    }
  }
}
""")

# What the forge showed just before the run's last push or atlantis comment: the newest comment ID and
# the head commit's statuses, each known by its commit and its number there (look). IDs, not clocks,
# tell what came after: the two VMs' clocks differ.
Seen = namedtuple("Seen", "comment statuses")


class Run:
    """This agent run: what it was given at launch, and its change in the forge. The tools share it,
    under LOCK."""

    def __init__(self, run_id, shard, request):
        self.id = run_id
        self.request = request
        self.shard = f"shards/{shard}"  # set at launch, never by the model
        self.branch = f"agent/{run_id}"
        self.work = Path.home() / "runs" / run_id  # this run's scratch space (app.py makes it)
        self.kind = None  # what triage sorted the request as: question, change, several or unsorted
        self.number = None  # the change's pull request number, once opened
        self.url = None  # its html_url: the forge's public address, which the page links to
        self.files = {}  # file name -> content this run proposed, on top of the default branch
        self.revisions = 0
        self.verdict = None  # the pipeline's last verdict: passed, routed, blocked or failed
        self.verdict_sha = None  # the commit it judged
        self.seen = Seen(0, frozenset())
        self.acted_at = 0.0  # when the last push or atlantis comment went out, on this VM's clock
        self.action = ""  # which it was, for the reasons the run gives
        self.ended = None  # why the run is over, once it is


RUN = None  # set by main()
# CrewAI may run several tool calls from one model response in parallel threads, so every forge
# write, and every read of the run's state, holds this lock.
LOCK = threading.Lock()
VALIDATING = threading.Lock()  # validate's scratch directory serves one call at a time
EVENTS = sys.stdout  # main() points this at the stream app.py reads
EMITTING = threading.Lock()


def emit(kind, **fields):
    """Send one event to the page."""
    with EMITTING:
        EVENTS.write(json.dumps({"kind": kind, **fields}) + "\n")


def clip(text):
    return text if len(text) <= CLIP else text[:CLIP] + f"\n... ({len(text) - CLIP} characters cut)"


def forge(method, path, body=None):
    """One call to the forge's API as the agent's identity, over the private network. That identity
    can push its own branches but can neither approve nor merge. Gitea's API:
    https://github.com/go-gitea/gitea/blob/v28.2.0/templates/swagger/v1-swagger.generated.json"""
    login = base64.b64encode(f"{os.environ['FORGE_USER']}:{os.environ['FORGE_PASSWORD']}".encode()).decode()
    request = urllib.request.Request(
        f"{os.environ['FORGE_URL']}/api/v1/repos/{os.environ['FORGE_REPO']}{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Basic {login}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read()
    return json.loads(text) if text else None


def read_file(path, ref):
    """A file's text and blob SHA on a branch, or (None, None) when it isn't there."""
    try:
        entry = forge("GET", f"/contents/{path}?ref={ref}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None, None
        raise
    return base64.b64decode(entry["content"] or "").decode(), entry["sha"]


def shard_files():
    """The shard's .tf files as the pipeline will plan them: the default branch's, with this run's
    proposals on top, since the pipeline plans each change merged atop the default branch."""
    try:
        entries = forge("GET", f"/contents/{RUN.shard}?ref={MAIN}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        entries = []
    files = {
        entry["name"]: read_file(entry["path"], MAIN)[0]
        for entry in entries
        if entry["type"] == "file" and entry["name"].endswith(".tf")
    }
    with LOCK:
        return files | RUN.files


def look():
    """The change as the forge shows it now: the pull request, the latest status per context on its
    current head commit, and every comment on it. A status's ID is its number on its own commit,
    counted from 1, and the pipeline posts the same statuses on every commit, so each status is
    known by its commit and its number together (its "key").
    https://github.com/go-gitea/gitea/blob/v28.2.0/services/convert/status.go,
    https://github.com/go-gitea/gitea/blob/v28.2.0/models/git/commit_status.go (as-of 2026-11-04)"""
    pull = forge("GET", f"/pulls/{RUN.number}")
    sha = pull["head"]["sha"]
    combined = forge("GET", f"/commits/{sha}/status")
    statuses = {status["context"]: status | {"key": (sha, status["id"])} for status in combined.get("statuses") or []}
    return pull, statuses, forge("GET", f"/issues/{RUN.number}/comments")


def snapshot():
    if RUN.number is None:
        return Seen(0, frozenset())
    _, statuses, comments = look()
    return Seen(max((c["id"] for c in comments), default=0), frozenset(s["key"] for s in statuses.values()))


def fresh(statuses, context, seen):
    """A context's latest status on the head commit, if it came after the last push or comment."""
    status = statuses.get(context)
    return status if status and status["key"] not in seen.statuses else None


def answers(comments, seen):
    """The pipeline's comments since the last push or comment."""
    return [c for c in comments if c["id"] > seen.comment and c["user"]["login"] == PIPELINE_USER]


def answered(statuses, comments, seen):
    """Whether the pipeline has said anything since: a new atlantis/* status or a new comment."""
    new_status = any(context.startswith("atlantis/") and fresh(statuses, context, seen) for context in statuses)
    return new_status or bool(answers(comments, seen))


def ask_pipeline(command):
    """Comment an Atlantis command on the change. Only these fixed commands, never the model's text."""
    with LOCK:
        seen = snapshot()
        forge("POST", f"/issues/{RUN.number}/comments", {"body": command})
        RUN.seen, RUN.acted_at, RUN.action = seen, time.monotonic(), f"the `{command}` comment"
    emit("note", text=f"Commented `{command}` on change #{RUN.number}.")


def over():
    return f"The run is over: {RUN.ended}. Give your final answer."


def close(reason):
    """End the run unmerged: free the change's lock, then close the change, saying why. On Gitea,
    Atlantis drops a lock by itself only when its change merges; closed unmerged, the change keeps
    it until an unlock. A change holds the lock while its latest plan stands, and then the pipeline
    answers the unlock (otherwise it stays silent), so the run waits for that answer: a change
    queued behind this one finds the lock gone once this one is closed.
    https://github.com/runatlantis/atlantis/blob/v0.49.0/server/events/event_parser.go,
    https://github.com/runatlantis/atlantis/blob/v0.49.0/server/events/unlock_command_runner.go (as-of 2026-11-05)"""
    with LOCK:
        if RUN.ended:
            return over()
        _, statuses, comments = look()
        seen = Seen(max((c["id"] for c in comments), default=0), frozenset())
        forge("POST", f"/issues/{RUN.number}/comments", {"body": "atlantis unlock"})
        if statuses.get("atlantis/plan", {}).get("status") == "success":
            waited = time.monotonic()
            while not answers(look()[2], seen) and time.monotonic() - waited < RETRY_SECONDS:
                time.sleep(POLL_SECONDS)
        body = f"Closing this change: {reason}. (Agent run {RUN.id}.)"
        forge("POST", f"/issues/{RUN.number}/comments", {"body": body})
        forge("PATCH", f"/pulls/{RUN.number}", {"state": "closed"})
        RUN.ended = reason
    emit("note", text=f"Closed change #{RUN.number}: {reason}.")
    return over()


def ended_in_forge(pull):
    """The change merged or was closed in the forge: either way, the run is over."""
    with LOCK:
        RUN.ended = RUN.ended or ("it merged" if pull.get("merged") else "the change was closed in the forge")
    return over()


def judged(verdict, pull, lines, said=""):
    """Record the pipeline's verdict on the head commit and tell the model what it allows."""
    with LOCK:
        RUN.verdict, RUN.verdict_sha = verdict, pull["head"]["sha"]
        left = MAX_REVISIONS - RUN.revisions
    revise = f"Revise the file with propose_change ({left} revisions left), or stop and say why."
    text = {
        "passed": "PASSED: the plan passed policy.\n"
        "Call request_apply: the pipeline applies exactly this saved plan.",
        "routed": f"ROUTED to a human by policy:\n{lines}\n"
        "Call request_apply: the pipeline applies this saved plan once the reader approves it.",
        "blocked": f"BLOCKED by policy:\n{lines}\n{revise}",
        "failed": f"FAILED before policy (the code check or the plan):\n{lines}\nThe pipeline said:\n{said}\n{revise}",
    }[verdict]
    return clip(text)


def wait_behind(number):
    """Wait, calling no model, until change #number, which holds the shard's lock, merges or closes."""
    emit("note", text=f"Waiting behind change #{number}, which holds {RUN.shard}'s lock. No model calls while waiting.")
    started = time.monotonic()
    while time.monotonic() - started < WAIT_SECONDS:
        own = forge("GET", f"/pulls/{RUN.number}")
        if own["state"] == "closed":
            ended_in_forge(own)
            return False
        holder = forge("GET", f"/pulls/{number}")
        if holder["state"] == "closed":
            ending = "merged" if holder.get("merged") else "closed"
            emit("note", text=f"Change #{number} {ending}: asking for a new plan.")
            time.sleep(POLL_SECONDS)  # time for the pipeline to drop that change's lock
            return True
        time.sleep(POLL_SECONDS)
    close(f"it waited 15 minutes behind pull request #{number}, which neither merged nor closed")
    return False


def wait_for_approval(seen):
    """Wait, calling no model, until the reader's approval passes atlantis/policy_check."""
    emit(
        "note",
        text=f"Waiting for the reader to approve the routed plan: in the forge, comment `atlantis approve_policies` "
        f"on change #{RUN.number}. No model calls while waiting.",
    )
    started = time.monotonic()
    while time.monotonic() - started < WAIT_SECONDS:
        pull, statuses, _ = look()
        if pull["state"] == "closed":
            ended_in_forge(pull)
            return False
        check = fresh(statuses, "atlantis/policy_check", seen)
        if check and check["status"] == "success":
            emit("note", text="The reader approved the routed plan.")
            return True
        time.sleep(POLL_SECONDS)
    close("it waited 15 minutes for the reader's approval")
    return False


def logged(function):
    """Stream each tool call and its result to the page. An error goes back to the model as text."""

    @wraps(function)
    def run(**args):
        emit("call", tool=function.__name__, args=args)
        try:
            result = function(**args)
        except urllib.error.HTTPError as error:
            result = f"Error: HTTP {error.code} from {error.url}: {error.read().decode(errors='replace')[:500]}"
        except Exception as error:
            result = f"Error: {error}"
        emit("result", tool=function.__name__, text=result)
        return result

    return run


# --- The tools: the model's whole reach -----------------------------------------------------------
# ASSUMPTION (unverified): GLM-5.3 on Akamai Inference calls the tools that take no arguments
# (read_repo, wait_for_pipeline, request_apply). The setup was verified with a three-argument tool only.


@tool
@logged
def read_repo() -> str:
    """Read your shard: every .tf file in it, as the pipeline will plan them (the default branch, with
    your own proposals on top)."""
    files = shard_files()
    if not files:
        return f"{RUN.shard}/ is empty: nothing has been merged into it yet."
    return clip("\n\n".join(f"# {RUN.shard}/{name}\n{text}" for name, text in sorted(files.items())))


@tool
@logged
def read_cloud(path: str) -> str:
    """Read the Linode account with a read-only token: GET one Linode API v4 path, such as
    /linode/instances or /networking/firewalls. Returns the JSON, cut short when long."""
    # https://techdocs.akamai.com/linode-api/reference/get-linode-instances
    if not path.startswith("/"):
        return "The path must start with /, like /linode/instances."
    token = os.environ["LINODE_TOKEN"]
    request = urllib.request.Request(LINODE_API + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return clip(response.read().decode())


@tool
@logged
def validate(filename: str, content: str) -> str:
    """Check one complete .tf file offline before you propose it: your shard, with this file in place,
    goes through `tofu init -backend=false` and `tofu validate`. Nothing is planned or applied."""
    if not FILENAME.fullmatch(filename):
        return BAD_FILENAME.format(filename)
    files = shard_files() | {filename: content}
    if any(FILE_CALL.search(text) for text in files.values()):
        return 'REFUSED: a function whose name contains "file", or templatestring. The pipeline refuses them too.'
    # The model's HCL runs here with none of this VM's credentials in its environment, and -get=false
    # fetches no module from anywhere:
    # https://github.com/opentofu/opentofu/blob/v1.13.2/website/docs/cli/commands/init.mdx
    env = {"PATH": os.environ["PATH"], "HOME": str(RUN.work), "TF_IN_AUTOMATION": "1"}
    if "TF_CLI_CONFIG_FILE" in os.environ:
        env["TF_CLI_CONFIG_FILE"] = os.environ["TF_CLI_CONFIG_FILE"]
    commands = [
        ["tofu", "init", "-backend=false", "-get=false", "-input=false", "-no-color"],
        ["tofu", "validate", "-no-color"],
    ]
    scratch = RUN.work / "validate"
    with VALIDATING:
        scratch.mkdir(parents=True, exist_ok=True)
        for old in scratch.glob("*.tf"):
            old.unlink()
        for name, text in files.items():
            (scratch / name).write_text(text)
        (scratch / "_pipeline.tf").write_text(PROVIDER.substitute(version=os.environ["LINODE_PROVIDER_VERSION"]))
        for command in commands:
            done = subprocess.run(command, cwd=scratch, env=env, capture_output=True, text=True, timeout=300)
            if done.returncode != 0:
                return clip(f"tofu {command[1]} failed:\n{done.stdout}{done.stderr}")
    return f"{filename} is valid in {RUN.shard}/. Propose it with propose_change."


@tool
@logged
def propose_change(filename: str, content: str, rationale: str) -> str:
    """Commit one complete .tf file to your shard, on your own branch: your only way to change anything.
    The first call opens a change in the forge, which the pipeline then plans; each later call is a
    revision, and you have two. Empty content empties the file, which removes its resources. The
    rationale is shown to the reader here, never in the forge."""
    if not FILENAME.fullmatch(filename):
        return BAD_FILENAME.format(filename)
    path = f"{RUN.shard}/{filename}"
    with LOCK:
        if RUN.ended:
            return over()
        if RUN.number is not None and RUN.revisions >= MAX_REVISIONS:
            return "You have no revisions left. Stop and say why the change could not pass."
        current, sha = read_file(path, RUN.branch if RUN.number is not None else MAIN)
        if current == content:
            return f"{path} already holds exactly this content: nothing was committed."
        seen = snapshot()
        # PUT creates the file, or replaces it given its blob SHA. An empty file stays a file: deleting
        # a shard's last file would drop its directory, and the pipeline would plan nothing. The
        # commit message is fixed, so the forge carries no model text.
        commit = {"content": base64.b64encode(content.encode()).decode(), "message": f"Agent run {RUN.id}: {path}"}
        if sha:
            commit["sha"] = sha
        if RUN.number is None:
            commit |= {"branch": MAIN, "new_branch": RUN.branch}
        else:
            commit["branch"] = RUN.branch
        forge("PUT", f"/contents/{path}", commit)
        RUN.files[filename] = content
        opened = RUN.number is None
        if opened:
            # The change's only text is the reader's request: an approver reads policy's summary and
            # the plan, never the model's account of them.
            title = f"Agent run {RUN.id}: {RUN.shard}"
            pull = forge("POST", "/pulls", {"base": MAIN, "head": RUN.branch, "title": title, "body": RUN.request})
            RUN.number, RUN.url = pull["number"], pull["html_url"]
            result = f"Opened change #{RUN.number} with {path}."
        else:
            RUN.revisions += 1
            result = f"Committed {path} to change #{RUN.number}, revision {RUN.revisions} of {MAX_REVISIONS}."
        RUN.seen, RUN.acted_at, RUN.action = seen, time.monotonic(), "the push"
        RUN.verdict = None
    if opened:
        emit("change", number=RUN.number, url=RUN.url)
    return result + " The pipeline plans it now: call wait_for_pipeline."


@tool
@logged
def wait_for_pipeline() -> str:
    """Wait for the pipeline's verdict on your change's latest commit: the plan passed policy, policy
    blocked it or routed it to a human, or the code check or the plan failed. If another change holds
    your shard's lock, this waits for it without calling you, then asks for a new plan."""
    if RUN.number is None:
        return "There is no change yet: call propose_change first."
    started = time.monotonic()
    replans = {}  # new plans asked for since each holder of the lock merged or closed
    while True:
        if time.monotonic() - started > WAIT_SECONDS:
            return close("it waited 15 minutes for the pipeline's verdict")
        with LOCK:  # so a push in flight lands first
            if RUN.ended:
                return over()
            seen, acted_at, action = RUN.seen, RUN.acted_at, RUN.action
        pull, statuses, comments = look()
        if pull["state"] == "closed":
            return ended_in_forge(pull)
        new = answers(comments, seen)
        said = "\n\n".join(c["body"] for c in new)
        lines = "\n".join(dict.fromkeys(VERDICT_LINE.findall(said)))
        plan = fresh(statuses, "atlantis/plan", seen)
        check = fresh(statuses, "atlantis/policy_check", seen)
        if plan and plan["status"] in ("failure", "error") and new:
            holder = LOCKED_BY.search(said)
            if holder:  # another change's unapplied plan holds the shard
                number = int(holder.group(1))
                tries = replans.get(number, 0)
                if tries == 0 and not wait_behind(number):
                    return over()
                if tries == 2:  # a change closed by hand, unmerged, keeps its lock
                    return close(
                        f"change #{number} is closed, yet still holds {RUN.shard}'s lock: comment "
                        f"`atlantis unlock` on change #{number}, then ask again"
                    )
                if tries == 1:
                    time.sleep(RETRY_SECONDS)  # a pipeline slow to drop the lock gets one more chance
                replans[number] = tries + 1
                ask_pipeline("atlantis plan")
                started = time.monotonic()
                continue
            if STILL_RUNNING in said:  # the pipeline is still busy with an earlier command on this change
                time.sleep(RETRY_SECONDS)
                ask_pipeline("atlantis plan")
                continue
            return judged("failed", pull, lines, said)
        if plan and plan["status"] == "success" and check and check["status"] != "pending":
            if check["status"] == "success":
                return judged("passed", pull, "")
            if "BLOCK:" in lines:
                return judged("blocked", pull, lines)
            if "ROUTE:" in lines:
                return judged("routed", pull, lines)
            return judged("failed", pull, lines, said)
        if not answered(statuses, comments, seen) and time.monotonic() - acted_at > ANSWER_SECONDS:
            return close(f"the pipeline did not answer within 2 minutes of {action}")
        time.sleep(POLL_SECONDS)


@tool
@logged
def request_apply() -> str:
    """Ask the pipeline to apply the plan it saved for your change, once wait_for_pipeline reported it
    passed or routed. The pipeline applies only that saved plan, then merges the change. A routed plan
    waits, without calling you, until the reader approves it."""
    with LOCK:
        if RUN.ended:
            return over()
        verdict, judged_sha = RUN.verdict, RUN.verdict_sha
    if verdict not in ("passed", "routed"):
        return "Only a plan that passed policy or was routed can be applied. Call wait_for_pipeline, or revise."
    if look()[0]["head"]["sha"] != judged_sha:
        return "Your change has a commit newer than the last verdict: call wait_for_pipeline."
    ask_pipeline("atlantis apply")
    started = time.monotonic()
    while True:
        if time.monotonic() - started > WAIT_SECONDS:
            return close("it waited 15 minutes for the pipeline to apply and merge the change")
        with LOCK:
            if RUN.ended:
                return over()
            seen, acted_at, action = RUN.seen, RUN.acted_at, RUN.action
        pull, statuses, comments = look()
        if pull.get("merged"):
            ended_in_forge(pull)
            return f"APPLIED: the pipeline applied the saved plan and merged change #{RUN.number}."
        if pull["state"] == "closed":
            return ended_in_forge(pull)
        new = answers(comments, seen)
        said = "\n\n".join(c["body"] for c in new)
        applied = fresh(statuses, "atlantis/apply", seen)
        if applied and applied["status"] in ("failure", "error") and new:
            if NOT_APPROVED in said and verdict == "routed":
                # The pipeline refuses until the reader approves this saved plan; then ask again.
                if not wait_for_approval(seen):
                    return over()
                ask_pipeline("atlantis apply")
                started = time.monotonic()
                continue
            if STILL_RUNNING in said:
                time.sleep(RETRY_SECONDS)
                ask_pipeline("atlantis apply")
                continue
            return clip(f"The pipeline did not apply it. It said:\n{said}")
        if "Automerging failed" in said:
            return clip(f"The pipeline applied the plan but could not merge the change. It said:\n{said}")
        if not answered(statuses, comments, seen) and time.monotonic() - acted_at > ANSWER_SECONDS:
            return close(f"the pipeline did not answer within 2 minutes of {action}")
        time.sleep(POLL_SECONDS)


# --- Triage: what the request asks for, sorted before any agent starts ----------------------------
# The code, not the model, sets what the agent may do with what triage says, and triage can only
# narrow it or end the run: a question gets the read tools alone, so its run opens no change, and
# several changes end the run before any code, so each gets its own plan, verdict and approval. A
# reply triage can't read leaves the run a change, as it was before triage.

TRIAGE = """You sort one request to an infrastructure agent before the agent starts. Reply with \
JSON only: {"kind": "question" or "change" or "several", "changes": [...]}.
- "question": the request only asks to be told something about the infrastructure or its code.
- "change": it asks for one change, however many resources that takes.
- "several": it asks for separate changes, one after another ("then", "after that", "also", a list).
For "several", list each change in order, in the request's own words. Otherwise "changes" is []."""

TOOLS = {
    "question": [read_repo, read_cloud],
    "change": [read_repo, read_cloud, validate, propose_change, wait_for_pipeline, request_apply],
}
TOOLS["unsorted"] = TOOLS["change"]

SORTED = {
    "question": "a question. The agent gets only read_repo and read_cloud, so this run can change nothing.",
    "change": "one change. The agent gets its tools for proposing it.",
    "unsorted": "no answer it could read, so the run goes ahead as one change.",
    "several": "{count} changes, so the run ends here: one change per request.",
}


def triage(llm, request):
    """Sort the request with one model call: ("question", []), ("change", []), ("several", the
    changes it lists), or ("unsorted", []) when the call fails or its reply can't be read."""
    try:
        reply = str(llm.call([{"role": "system", "content": TRIAGE}, {"role": "user", "content": request}]))
        found = json.loads(re.search(r"\{.*\}", reply, re.DOTALL).group())  # fenced or bare
        kind, listed = found["kind"], found.get("changes", [])
    except Exception:  # the call failed, or the reply holds no JSON object with a kind
        return "unsorted", []
    if not isinstance(listed, list):
        listed = []
    changes = [text.strip() for text in listed if isinstance(text, str) and text.strip()]
    if kind == "question":
        return "question", []
    if kind == "several" and len(changes) > 1:
        return "several", changes
    if kind in ("change", "several"):  # "several" listing one change is one change
        return "change", []
    return "unsorted", []


def sort(llm):
    """Triage the request and show the result on the page. Returns the agent's tools, or None when
    the run ends here, its changes listed, since it asked for several."""
    RUN.kind, changes = triage(llm, RUN.request)
    emit("triage", sorted_as=RUN.kind, text="Triage: " + SORTED[RUN.kind].format(count=len(changes)))
    if RUN.kind != "several":
        return TOOLS[RUN.kind]
    listed = "\n".join(f"{number}. {change}" for number, change in enumerate(changes, 1))
    emit(
        "answer",
        text=f"This request asks for {len(changes)} changes:\n{listed}\nSend them one at a time: each "
        "gets its own plan, policy verdict and approval, and each builds on what the one before merged.",
    )
    emit("done", outcome="several changes", text="The agent proposed no change: one change per request.")
    return None


# --- The agent ------------------------------------------------------------------------------------

RULES = """You hold no credential that can change anything. Your only write is propose_change: a \
commit on your own branch, which opens a change in the forge. The pipeline plans it with its own \
credentials, policy judges the saved plan, and only the pipeline applies it.

Before any plan, the pipeline's code check refuses a file that:
- declares any block but resource, data, variable, output or locals (the pipeline supplies the \
provider, linode/linode {version}, and the state);
- declares a resource or data type that doesn't start with linode_;
- has a provisioner, or sets file_path on linode_image or source on linode_object_storage_object;
- calls any function whose name contains "file" (file, templatefile, filebase64, fileset...) or \
templatestring."""

TASK = """The reader asks: {request}

Your shard is {shard}/, and file names are bare, like firewall.tf. See what it holds with \
read_repo, and what the account holds with read_cloud if you need to. Put the change in one file, \
check it with validate, propose it with propose_change, then call wait_for_pipeline. If the \
pipeline refuses or blocks it, revise that file (at most two revisions) or stop and say why. If the \
plan passed, or policy routed it to a human, call request_apply. To remove resources, propose their \
file with empty content."""

# A question's run: the same agent, holding the read tools alone.
READ_ONLY = """You hold no credential that can change anything, and this run gives you no tool that \
writes: you read the repository and, with a read-only token, the account."""

QUESTION = """The reader asks: {request}

This run answers a question and changes nothing. read_repo shows what your shard {shard}/ holds, \
and read_cloud reads the account through the Linode API, at paths such as /linode/instances or \
/networking/firewalls. Answer from what they return. If the reader asks for a change instead, say \
that this run could not make one."""

STOPPED = {
    None: "the agent stopped before the pipeline gave a verdict",
    "failed": "the agent stopped after the code check or the plan failed",
    "blocked": "the agent stopped after policy blocked the plan",
    "passed": "the agent stopped before the pipeline applied the plan",
    "routed": "the agent stopped before the pipeline applied the plan",
}


def count_model_call(source, event):
    emit("model")


def finish():
    """A run that ends unmerged frees the shard's lock and closes its change, saying why."""
    if RUN.number is None and RUN.kind == "question":
        return {"outcome": "answered", "text": "The agent answered, and proposed no change."}
    if RUN.number is None:
        return {"outcome": "nothing proposed", "text": "The agent proposed no change."}
    pull = forge("GET", f"/pulls/{RUN.number}")
    if pull.get("merged"):
        return {"outcome": "merged", "text": f"The pipeline applied change #{RUN.number} and merged it."}
    if pull["state"] != "closed":
        close(STOPPED[RUN.verdict])
    return {"outcome": "closed", "text": f"Change #{RUN.number} is closed: {RUN.ended or 'closed in the forge'}."}


def main():
    global RUN, EVENTS
    # app.py reads the events from stdout as this process started. Anything else printed to stdout,
    # CrewAI's console output included, goes to stderr instead: the service's log.
    EVENTS = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    RUN = Run(sys.argv[1], sys.argv[2], sys.stdin.read().strip())
    crewai_event_bus.on(LLMCallStartedEvent)(count_model_call)  # the page counts model calls, triage's too
    # Akamai Inference's OpenAI-compatible endpoint: https://docs.crewai.com/en/concepts/llms
    llm = LLM(
        model=os.environ["INFERENCE_MODEL"],
        custom_openai=True,
        base_url=os.environ["INFERENCE_ENDPOINT"],
        api_key=os.environ["INFERENCE_API_KEY"],
        max_tokens=4096,
        timeout=120,
    )

    emit("start", run=RUN.id, shard=RUN.shard, request=RUN.request)
    tools = sort(llm)
    if tools is None:
        return
    question = RUN.kind == "question"
    agent = Agent(
        role="Infrastructure agent",
        goal="Answer the reader's question about their Linode infrastructure from what you read."
        if question
        else f"Make the reader's change to their Linode infrastructure by proposing OpenTofu code in {RUN.shard}/.",
        backstory=READ_ONLY if question else RULES.format(version=os.environ["LINODE_PROVIDER_VERSION"]),
        tools=tools,
        llm=llm,
        max_iter=12,  # bounds the model calls one request can make
        max_retry_limit=0,  # a failure is reported as it happened, not retried
        allow_delegation=False,
    )
    task = Task(
        description=(QUESTION if question else TASK).format(request=RUN.request, shard=RUN.shard),
        expected_output="Two or three sentences for the reader: the answer, and which read it came from."
        if question
        else "Two or three sentences for the reader: what you proposed, the pipeline's verdict, "
        "and whether it was applied.",
        agent=agent,
    )
    # CrewAI runs a tool inline with no time limit, and an agent's max_execution_time defaults to
    # none, so a tool may wait as long as it must:
    # https://github.com/crewAIInc/crewAI/blob/1.15.33/lib/crewai/src/crewai/agents/agent_executor.py,
    # https://github.com/crewAIInc/crewAI/blob/1.15.33/lib/crewai/src/crewai/agent/core.py
    # Its tool-result cache is opt-in; it stays off, since a polling tool must never return a stale result.
    crew = Crew(agents=[agent], tasks=[task], memory=False, tracing=False, cache=False)

    try:
        answer = str(crew.kickoff().raw).strip()
    except Exception as error:  # the model call or CrewAI failed: say so, then close the change below
        answer = f"The agent stopped: {error}"
    emit("answer", text=answer)
    try:
        ending = finish()
    except Exception as error:  # the forge is unreachable: the change, if any, stays open there
        ending = {"outcome": "failed", "text": f"The run could not close its change: {error}"}
    emit("done", **ending)


if __name__ == "__main__":
    main()
