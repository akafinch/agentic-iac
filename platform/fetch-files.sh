#!/usr/bin/env bash
# First boot's first step, on both VMs: fetch each file the VM runs from the files bucket, where the
# module puts it from your checkout, and check it against the SHA-256 this VM's user data lists.
# Each template's runcmd runs it once, as root:
#   fetch-files.sh <env file> <manifest> <destination>
# The env file names the bucket and holds this VM's own read-only key to it (FILES_ENDPOINT,
# FILES_BUCKET, FILES_ACCESS_KEY, FILES_SECRET_KEY). The manifest has one line per file: its mode,
# its SHA-256 and its path, which is the same in your checkout, in the bucket and under the
# destination. Nothing is installed until every file matches. User data can't carry the files
# themselves: the API refuses more than 16,384 bytes of it, decoded.
set -euo pipefail

. "$1"
manifest=$2
dest=$3

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT

while read -r _ sha256 path; do
  mkdir -p "$stage/$(dirname "$path")"
  # Path-style, signed with the key (SigV4, region "us"), as teardown-shards.sh asks the state
  # bucket. The key reaches curl through a config read from a pipe, never through its arguments. A
  # new key can take 30 seconds to work on an E3 endpoint, so every error is retried for a minute.
  # https://github.com/linode/terraform-provider-linode/blob/v4.9.0/linode/obj/helpers.go (as-of 2026-11-04)
  curl -fsS --retry 12 --retry-delay 5 --retry-all-errors --aws-sigv4 "aws:amz:us:s3" \
    -K <(printf 'user = "%s:%s"\n' "$FILES_ACCESS_KEY" "$FILES_SECRET_KEY") \
    -o "$stage/$path" "$FILES_ENDPOINT/$FILES_BUCKET/$path"
  echo "$sha256  $stage/$path" | sha256sum --check --quiet
done < "$manifest"

while read -r mode _ path; do
  install -D -m "$mode" "$stage/$path" "$dest/$path"
done < "$manifest"
echo "Fetched $(wc -l < "$manifest") files, each matching its SHA-256."
