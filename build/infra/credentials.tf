locals {
  # The API families the minted tokens cover. Neither token has account or object_storage scope:
  # without account scope no token can lift a delete lock, and without object_storage none can mint
  # a key to the state bucket. vpc has no read_only scope, so the agent's token leaves it out. Both
  # read events, which the provider polls to follow an instance's create, boot and delete.
  # https://techdocs.akamai.com/linode-api/reference/get-started#oauth-reference (as-of 2026-10-30);
  # https://github.com/linode/terraform-provider-linode/blob/v4.9.0/linode/instance/resource.go
  token_families  = ["domains", "firewall", "ips", "linodes", "nodebalancers", "volumes", "vpc"]
  pipeline_scopes = join(" ", concat([for f in local.token_families : "${f}:read_write"], ["events:read_only"]))
  agent_scopes    = join(" ", concat([for f in local.token_families : "${f}:read_only" if f != "vpc"], ["events:read_only"]))

  forge_users = {
    reader   = "reader"   # the approver: you, in a browser
    agent    = "agent"    # pushes its own branches; can't approve or merge
    pipeline = "pipeline" # Atlantis: the only identity that merges
  }
}

# Recorded once, at the first apply, so later plans don't move the tokens' expiry.
resource "time_offset" "token_expiry" {
  offset_days = var.token_lifetime_days
}

# Minted by the module, so `tofu destroy` revokes them. The pipeline's token writes and stays on the
# platform VM; the agent's only reads. Minting one takes account:read_write on the deploy token:
# https://techdocs.akamai.com/linode-api/reference/post-personal-access-token (as-of 2026-10-30)
resource "linode_token" "pipeline" {
  label  = "${var.name}-pipeline"
  scopes = local.pipeline_scopes
  expiry = time_offset.token_expiry.rfc3339
}

resource "linode_token" "agent" {
  label  = "${var.name}-agent"
  scopes = local.agent_scopes
  expiry = time_offset.token_expiry.rfc3339
}

# The forge identities' passwords and the webhook secret. They reach the VMs in user data and stay
# in local state, which never leaves your machine.
resource "random_password" "forge" {
  for_each = local.forge_users
  length   = 24
  special  = false
}

resource "random_password" "webhook_secret" {
  length  = 32
  special = false
}
