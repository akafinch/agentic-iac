# The two URLs you open, from your range. Plain HTTP, behind the firewall.
output "forge_url" {
  description = "The forge. Sign in as reader to approve routed plans."
  value       = "http://${local.platform_ip}:${local.forge_port}"
}

output "web_url" {
  description = "The web interface, where you ask the agent for a change."
  value       = "http://${local.agent_ip}:${local.web_port}"
}

output "reader_forge_user" {
  description = "Your identity on the forge: the approver."
  value       = local.forge_users.reader
}

output "reader_forge_password" {
  description = "Read it with: tofu output -raw reader_forge_password"
  value       = random_password.forge["reader"].result
  sensitive   = true
}

output "platform_ip" {
  description = "The platform VM's public IPv4, for SSH: operators and the shard teardown."
  value       = local.platform_ip
}

output "agent_ip" {
  description = "The agent VM's public IPv4, for SSH."
  value       = local.agent_ip
}

output "platform_vpc_ip" {
  value = local.platform_vpc_ip
}

output "agent_vpc_ip" {
  value = local.agent_vpc_ip
}

output "state_bucket" {
  description = "Holds every shard's state. Empty it before tofu destroy."
  value       = linode_object_storage_bucket.state.label
}

output "state_endpoint" {
  value = "https://${linode_object_storage_bucket.state.s3_endpoint}"
}

output "state_region" {
  value = linode_object_storage_bucket.state.region
}

output "token_expiry" {
  value = time_offset.token_expiry.rfc3339
}

output "pipeline_token_scopes" {
  value = local.pipeline_scopes
}

output "agent_token_scopes" {
  value = local.agent_scopes
}

output "platform_id" {
  value = linode_instance.platform.id
}

output "agent_id" {
  value = linode_instance.agent.id
}

output "vpc_id" {
  value = linode_vpc.this.id
}

output "subnet_id" {
  value = linode_vpc_subnet.this.id
}

output "firewall_id" {
  value = linode_firewall.this.id
}
