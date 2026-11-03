locals {
  # Each VM's cloud-init is rendered from your checkout: the templates under platform/ embed the
  # gate/ and agent/ files their VM runs, read through repo_root.
  repo_root = abspath("${path.module}/../..")

  shared_vars = {
    repo_root               = local.repo_root
    forge_port              = local.forge_port
    web_port                = local.web_port
    platform_vpc_ip         = local.platform_vpc_ip
    agent_vpc_ip            = local.agent_vpc_ip
    opentofu_version        = var.opentofu_version
    linode_provider_version = var.linode_provider_version
  }

  platform_user_data = templatefile("${local.repo_root}/platform/platform.yaml.tftpl", merge(local.shared_vars, {
    reader_cidr             = var.reader_cidr
    vpc_subnet              = local.vpc_subnet
    forge_reader_user       = local.forge_users.reader
    forge_reader_password   = random_password.forge["reader"].result
    forge_agent_user        = local.forge_users.agent
    forge_agent_password    = random_password.forge["agent"].result
    forge_pipeline_user     = local.forge_users.pipeline
    forge_pipeline_password = random_password.forge["pipeline"].result
    webhook_secret          = random_password.webhook_secret.result
    pipeline_token          = linode_token.pipeline.token
    state_bucket            = linode_object_storage_bucket.state.label
    state_endpoint          = "https://${linode_object_storage_bucket.state.s3_endpoint}"
    state_access_key        = linode_object_storage_key.state.access_key
    state_secret_key        = linode_object_storage_key.state.secret_key
    gitea_version           = var.gitea_version
    atlantis_version        = var.atlantis_version
    conftest_version        = var.conftest_version
  }))

  # The agent VM holds only its own credentials: the inference key, the read-only token and the
  # agent's forge identity. No pipeline token, state key or other password is in this map.
  agent_user_data = templatefile("${local.repo_root}/platform/agent.yaml.tftpl", merge(local.shared_vars, {
    forge_url            = "http://${local.platform_vpc_ip}:${local.forge_port}"
    forge_agent_user     = local.forge_users.agent
    forge_agent_password = random_password.forge["agent"].result
    agent_token          = linode_token.agent.token
    inference_endpoint   = var.inference_endpoint
    inference_model      = var.inference_model
    inference_api_key    = var.inference_api_key
    python_libraries     = var.python_libraries
  }))

  # User data can't exceed 65,535 bytes once base64-encoded:
  # https://techdocs.akamai.com/linode-api/reference/post-linode-instance (user_data, as-of 2026-10-30)
  user_data_limit = 65535
}

# The platform VM: the forge, the pipeline and the policy engine, started by first boot. A changed
# user_data replaces the VM and wipes the forge
# (https://github.com/linode/terraform-provider-linode/blob/v4.9.0/linode/instance/schema_resource.go),
# so run the shard teardown before re-applying anything rendered into it, a policy included.
resource "linode_instance" "platform" {
  label           = "${var.name}-platform"
  region          = var.region
  type            = var.platform_type
  image           = var.image
  authorized_keys = var.authorized_keys
  firewall_id     = linode_firewall.this.id

  # Configuration-profile interfaces, named so the account's default interface generation can't
  # change what this module builds.
  interface_generation = "legacy_config"

  interface {
    purpose = "public"
  }

  interface {
    purpose   = "vpc"
    subnet_id = linode_vpc_subnet.this.id
    ipv4 {
      vpc = local.platform_vpc_ip
    }
  }

  metadata {
    user_data = base64encode(local.platform_user_data)
  }

  lifecycle {
    precondition {
      condition     = length(base64encode(local.platform_user_data)) <= local.user_data_limit
      error_message = "The platform VM's user data is over 65,535 bytes base64-encoded. Gzip the files it embeds."
    }
  }
}

# The agent VM: the web interface, its service and every agent run.
resource "linode_instance" "agent" {
  label           = "${var.name}-agent"
  region          = var.region
  type            = var.agent_type
  image           = var.image
  authorized_keys = var.authorized_keys
  firewall_id     = linode_firewall.this.id

  interface_generation = "legacy_config"

  interface {
    purpose = "public"
  }

  interface {
    purpose   = "vpc"
    subnet_id = linode_vpc_subnet.this.id
    ipv4 {
      vpc = local.agent_vpc_ip
    }
  }

  metadata {
    user_data = base64encode(local.agent_user_data)
  }

  lifecycle {
    precondition {
      condition     = length(base64encode(local.agent_user_data)) <= local.user_data_limit
      error_message = "The agent VM's user data is over 65,535 bytes base64-encoded. Gzip the files it embeds."
    }
  }
}

# Each VM's public IPv4, read back once the VM exists: the address your browser and SSH use.
data "linode_instance_networking" "platform" {
  linode_id = linode_instance.platform.id
}

data "linode_instance_networking" "agent" {
  linode_id = linode_instance.agent.id
}

locals {
  platform_ip = data.linode_instance_networking.platform.ipv4[0].public[0].address
  agent_ip    = data.linode_instance_networking.agent.ipv4[0].public[0].address
}

# Delete locks on both VMs. Making or lifting one takes an unrestricted user and account:read_write,
# which only your deploy token has, so no agent can lock or unlock a VM. Each lock depends on its VM,
# so destroy lifts it first.
# https://github.com/linode/terraform-provider-linode/blob/v4.9.0/docs/resources/lock.md,
# https://techdocs.akamai.com/linode-api/reference/post-resource-lock (as-of 2026-10-30)
resource "linode_lock" "vm" {
  provider = linode.beta
  for_each = {
    platform = linode_instance.platform.id
    agent    = linode_instance.agent.id
  }

  entity_id   = each.value
  entity_type = "linode"
  lock_type   = "cannot_delete"
}
