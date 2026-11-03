#!/usr/bin/env bash
# First boot, on both VMs: OpenTofu, and a local mirror holding the one provider a change may use.
# Each template's runcmd runs it once, as root:
#   install-tofu.sh <opentofu-version> <linode-provider-version>
# The pipeline plans and applies with this tofu, and the agent's validate tool checks HCL with it.
# Both install providers only from the mirror, through the CLI config /opt/agentic-iac/tofu.tfrc
# (TF_CLI_CONFIG_FILE), so a plan downloads nothing and a change naming any other provider fails at
# init.
set -euo pipefail

version=$1
provider_version=$2

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Download a release asset and check it against the SHA-256 sums its project publishes beside it.
fetch() {  # fetch <release URL> <asset> <sums file>
  curl -fsSL --retry 5 -o "$work/$2" "$1/$2"
  curl -fsSL --retry 5 -o "$work/$3" "$1/$3"
  (cd "$work" && awk -v asset="$2" '$2 == asset' "$3" | sha256sum --check --strict)
}

# Into /usr/local/bin, which is on systemd's default PATH for every service.
# https://github.com/opentofu/opentofu/releases/tag/v1.13.2 (as-of 2026-11-02)
fetch "https://github.com/opentofu/opentofu/releases/download/v$version" \
  "tofu_${version}_linux_amd64.tar.gz" "tofu_${version}_SHA256SUMS"
tar -xzf "$work/tofu_${version}_linux_amd64.tar.gz" -C "$work" tofu
install -m 0755 "$work/tofu" /usr/local/bin/tofu

# The mirror: `tofu providers mirror` fetches the providers a configuration requires, checking each
# package's signature as `tofu init` does, into a directory laid out for a filesystem mirror. This
# configuration requires only the Linode provider, at the version the pipeline's _pipeline.tf names.
# https://github.com/opentofu/opentofu/blob/v1.13.2/website/docs/cli/commands/providers/mirror.mdx (as-of 2026-11-02)
mkdir "$work/mirror"
cat > "$work/mirror/versions.tf" <<EOF
terraform {
  required_providers {
    linode = {
      source  = "linode/linode"
      version = "$provider_version"
    }
  }
}
EOF
(cd "$work/mirror" && tofu providers mirror /opt/agentic-iac/providers)

# Explicit provider_installation with only this mirror, and no direct block: OpenTofu installs the
# Linode provider from it and nothing from anywhere else.
# https://github.com/opentofu/opentofu/blob/v1.13.2/website/docs/cli/config/config-file.mdx (as-of 2026-11-02)
cat > /opt/agentic-iac/tofu.tfrc <<'EOF'
provider_installation {
  filesystem_mirror {
    path    = "/opt/agentic-iac/providers"
    include = ["registry.opentofu.org/linode/linode"]
  }
}
EOF
