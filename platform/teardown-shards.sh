#!/usr/bin/env bash
# The shard teardown: destroys everything the agents built, shard by shard, then empties the state
# bucket. Run it once, as root, over SSH, with no agent run in flight, before `tofu destroy` of the
# platform:
#   ssh root@<platform_ip> /opt/agentic-iac/platform/teardown-shards.sh
# It runs on this VM because only the pipeline holds the read-write token and the bucket keys, so the
# pipeline's credentials stay the only ones that change what the agents built. Each shard goes
# through the gate's own init-shard.sh, so its destroy uses the provider and state backend every
# apply used.
set -euo pipefail

# The pipeline stops first, so no plan or apply starts while the shards go. It stays stopped: the
# platform itself goes next.
systemctl stop atlantis.service

# Atlantis's environment: the read-write token, the bucket and its keys, the mirror's CLI config and
# the provider version, exported to every command below.
set -a
. /etc/agentic-iac/atlantis.env
set +a

# One request to the state bucket: path-style, signed with its keys (SigV4, region "us"), the form
# verified against an E3 bucket with curl 8.5.0 on 2026-10-29. The keys reach curl through a config
# read from a pipe, never through its arguments.
s3() {  # s3 <method> <path and query>
  curl -fsS -X "$1" --aws-sigv4 "aws:amz:us:s3" \
    -K <(printf 'user = "%s:%s"\n' "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY") \
    "$STATE_ENDPOINT/$STATE_BUCKET$2"
}

# Every key in the bucket, one per line (ListObjectsV2). A page holds 1,000 keys, far more than a demo
# writes; the check at the end catches any left over.
keys() {
  s3 GET "?list-type=2" | python3 -c '
import sys
import xml.etree.ElementTree as ElementTree
for element in ElementTree.parse(sys.stdin).iter():
    if element.tag.rsplit("}", 1)[-1] == "Key":
        print(element.text)'
}

# The shards that have a state: shards/<name>/terraform.tfstate (gate/init-shard.sh).
shards=$(keys | sed -n 's|^shards/\([a-z0-9][a-z0-9-]*\)/terraform\.tfstate$|\1|p')

# The change repository's default branch, cloned as the pipeline. Its forge token reaches git through
# a credential helper that reads the environment, never through the URL or the arguments.
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git -c credential.helper='!f() { echo "username=$ATLANTIS_GITEA_USER"; echo "password=$ATLANTIS_GITEA_TOKEN"; }; f' \
  clone --quiet --branch main "$ATLANTIS_GITEA_BASE_URL/reader/infra.git" "$work/infra"

for name in $shards; do
  echo "Destroying shards/$name"
  # A shard whose change never merged has no directory on main. Empty, it still destroys everything
  # its state holds: a destroy plans away every resource in the state, whatever the configuration.
  # https://github.com/opentofu/opentofu/blob/v1.13.2/website/docs/language/resources/behavior.mdx (as-of 2026-10-30)
  mkdir -p "$work/infra/shards/$name"
  (
    cd "$work/infra/shards/$name"
    /opt/agentic-iac/gate/init-shard.sh
    tofu destroy -auto-approve -input=false -no-color
  )
done

# Then every object: the shard states, now empty, and anything else. The API deletes only an empty
# bucket, and the platform's tofu destroy deletes this one.
for key in $(keys); do
  s3 DELETE "/$key" > /dev/null
done

left=$(keys)
if [[ -n $left ]]; then
  echo "The state bucket still holds objects; run this again:" >&2
  echo "$left" >&2
  exit 1
fi
echo "Every shard is destroyed and the state bucket is empty. Next, on your machine: tofu destroy"
