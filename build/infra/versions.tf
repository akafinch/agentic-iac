# The platform module. You apply it once, by hand, with OpenTofu: the guardrails can't be deployed
# by the agent they guard. The HCL is engine-neutral, and its state stays local on your machine.
terraform {
  required_version = ">= 1.13.2"

  required_providers {
    # Akamai Cloud. Resource attributes checked against the provider's docs and source at v4.9.0:
    # https://github.com/linode/terraform-provider-linode/tree/v4.9.0/docs/resources (as-of 2026-10-30)
    linode = {
      source  = "linode/linode"
      version = "~> 4.9"
    }
    # Open source, MPL-2.0: https://github.com/hashicorp/terraform-provider-random (v3.9.1, as-of 2026-10-30)
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
    # Open source, MPL-2.0: https://github.com/hashicorp/terraform-provider-time (v0.14.2, as-of 2026-10-30)
    time = {
      source  = "hashicorp/time"
      version = "~> 0.14"
    }
  }
}

# The deploy token comes from LINODE_TOKEN in your environment and is never written to a file.
provider "linode" {}

# Resource locks are an early-access API, served only under v4beta, so the locks alone go through
# this alias and everything else stays on the stable v4 API. Early-access features may not be
# offered to every account in every region.
# https://techdocs.akamai.com/linode-api/reference/post-resource-lock,
# https://github.com/linode/terraform-provider-linode/blob/v4.9.0/docs/index.md#early-access (as-of 2026-10-30)
provider "linode" {
  alias       = "beta"
  api_version = "v4beta"
}
