#!/usr/bin/env bash
# First boot of the platform VM, after install-tofu.sh: the forge (Gitea), the pipeline (Atlantis)
# and the policy engine (Conftest). It installs all three, creates the forge's identities and the
# change repository, and starts the pipeline last. platform.yaml.tftpl's runcmd runs it once, as root:
#   setup-platform.sh <gitea-version> <atlantis-version> <conftest-version> <forge-port>
# cloud-init logs what it prints, so it never prints a secret.
set -euo pipefail

gitea_version=$1
atlantis_version=$2
conftest_version=$3
forge_port=$4

# Written by cloud-init: the forge identities and their passwords (root's alone), and Atlantis's
# settings, of which this needs the pipeline's forge user, Atlantis's port and the webhook secret.
. /etc/agentic-iac/forge-users.env
. /etc/agentic-iac/atlantis.env

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Download a release asset and check it against the SHA-256 sums its project publishes beside it.
fetch() {  # fetch <release URL> <asset> <sums file>
  curl -fsSL --retry 5 -o "$work/$2" "$1/$2"
  curl -fsSL --retry 5 -o "$work/$3" "$1/$3"
  (cd "$work" && awk -v asset="$2" '$2 == asset' "$3" | sha256sum --check --strict)
}

# --- Binaries, into /usr/local/bin, on systemd's default PATH --------------------------------------

# https://github.com/go-gitea/gitea/releases/tag/v28.2.0 (as-of 2026-11-02)
fetch "https://github.com/go-gitea/gitea/releases/download/v$gitea_version" \
  "gitea-$gitea_version-linux-amd64" "gitea-$gitea_version-linux-amd64.sha256"
install -m 0755 "$work/gitea-$gitea_version-linux-amd64" /usr/local/bin/gitea

# https://github.com/runatlantis/atlantis/releases/tag/v0.49.0 (as-of 2026-11-02)
fetch "https://github.com/runatlantis/atlantis/releases/download/v$atlantis_version" \
  atlantis_linux_amd64.zip checksums.txt
unzip -q -o "$work/atlantis_linux_amd64.zip" atlantis -d "$work"
install -m 0755 "$work/atlantis" /usr/local/bin/atlantis

# Atlantis runs the conftest on its PATH: gate/repos.yaml sets no conftest_version, and nothing sets
# DEFAULT_CONFTEST_VERSION, so Atlantis downloads no other.
# https://github.com/open-policy-agent/conftest/releases/tag/v0.71.1,
# https://github.com/runatlantis/atlantis/blob/v0.49.0/server/core/runtime/policy/conftest_client.go (as-of 2026-11-02)
fetch "https://github.com/open-policy-agent/conftest/releases/download/v$conftest_version" \
  "conftest_${conftest_version}_Linux_x86_64.tar.gz" checksums.txt
tar -xzf "$work/conftest_${conftest_version}_Linux_x86_64.tar.gz" -C "$work" conftest
install -m 0755 "$work/conftest" /usr/local/bin/conftest

# --- The forge ------------------------------------------------------------------------------------

