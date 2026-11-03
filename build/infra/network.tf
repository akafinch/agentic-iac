locals {
  vpc_subnet = "10.0.0.0/24"
  # Fixed VPC addresses, so each VM's user data can name the other before either exists. A subnet's
  # first two and last two addresses are reserved:
  # https://techdocs.akamai.com/cloud-computing/docs/assign-a-compute-instance-to-a-vpc (as-of 2026-10-30)
  platform_vpc_ip = cidrhost(local.vpc_subnet, 10)
  agent_vpc_ip    = cidrhost(local.vpc_subnet, 11)

  # The ports the forge and the web interface bind: the only service ports the firewall opens.
  forge_port = 3000
  web_port   = 8000
}

# The private network. The agent VM reaches the forge over it.
resource "linode_vpc" "this" {
  label  = var.name
  region = var.region
}

resource "linode_vpc_subnet" "this" {
  vpc_id = linode_vpc.this.id
  label  = var.name
  ipv4   = local.vpc_subnet
}

# One Cloud Firewall on both VMs, attached as each is created. Inbound traffic is dropped except
# SSH, the forge and the web interface from your range, and the forge from the subnet: Cloud
# Firewall filters VPC traffic too
# (https://techdocs.akamai.com/cloud-computing/docs/vpc, as-of 2026-10-30). Atlantis's port is
# never opened; the forge reaches it on loopback.
resource "linode_firewall" "this" {
  label = var.name

  inbound {
    label    = "ssh-from-reader"
    action   = "ACCEPT"
    protocol = "TCP"
    ports    = "22"
    ipv4     = [var.reader_cidr]
  }

  inbound {
    label    = "forge-from-reader"
    action   = "ACCEPT"
    protocol = "TCP"
    ports    = tostring(local.forge_port)
    ipv4     = [var.reader_cidr]
  }

  inbound {
    label    = "web-from-reader"
    action   = "ACCEPT"
    protocol = "TCP"
    ports    = tostring(local.web_port)
    ipv4     = [var.reader_cidr]
  }

  inbound {
    label    = "forge-from-vpc"
    action   = "ACCEPT"
    protocol = "TCP"
    ports    = tostring(local.forge_port)
    ipv4     = [local.vpc_subnet]
  }

  inbound_policy  = "DROP"
  outbound_policy = "ACCEPT"
}
