"""The platform module and both VMs' first-boot user data, checked without deploying anything.

Only `tofu plan` runs here, never apply. OpenTofu 1.13.2 must be on PATH:
https://github.com/opentofu/opentofu/releases/tag/v1.13.2
The module's providers download from registry.opentofu.org, checked against
build/infra/.terraform.lock.hcl. Each tofu run gets a scrubbed environment, so no credential or CLI
config of yours reaches it.
"""

import base64
import gzip
import json
import os
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest
import yaml  # PyYAML, which CrewAI depends on

REPO = Path(__file__).resolve().parents[2]

# User data can't exceed 65,535 bytes once base64-encoded:
# https://techdocs.akamai.com/linode-api/reference/post-linode-instance (user_data, as-of 2026-11-02)
USER_DATA_LIMIT = 65535

# Two of your ranges, as terraform.tfvars lists them (documentation ranges, RFC 5737).
READER_CIDRS = ["203.0.113.7/32", "198.51.100.0/24"]


def tofu(workdir, home, *args):
    """Run tofu with nothing from this environment but PATH: a temporary HOME and a dummy token."""
    # A missing tofu fails the test, never skips it: without it nothing here has been checked.
    if shutil.which("tofu") is None:
        pytest.fail("tofu is not on PATH. Install OpenTofu 1.13.2 and put it on PATH to test the platform.")
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "LINODE_TOKEN": "placeholder-not-a-token"}
    result = subprocess.run(["tofu", *args], cwd=workdir, env=env, capture_output=True, text=True)
    assert result.returncode == 0, f"tofu {args[0]} failed:\n{result.stdout}{result.stderr}"
    return result.stdout


