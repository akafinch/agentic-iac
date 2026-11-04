"""The gate's fixtures: each change passes, is refused before planning, is blocked or is routed.

These run the real gate/code-check.sh and the real policy sets through Conftest, the way the
pipeline runs them. Conftest must be on PATH (v0.71.1, the version the platform installs):
https://github.com/open-policy-agent/conftest/releases/tag/v0.71.1
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / "gate"
PLANS = Path(__file__).resolve().parent / "plans"
ADMIN_RANGE = "203.0.113.7/32"

FIREWALL = """\
resource "linode_firewall" "web" {
  label           = "web"
  inbound_policy  = "DROP"
  outbound_policy = "ACCEPT"

  inbound {
    label    = "https-from-anywhere"
    action   = "ACCEPT"
    protocol = "TCP"
    ports    = "443"
    ipv4     = ["0.0.0.0/0"]
    ipv6     = ["::/0"]
  }
}
"""

RUNS_A_PROGRAM = """\
data "external" "env" {
  program = ["sh", "-c", "env"]
}
"""


def require_conftest():
    # A missing Conftest fails the test, never skips it: without it nothing here has been checked.
    if shutil.which("conftest") is None:
        pytest.fail("conftest is not on PATH. Install Conftest v0.71.1 and put it on PATH to test the gate.")


def code_check(tmp_path, files):
    """Stage files in shards/x/ of a new git repository and run the code check there, as the pipeline does."""
    require_conftest()
    shard = tmp_path / "shards" / "x"
    shard.mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=env)
    for name, text in files.items():
        (shard / name).write_text(text)
    subprocess.run(["git", "add", "."], cwd=shard, check=True, env=env)
    return subprocess.run([str(GATE / "code-check.sh")], cwd=shard, env=env, capture_output=True, text=True)


def policy(policy_set, plan, admin_range=ADMIN_RANGE):
    """Run one policy set on a saved plan's JSON, as Atlantis runs it, with ADMIN_RANGE set or unset."""
    require_conftest()
    env = {k: v for k, v in os.environ.items() if k != "ADMIN_RANGE"}
    if admin_range is not None:
        env["ADMIN_RANGE"] = admin_range
    return subprocess.run(
        ["conftest", "test", "-p", str(GATE / "policy" / policy_set), str(PLANS / plan), "--no-color"],
        env=env, capture_output=True, text=True,
    )


# --- The code check, before any plan --------------------------------------------------------------


def test_an_addition_passes_the_code_check(tmp_path):
    result = code_check(tmp_path, {"firewall.tf": FIREWALL})
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_emptied_file_passes_the_code_check(tmp_path):
    # Removing resources empties their file; deleting it could drop the whole shard directory.
    result = code_check(tmp_path, {"firewall.tf": ""})
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("name", "text", "reason"),
    [
        ("env.tf", RUNS_A_PROGRAM, 'REFUSED: data "external"'),
        ("env.tofu", RUNS_A_PROGRAM, "REFUSED: env.tofu"),
        ("import.tf", 'import {\n  to = linode_instance.a\n  id = "123"\n}\n', 'REFUSED: block type "import"'),
        (
            "fileset.tf",
            'locals {\n  names = "%{ for f in fileset(".", "*") }${f} %{ endfor }"\n}\n',
            "reads files on the pipeline's machine",
        ),
        (
            "templatestring.tf",
            'locals {\n  rendered = templatestring(local.template, {})\n}\n',
            "templatestring",
        ),
    ],
    ids=["data-external", "tofu-file", "import-block", "fileset-directive", "templatestring"],
)
def test_code_that_runs_or_reads_beyond_the_change_is_refused(tmp_path, name, text, reason):
    result = code_check(tmp_path, {name: text})
    assert result.returncode != 0
    refusals = [line for line in result.stdout.splitlines() if "REFUSED:" in line]
    assert any(reason in line for line in refusals), result.stdout + result.stderr


# --- Policy, on the saved plan --------------------------------------------------------------------


def test_an_addition_passes_both_policy_sets():
    for policy_set in ("block", "route"):
        result = policy(policy_set, "addition.json")
        assert result.returncode == 0, f"{policy_set}: {result.stdout}{result.stderr}"


def test_world_open_ssh_is_blocked_naming_the_admin_range():
    blocked = policy("block", "world-open-ssh.json")
    assert blocked.returncode != 0
    reason = 'BLOCK: linode_firewall.web rule "ssh-from-anywhere" admits SSH (TCP 22) from 0.0.0.0/0'
    assert f"{reason}, outside the admin range {ADMIN_RANGE}." in blocked.stdout
    assert policy("route", "world-open-ssh.json").returncode == 0


def test_ssh_passes_from_any_of_several_admin_ranges_and_reasons_name_them_all():
    # Comma-separated, as the platform writes ADMIN_RANGE. The addition's SSH source, 203.0.113.7/32,
    # lies in the second range only.
    ranges = "198.51.100.0/24,203.0.113.0/24"
    assert policy("block", "addition.json", admin_range=ranges).returncode == 0
    blocked = policy("block", "world-open-ssh.json", admin_range=ranges)
    assert blocked.returncode != 0
    assert "from 0.0.0.0/0, outside the admin range 198.51.100.0/24, 203.0.113.0/24." in blocked.stdout


def test_a_destroy_fails_only_route():
    assert policy("block", "destroy.json").returncode == 0
    routed = policy("route", "destroy.json")
    assert routed.returncode != 0
    assert "ROUTE: linode_firewall.web will be destroyed" in routed.stdout


def test_without_an_admin_range_all_ssh_is_blocked():
    # Fail closed: the addition's SSH rule admits only what would be the admin range.
    blocked = policy("block", "addition.json", admin_range=None)
    assert blocked.returncode != 0
    assert 'BLOCK: linode_firewall.web rule "ssh-from-admin"' in blocked.stdout
    assert "the admin range is not set" in blocked.stdout
