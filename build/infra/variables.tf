# ── Yours to set, in terraform.tfvars (git ignores it; start from terraform.tfvars.example) ─────

variable "reader_cidr" {
  description = "Your source range in IPv4 CIDR form, e.g. 203.0.113.7/32. The firewall admits SSH, the forge and the web interface from it alone, and policy names it as the admin range."
  type        = string

  validation {
    # Cloud Firewall takes addresses in IP/mask form only.
    condition     = can(cidrnetmask(var.reader_cidr))
    error_message = "reader_cidr must be an IPv4 range in CIDR form, such as 203.0.113.7/32."
  }
}

variable "authorized_keys" {
  description = "SSH public keys for root on both VMs, for operators and the shard teardown."
  type        = list(string)
}

variable "inference_api_key" {
  description = "Your Akamai Inference API key, made in Cloud Manager. Only the agent VM receives it."
  type        = string
  sensitive   = true
}

# ── Per-environment footprint ─────────────────────────────────────────────────────────────────

variable "region" {
  description = "Region for both VMs, the VPC and the state bucket. us-iad-2 offers all of them, E3 Object Storage included."
  type        = string
  default     = "us-iad-2"
}

variable "platform_type" {
  description = "Plan for the platform VM, which runs the forge, the pipeline and parallel plans."
  type        = string
  default     = "g6-standard-2"
}

variable "agent_type" {
  description = "Plan for the agent VM, which runs the web interface and the agents."
  type        = string
  default     = "g6-standard-1"
}

variable "token_lifetime_days" {
  description = "Days the minted tokens live after the first apply. They must outlast the demo and its teardown, which runs on the pipeline's token."
  type        = number
  default     = 30
}

variable "name" {
  description = "Prefix for every label this module creates."
  type        = string
  default     = "agentic-iac"
}

variable "linode_provider_version" {
  description = "The Linode provider version the platform's local mirror serves to the agent's changes, so plans download nothing."
  type        = string
  default     = "4.9.0"
}

# ── Pinned by the design: set in adp.auto.tfvars.json, so each has one author ──────────────────

variable "image" {
  description = "The VMs' image."
  type        = string
}

variable "state_endpoint_type" {
  description = "Object Storage endpoint type for the state bucket."
  type        = string
}

variable "inference_endpoint" {
  description = "Akamai Inference's OpenAI-compatible base URL, configurable during the beta."
  type        = string
}

variable "inference_model" {
  description = "The agent's model, one your Akamai Inference account lists."
  type        = string
}

variable "gitea_version" {
  description = "The forge."
  type        = string
}

variable "atlantis_version" {
  description = "The plan and apply pipeline."
  type        = string
}

variable "conftest_version" {
  description = "The policy engine, running OPA's Rego."
  type        = string
}

variable "opentofu_version" {
  description = "The engine the pipeline runs and the agent validates with."
  type        = string
}

variable "python_libraries" {
  description = "The agent's pinned Python libraries."
  type        = map(string)
}