def checkout(tmp_path):
    """The module and everything it renders, in the checkout's layout, since the module reads
    platform/, gate/ and agent/ two levels above build/infra/. No local state, plans, variables or
    provider downloads come along."""
    root = tmp_path / "checkout"
    ignore = shutil.ignore_patterns(".terraform", "terraform.tfvars", "*.tfstate*", "*.tfplan", "__pycache__")
    for part in ("build/infra", "platform", "gate", "agent"):
        shutil.copytree(REPO / part, root / part, ignore=ignore)
    # What running the service leaves behind: byte code, which the agent's user data must skip.
    (root / "agent" / "__pycache__").mkdir()
    (root / "agent" / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\xcb\r\r\n\x00\xff")
    return root


# --- Criterion 1: the plan is the inventory ---------------------------------------------------------


def test_the_plan_is_the_inventory(tmp_path):
    """Two VMs, a firewall admitting each of your ranges, a VPC and subnet, the bucket and its key,
    two locks, and two tokens that expire, neither with account or Object Storage scope and the
    agent's read-only; from Akamai's provider and two open-source ones only."""
    module = checkout(tmp_path) / "build" / "infra"
    home = tmp_path / "home"
    home.mkdir()
    tofu(module, home, "init", "-input=false", "-no-color")
    tofu(
        module, home, "plan", "-input=false", "-no-color", "-out=tfplan",
        "-var", f"reader_cidrs={json.dumps(READER_CIDRS)}",
        "-var", 'authorized_keys=["ssh-ed25519 AAAA... you@example.com"]',
        "-var", "inference_api_key=placeholder",
    )
    plan = json.loads(tofu(module, home, "show", "-json", "tfplan"))

    managed = [change for change in plan["resource_changes"] if change["mode"] == "managed"]
    assert all(change["change"]["actions"] == ["create"] for change in managed)
    assert Counter(change["type"] for change in managed if change["type"].startswith("linode_")) == {
        "linode_instance": 2,
        "linode_firewall": 1,
        "linode_vpc": 1,
        "linode_vpc_subnet": 1,
        "linode_object_storage_bucket": 1,
        "linode_object_storage_key": 1,
        "linode_lock": 2,
        "linode_token": 2,
    }

    firewall = next(change["change"]["after"] for change in managed if change["type"] == "linode_firewall")
    sources = {rule["label"]: rule["ipv4"] for rule in firewall["inbound"]}
    assert [sources[f"{name}-from-reader"] for name in ("ssh", "forge", "web")] == [READER_CIDRS] * 3, sources

    tokens = {change["name"]: change["change"] for change in managed if change["type"] == "linode_token"}
    assert set(tokens) == {"pipeline", "agent"}
    for name, change in tokens.items():
        # The expiry is known at plan, or set at apply from the offset the first apply records.
        assert change["after"].get("expiry") or change["after_unknown"].get("expiry") is True, name
        scopes = change["after"]["scopes"].split()
        families = {scope.split(":")[0] for scope in scopes}
        assert "*" not in scopes and not families & {"account", "object_storage"}, (name, scopes)
    assert all(scope.endswith(":read_only") for scope in tokens["agent"]["after"]["scopes"].split())

    providers = {change["provider_name"] for change in plan["resource_changes"]}
    providers |= {provider["full_name"] for provider in plan["configuration"]["provider_config"].values()}
    assert providers == {
        "registry.opentofu.org/linode/linode",
        "registry.opentofu.org/hashicorp/random",
        "registry.opentofu.org/hashicorp/time",
    }


# --- Criterion 2: each VM holds only its own secrets, and nothing that ships holds one -------------


def sentinel(tag, length):
    """A stand-in secret as long as the real one, so a rendering is at least as large as on deploy."""
    return tag + "0" * (length - len(tag))


# Every secret the module renders into user data, by template variable.
SECRETS = {
    "pipeline_token": sentinel("pipelinetoken", 64),  # Linode personal access tokens: 64 characters
    "agent_token": sentinel("agenttoken", 64),
    "forge_reader_password": sentinel("readerpassword", 24),  # random_password.forge: 24
    "forge_agent_password": sentinel("agentpassword", 24),
    "forge_pipeline_password": sentinel("pipelinepassword", 24),
    "webhook_secret": sentinel("webhooksecret", 32),  # random_password.webhook_secret: 32
    "state_access_key": sentinel("stateaccesskey", 20),  # Object Storage keys: 20 and 40
    "state_secret_key": sentinel("statesecretkey", 40),
    "inference_api_key": sentinel("inferencekey", 200),  # generous: no published length
}
AGENT_SECRETS = {"inference_api_key", "agent_token", "forge_agent_password"}


def secrets_in(text):
    return {name for name, value in SECRETS.items() if value in text}


def template_variables(repo_root):
    """Every variable either template receives from build/infra/compute.tf, with realistic values."""
    pinned = json.loads((REPO / "build" / "infra" / "adp.auto.tfvars.json").read_text())
    return SECRETS | {
        "repo_root": str(repo_root),
        "forge_port": 3000,  # build/infra/network.tf
        "web_port": 8000,
        "platform_vpc_ip": "10.0.0.10",
        "agent_vpc_ip": "10.0.0.11",
        "vpc_subnet": "10.0.0.0/24",
        "forge_url": "http://10.0.0.10:3000",
        "reader_cidrs": READER_CIDRS,
        "forge_reader_user": "reader",
        "forge_agent_user": "agent",
        "forge_pipeline_user": "pipeline",
        "state_bucket": "agentic-iac-state-a1b2c3",  # the name, then random_id's 6 hex characters
        "state_endpoint": "https://us-iad-10.linodeobjects.com",
        "linode_provider_version": "4.9.0",  # build/infra/variables.tf
        **{
            name: pinned[name]
            for name in (
                "opentofu_version", "gitea_version", "atlantis_version", "conftest_version",
                "inference_endpoint", "inference_model", "python_libraries",
            )
        },
    }


def render(tmp_path, repo_root):
    """Both templates through OpenTofu's templatefile(), each given every variable either VM's map
    holds, so a template that reached for the other VM's secret would render it here: the outputs of
    a scratch module, read from its plan."""
    scratch = tmp_path / "render"
    scratch.mkdir()
    (scratch / "terraform.tfvars.json").write_text(json.dumps({"vars": template_variables(repo_root)}))
    outputs = {
        name: {"value": f'${{templatefile("{repo_root}/platform/{name}.yaml.tftpl", var.vars)}}'}
        for name in ("platform", "agent")
    }
    (scratch / "main.tf.json").write_text(json.dumps({"variable": {"vars": {}}, "output": outputs}))
    home = tmp_path / "home"
    home.mkdir()
    tofu(scratch, home, "init", "-input=false", "-no-color")
    tofu(scratch, home, "plan", "-input=false", "-no-color", "-out=tfplan")
    planned = json.loads(tofu(scratch, home, "show", "-json", "tfplan"))["planned_values"]["outputs"]
    return {name: planned[name]["value"] for name in outputs}


def unpack(rendering):
    """The cloud-config, and each file it writes, decoded: path -> (permissions, text)."""
    config = yaml.safe_load(rendering)
    files = {}
    for entry in config["write_files"]:
        text = entry["content"]
        if entry.get("encoding") == "gz+b64":
            text = gzip.decompress(base64.b64decode(text)).decode()
        files[entry["path"]] = (entry["permissions"], text)
    return config, files


def test_each_vm_holds_only_its_own_secrets_within_the_user_data_cap(tmp_path):
    """The agent VM's user data holds only the inference key, its read-only token and its forge
    password; the platform's holds neither of the first two. Each fits the user-data cap: the
    module's own size check can't fire at plan, because the real renderings interpolate tokens and
    keys minted at apply, so this checks sentinel renderings at least as large."""
    renderings = render(tmp_path, checkout(tmp_path))
    found = {}
    for name, rendering in renderings.items():
        size = len(base64.b64encode(rendering.encode()))
        assert size <= USER_DATA_LIMIT, f"{name}: {size} bytes base64-encoded"
        config, files = unpack(rendering)
        assert not [path for path in files if "__pycache__" in path], name
        # Secrets sit only in 0400 files, each read by one service or by root, never on a command line.
        for path, (permissions, text) in files.items():
            if secrets_in(text):
                assert permissions == "0400", path
        assert not secrets_in(json.dumps({key: value for key, value in config.items() if key != "write_files"}))
        found[name] = secrets_in(rendering + "".join(text for _, text in files.values()))
    assert found["agent"] == AGENT_SECRETS
    assert not found["platform"] & {"inference_api_key", "agent_token"}


# What may not ship. The two key patterns are assembled, so this file, which the scan reads too, never
# holds them literally.
PRIVATE_KEY = re.compile("-----BEGIN [A-Z0-9 ]*PRIV" + "ATE KEY-----")
INFERENCE_KEY = re.compile("sk" + "_aka_" + r"[A-Za-z0-9_-]{16,}")  # Akamai Inference user keys
# A Linode personal access token: 64 hex characters. Lock files hold digests of the same shape,
# labelled zh: or sha256:, and those are not secrets.
LINODE_TOKEN = re.compile(r"(?<![0-9a-f])(?<!zh:)(?<!sha256:)[0-9a-f]{64}(?![0-9a-f])")
STATE = re.compile(r'"lineage"\s*:')  # every state file records its lineage
# A credential written in literally: a name that says token, password, secret or key, given a value
# of random letters and digits, rather than a reference (var.x, ${x}, $X) or a placeholder's words.
# The name's possessive \w++ never backtracks, so the scan stays linear on long runs of letters.
ASSIGNMENT = re.compile(r"\b(\w++)[\"']?\s*[=:]\s*[\"']?([A-Za-z0-9+/_-]{16,})")
CREDENTIAL_NAME = re.compile(r"token|password|secret|api_key|access_key", re.IGNORECASE)


def test_nothing_that_ships_holds_a_secret():
    """No file git would ship holds state, terraform.tfvars, a private or Akamai Inference key, or a
    literal credential. Only this repository's ignore rules apply, never your own."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO, env=env, capture_output=True, check=True,
    ).stdout.decode().split("\0")
    problems = []
    for name in filter(None, listed):
        path = REPO / name
        if path.name == "terraform.tfvars" or ".tfstate" in path.name:
            problems.append(f"{name}: state or variables")
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        for what, pattern in [("a private key", PRIVATE_KEY), ("an inference key", INFERENCE_KEY),
                              ("a Linode token", LINODE_TOKEN), ("state", STATE)]:
            if pattern.search(text):
                problems.append(f"{name}: {what}")
        for key, value in ASSIGNMENT.findall(text):
            if CREDENTIAL_NAME.search(key) and re.search(r"[0-9]", value) and re.search(r"[A-Za-z]", value):
                problems.append(f"{name}: a literal credential for {key}")
    assert not problems, "\n".join(problems)


# --- Your ranges, from the firewall to policy -------------------------------------------------------


def test_policy_gets_each_of_your_ranges(tmp_path):
    """Atlantis's environment names every one of your ranges as the admin range, comma-separated,
    the form the block policy splits."""
    _, files = unpack(render(tmp_path, checkout(tmp_path))["platform"])
    _, env = files["/etc/agentic-iac/atlantis.env"]
    assert f"ADMIN_RANGE={','.join(READER_CIDRS)}" in env.splitlines()
