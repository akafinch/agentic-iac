#!/usr/bin/env bash
# The pre-plan code check, run in a shard directory of a change's checkout (init-shard.sh calls it
# before `tofu init`). Planning executes the change's code with the pipeline's credentials, so this
# refuses, with REFUSED: reasons, anything that could run a program or read beyond the change.
#
# 1. Files. OpenTofu loads more than .tf files: .tofu, .tf.json and .tofu.json too, and Atlantis
#    also reads env/<workspace>.tfvars. Only regular .tf files at the shard's root pass, and never
#    _pipeline.tf, which the pipeline writes itself.
#    https://github.com/opentofu/opentofu/blob/v1.13.2/website/docs/language/files/index.mdx
#    https://github.com/runatlantis/atlantis/blob/v0.49.0/server/core/runtime/plan_step_runner.go
# 2. Contents. Each .tf file goes through gate/code/check.rego with Conftest's HCL2 parser, named on
#    the command line, so nothing depends on how Conftest would walk a directory.
#    https://github.com/open-policy-agent/conftest/blob/v0.71.1/parser/parser.go
set -euo pipefail

gate=$(cd "$(dirname "$0")" && pwd)

# What git tracks here, at any depth: "<mode> <object> <stage>\t<path>", paths relative to this
# directory. Git quotes a path holding unusual characters, so such a path never ends in .tf.
# https://git-scm.com/docs/git-ls-files
listing=$(git ls-files --stage .)

tf_files=()
refused=0
while IFS= read -r entry; do
  [[ -n $entry ]] || continue
  mode=${entry%% *}
  path=${entry#*$'\t'}
  if [[ $path == */* || $path != *.tf || $path == _pipeline.tf ]] || [[ $mode != 100644 && $mode != 100755 ]]; then
    echo "REFUSED: $path. A change may hold only regular .tf files at the shard's root, and never _pipeline.tf."
    refused=1
  else
    tf_files+=("./$path")
  fi
done <<< "$listing"

if (( refused )); then
  exit 1
fi

# Conftest refuses an empty file list, and an empty shard has nothing to check.
if (( ${#tf_files[@]} )); then
  conftest test --no-color --parser hcl2 -p "$gate/code" "${tf_files[@]}"
fi
