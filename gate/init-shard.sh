#!/usr/bin/env bash
# The first step of every plan (gate/repos.yaml), run by Atlantis in a shard directory of the
# change's checkout, in place of its own `init`. The shard teardown runs it too, in a clone of the
# default branch, before `tofu destroy`.
#
# A shard is a directory shards/<name>/ with one state, shards/<name>/terraform.tfstate in the
# state bucket, and one lock: Atlantis locks per directory and workspace, OpenTofu per state.
# https://github.com/runatlantis/atlantis/blob/v0.49.0/runatlantis.io/docs/locking.md
set -euo pipefail

gate=$(cd "$(dirname "$0")" && pwd)

# Plans run only in shards/<name>/, one level down. Names are lowercase letters, digits and dashes,
# the form the web service gives new shards and run IDs.
prefix=$(git rev-parse --show-prefix)
if [[ ! $prefix =~ ^shards/([a-z0-9][a-z0-9-]*)/$ ]]; then
  echo "REFUSED: ${prefix:-the repository root} is not a shard. Plans run only in shards/<name>/, named with lowercase letters, digits and dashes."
  exit 1
fi
name=${BASH_REMATCH[1]}

# One state per shard: another workspace would be a second state beside the locked one. Atlantis
# sets WORKSPACE for its run steps.
# https://github.com/runatlantis/atlantis/blob/v0.49.0/runatlantis.io/docs/custom-workflows.md
if [[ ${WORKSPACE:-default} != default ]]; then
  echo "REFUSED: workspace '$WORKSPACE'. Each shard has one state, in the default workspace."
  exit 1
fi

"$gate/code-check.sh"

# The provider and the state backend are the pipeline's, never the change's. This backend was
# verified on an E3 bucket with OpenTofu 1.13.2 on 2026-10-29: use_lockfile takes the lock with a
# conditional write, which E3 enforces. LINODE_PROVIDER_VERSION is the version the platform's local
# provider mirror holds, set by the platform module.
# https://github.com/opentofu/opentofu/blob/v1.13.2/website/docs/language/settings/backends/s3.mdx
: "${STATE_BUCKET:?must name the state bucket}" "${STATE_ENDPOINT:?must be the state bucket endpoint URL}"
: "${LINODE_PROVIDER_VERSION:?must be the provider version the local mirror holds}"
cat > _pipeline.tf <<EOF
# Written by the pipeline at plan time (gate/init-shard.sh). A change may not declare this file.
terraform {
  required_providers {
    linode = {
      source  = "linode/linode"
      version = "$LINODE_PROVIDER_VERSION"
    }
  }

  backend "s3" {
    bucket                      = "$STATE_BUCKET"
    key                         = "shards/$name/terraform.tfstate"
    region                      = "us"
    endpoints                   = { s3 = "$STATE_ENDPOINT" }
    use_lockfile                = true
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
  }
}
EOF

# Quiet unless it fails, so the plan's comment shows the plan. The bucket keys come from
# AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in the pipeline's environment.
if ! output=$(tofu init -input=false -no-color 2>&1); then
  echo "$output"
  exit 1
fi