# ROOT_URL is the forge's public address, so the links in its pull requests open from your browser.
# The module renders user data before the VM has an address, so first boot asks the metadata
# service: a token, then the network data as JSON, whose ipv4.public lists CIDRs like 192.0.2.1/32.
# https://techdocs.akamai.com/cloud-computing/docs/metadata-service-api (as-of 2026-11-02)
metadata_token=$(curl -fsS -X PUT -H "Metadata-Token-Expiry-Seconds: 300" http://169.254.169.254/v1/token)
public_ip=$(curl -fsS -H "Metadata-Token: $metadata_token" -H "Accept: application/json" \
  http://169.254.169.254/v1/network |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["ipv4"]["public"][0].split("/")[0])')

# Gitea refuses to run as root and saves the secrets it generates (INTERNAL_TOKEN, JWT_SECRET) into
# its config file, so the gitea user owns it. Webhooks may call only Atlantis, on this VM: strict
# mode admits only what is listed, and a listed host without a port covers only 80 and 443.
# https://github.com/go-gitea/gitea/blob/v28.2.0/custom/conf/app.example.ini,
# https://github.com/go-gitea/gitea/blob/v28.2.0/modules/setting/security.go,
# https://github.com/go-gitea/gitea/blob/v28.2.0/modules/egress/policy/matchlist.go (as-of 2026-11-02)
install -d -o gitea -g gitea -m 0750 /etc/gitea
cat > /etc/gitea/app.ini <<EOF
; Written at first boot by /opt/agentic-iac/platform/setup-platform.sh.
WORK_PATH = /var/lib/gitea

[server]
HTTP_PORT = $forge_port
ROOT_URL = http://$public_ip:$forge_port/
; Every link uses ROOT_URL. By default Gitea builds them from each request's Host, so the agent,
; which calls the forge at its private address, would get private links.
PUBLIC_URL_DETECTION = never

[database]
; One file under WORK_PATH. The release binary's SQLite driver is pure Go.
DB_TYPE = sqlite3

[security]
; No web installer: first boot creates the identities with the CLI.
INSTALL_LOCK = true
EGRESS_MODE = strict
ALLOWED_HOST_LIST = loopback:$ATLANTIS_PORT

[cron.update_checker]
; No calls home for new releases.
ENABLED = false
EOF
chown gitea:gitea /etc/gitea/app.ini

# Gitea's CLI, as the gitea user, against that config.
as_gitea() { runuser -u gitea -- /usr/local/bin/gitea --config /etc/gitea/app.ini "$@"; }

# The database, then the identities, all before the server first starts: migrate exists "so that you
# can run gitea admin create user before starting the server". The CLI takes a password only as an
# argument, visible to local processes for an instant, on a VM only root logs in to.
# https://github.com/go-gitea/gitea/blob/v28.2.0/cmd/migrate.go,
# https://github.com/go-gitea/gitea/blob/v28.2.0/cmd/admin_user_create.go (as-of 2026-11-02)
as_gitea migrate
add_user() {  # add_user <name> <password>
  as_gitea admin user create --username "$1" --password "$2" --email "$1@example.com" \
    --must-change-password=false
}
add_user "$FORGE_READER_USER" "$FORGE_READER_PASSWORD"      # you, the approver
add_user "$FORGE_AGENT_USER" "$FORGE_AGENT_PASSWORD"        # pushes its own branches; can't merge
add_user "$FORGE_PIPELINE_USER" "$FORGE_PIPELINE_PASSWORD"  # Atlantis: the only identity that merges

# The pipeline's forge token, with the scopes Atlantis documents for Gitea. It is minted with the
# other CLI steps, while the CLI is the database's only user; --raw prints the token alone, and it
# goes straight into Atlantis's environment file.
# https://github.com/go-gitea/gitea/blob/v28.2.0/cmd/admin_user_generate_access_token.go,
# https://github.com/runatlantis/atlantis/blob/v0.49.0/runatlantis.io/docs/access-credentials.md (as-of 2026-11-02)
forge_token=$(as_gitea admin user generate-access-token --username "$FORGE_PIPELINE_USER" \
  --token-name atlantis --scopes write:issue,write:repository,read:user --raw)
[[ $forge_token =~ ^[[:alnum:]_-]+$ ]] || { echo "gitea printed no token" >&2; exit 1; }
echo "ATLANTIS_GITEA_TOKEN=$forge_token" >> /etc/agentic-iac/atlantis.env

systemctl daemon-reload
systemctl enable --now gitea.service
curl -fsS --retry 30 --retry-delay 2 --retry-connrefused -o /dev/null "http://127.0.0.1:$forge_port/api/healthz"

# --- The change repository, seeded as you, the reader, through Gitea's API ------------------------
# https://github.com/go-gitea/gitea/blob/v28.2.0/routers/api/v1/api.go,
# https://github.com/go-gitea/gitea/tree/v28.2.0/modules/structs (as-of 2026-11-02)

# One API call as the reader, with the JSON body on stdin. The password reaches curl through a
# config read from a pipe, never through its arguments.
api() {  # api <method> <path> <<< <JSON body>
  curl -fsS -o /dev/null -X "$1" -H "Content-Type: application/json" --data-binary @- \
    -K <(printf 'user = "%s:%s"\n' "$FORGE_READER_USER" "$FORGE_READER_PASSWORD") \
    "http://127.0.0.1:$forge_port/api/v1$2"
}
repo=$FORGE_READER_USER/infra

# With a first commit on main, the branch every change plans atop.
api POST /user/repos <<< '{"name": "infra", "auto_init": true, "default_branch": "main"}'

# The agent pushes its own branches, and the pipeline comments and merges: both need write.
for user in "$FORGE_AGENT_USER" "$FORGE_PIPELINE_USER"; do
  api PUT "/repos/$repo/collaborators/$user" <<< '{"permission": "write"}'
done

# main takes no direct push, and only the pipeline merges, with no approvals required. Approval of a
# routed plan lives in the pipeline; Atlantis merges without forcing, so a required approval here
# would refuse the merge of every plan policy passed.
# https://github.com/runatlantis/atlantis/blob/v0.49.0/server/events/vcs/gitea/client.go (as-of 2026-10-30)
api POST "/repos/$repo/branch_protections" <<EOF
{"rule_name": "main", "enable_push": false, "required_approvals": 0,
 "enable_merge_whitelist": true, "merge_whitelist_usernames": ["$FORGE_PIPELINE_USER"]}
EOF

# The webhook to Atlantis on loopback, signed with the shared secret. A hook made through the API is
# inactive unless told otherwise, and pull_request also brings the pull requests' comments and syncs.
# https://github.com/go-gitea/gitea/blob/v28.2.0/routers/api/v1/utils/hook.go,
# https://github.com/runatlantis/atlantis/blob/v0.49.0/runatlantis.io/docs/configuring-webhooks.md (as-of 2026-11-02)
api POST "/repos/$repo/hooks" <<EOF
{"type": "gitea", "active": true, "events": ["push", "issue_comment", "pull_request"],
 "config": {"url": "http://127.0.0.1:$ATLANTIS_PORT/events", "content_type": "json",
            "secret": "$ATLANTIS_GITEA_WEBHOOK_SECRET"}}
EOF

# --- The pipeline, last: its token is in its environment, and the forge is seeded -----------------
systemctl enable --now atlantis.service
curl -fsS --retry 30 --retry-delay 2 --retry-connrefused -o /dev/null "http://127.0.0.1:$ATLANTIS_PORT/healthz"
echo "The forge is at http://$public_ip:$forge_port/ and the pipeline is running."
