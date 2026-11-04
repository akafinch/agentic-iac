locals {
  # Each VM's cloud-init is rendered from your checkout, read through repo_root: the templates under
  # platform/ list the files their VM runs, and storage.tf puts those files in the files bucket.
  repo_root = abspath("${path.module}/../..")

  # The files each VM runs, by their path in your checkout, with each one's mode on the VM. User data
  # can't hold them: the API refuses more than 16,384 bytes of it, decoded ("decoded user_data must
  # not exceed 16384 bytes", on create, as-of 2026-11-04). So each goes to the files bucket under its
  # path, each VM's user data lists their SHA-256s, and first boot fetches and checks them into
  # /opt/agentic-iac (platform/fetch-files.sh).
  platform_files = merge(
    # The gate, verbatim, which Atlantis reads and can't change. Its scripts are executable: the plan
    # workflow runs init-shard.sh by path.
    { for name in fileset(local.repo_root, "gate/**") : name => endswith(name, ".sh") ? "0755" : "0644" },
    # First boot's scripts, and the shard teardown, which only root may run.
    {
      "platform/install-tofu.sh"    = "0755"
      "platform/setup-platform.sh"  = "0755"
      "platform/teardown-shards.sh" = "0700"
    },
  )
  # The service, verbatim and read-only to it, and the OpenTofu its validate tool runs. __pycache__/
  # is skipped: Python rebuilds it, and its byte code isn't stable from one checkout to the next.
  # https://docs.python.org/3.12/tutorial/modules.html#compiled-python-files (as-of 2026-10-30)
  agent_files = merge(
    { for name in fileset(local.repo_root, "agent/**") : name => "0644" if !strcontains("/${name}", "/__pycache__/") },
    { "platform/install-tofu.sh" = "0755" },
  )

  shared_vars = {
    repo_root               = local.repo_root
    forge_port              = local.forge_port
    web_port                = local.web_port
    platform_vpc_ip         = local.platform_vpc_ip
    agent_vpc_ip            = local.agent_vpc_ip
    opentofu_version        = var.opentofu_version
    linode_provider_version = var.linode_provider_version
    files_bucket            = linode_object_storage_bucket.files.label
    files_endpoint          = "https://${linode_object_storage_bucket.files.s3_endpoint}"
  }

  platform_user_data = templatefile("${local.repo_root}/platform/platform.yaml.tftpl", merge(local.shared_vars, {
    platform_files            = local.platform_files
    platform_files_access_key = linode_object_storage_key.files_reader["platform"].access_key
    platform_files_secret_key = linode_object_storage_key.files_reader["platform"].secret_key
    reader_cidrs              = var.reader_cidrs
    vpc_subnet                = local.vpc_subnet
    forge_reader_user         = local.forge_users.reader
    forge_reader_password     = random_password.forge["reader"].result
    forge_agent_user          = local.forge_users.agent
    forge_agent_password      = random_password.forge["agent"].result
    forge_pipeline_user       = local.forge_users.pipeline
    forge_pipeline_password   = random_password.forge["pipeline"].result
    webhook_secret            = random_password.webhook_secret.result
    pipeline_token            = linode_token.pipeline.token
    state_bucket              = linode_object_storage_bucket.state.label
    state_endpoint            = "https://${linode_object_storage_bucket.state.s3_endpoint}"
    state_access_key          = linode_object_storage_key.state.access_key
    state_secret_key          = linode_object_storage_key.state.secret_key
    gitea_version             = var.gitea_version
    atlantis_version          = var.atlantis_version
    conftest_version          = var.conftest_version
  }))

  # The agent VM holds only its own credentials: the inference key, the read-only token, its
  # read-only key to the files bucket and the agent's forge identity. No pipeline token, state key or
  # other password is in this map.
  agent_user_data = templatefile("${local.repo_root}/platform/agent.yaml.tftpl", merge(local.shared_vars, {
    agent_files            = local.agent_files
    agent_files_access_key = linode_object_storage_key.files_reader["agent"].access_key
    agent_files_secret_key = linode_object_storage_key.files_reader["agent"].secret_key
    forge_url              = "http://${local.platform_vpc_ip}:${local.forge_port}"
    forge_agent_user       = local.forge_users.agent
    forge_agent_password   = random_password.forge["agent"].result
    agent_token            = linode_token.agent.token
    inference_endpoint     = var.inference_endpoint
    inference_model        = var.inference_model
    inference_api_key      = var.inference_api_key
    python_libraries       = var.python_libraries
  }))

  # Decoded, user data can't exceed 16,384 bytes (above). Base64 holds six bits in every character
  # but its padding, so this counts bytes, not characters.
  user_data_limit = 16384
  user_data_bytes = {
    for name, text in { platform = local.platform_user_data, agent = local.agent_user_data } :
    name => floor(length(replace(base64encode(text), "=", "")) * 3 / 4)
  }
}

# The platform VM: the forge, the pipeline and the policy engine, started by first boot. A changed
# user_data replaces the VM and wipes the forge
# (https://github.com/linode/terraform-provider-linode/blob/v4.9.0/linode/instance/schema_resource.go),
# so run the shard teardown before re-applying a change to any file it runs, a policy included, or
# to anything rendered into it.
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

  # First boot fetches the files this VM runs, so they're in the bucket before the VM exists.
  depends_on = [linode_object_storage_object.file]

  lifecycle {
    precondition {
      condition     = local.user_data_bytes.platform <= local.user_data_limit
      error_message = "The platform VM's user data is over 16,384 bytes. A file it runs belongs in local.platform_files, not in its template."
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

  depends_on = [linode_object_storage_object.file]

  lifecycle {
    precondition {
      condition     = local.user_data_bytes.agent <= local.user_data_limit
      error_message = "The agent VM's user data is over 16,384 bytes. A file it runs belongs in local.agent_files, not in its template."
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
