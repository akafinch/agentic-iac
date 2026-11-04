# Platform module

The OpenTofu you apply once, by hand, to stand up the demo. A plan of it reads as the inventory:
two Ubuntu 24.04 VMs (platform: forge, pipeline, policy engine; agent: web interface and agent
runs), a VPC between them, one Cloud Firewall on both, an E3 Object Storage bucket for every
shard's state with a key limited to it, two minted tokens that expire, and a delete lock on each VM.
Its own state stays local. The repository README walks the whole journey, teardown included.

## Before you apply

- `LINODE_TOKEN` in your environment: a personal access token with all scopes, from an unrestricted
  user. The tokens the module mints can't exceed its scopes, and only an unrestricted user with
  `account:read_write` can set the locks.
- An Akamai Inference API key, made in Cloud Manager. No Terraform resource can make one.
- `terraform.tfvars` from `terraform.tfvars.example`: `reader_cidrs`, `authorized_keys`,
  `inference_api_key`. Region (`us-iad-2`), plan sizes, `token_lifetime_days` (30) and `name` have
  defaults. `adp.auto.tfvars.json` holds the pinned versions, image, endpoint type and model.

```bash
tofu init && tofu plan && tofu apply
```

## What it renders

Each VM boots from cloud-init that the module renders out of your checkout:
`platform/platform.yaml.tftpl` and `platform/agent.yaml.tftpl`, reading the `gate/` and `agent/`
files they install. The agent VM's template receives only the inference key, the read-only token
and the agent's forge identity. A rendering over 65,535 bytes base64-encoded is refused at apply,
before its VM is created.

A changed rendering replaces its VM. On the platform VM that wipes the forge, and the next shard
plan would destroy what the agents built, so run the shard teardown before re-applying a change to
anything it embeds, a policy included.

## Outputs

`forge_url` and `web_url` are the two URLs you open, from your range, over plain HTTP.
`reader_forge_user` and `reader_forge_password` (`tofu output -raw reader_forge_password`) sign you
in as the approver. `platform_ip` and `agent_ip` take SSH, for operators and the teardown. The rest
are connection details and IDs: VPC addresses, the state bucket, its endpoint and region, the token
expiry and scopes.

## Not reclaimed by `tofu destroy`

The shard states and everything the agents built, which the shard teardown destroys first; the
bucket's objects, since the API deletes only an empty bucket; the Object Storage subscription; your
deploy token and inference key.

## Sources (as-of 2026-10-30)

- Linode provider v4.9.0, docs and source:
  https://github.com/linode/terraform-provider-linode/tree/v4.9.0. Instance interfaces,
  `firewall_id` and `metadata.user_data`; `linode_token` scopes and RFC 3339 expiry;
  `linode_object_storage_bucket.endpoint_type`; limited keys via `bucket_access`; `linode_lock`,
  early access, reached through the `linode.beta` alias (`api_version = "v4beta"`).
- Akamai API reference: scopes, space-separated
  (https://techdocs.akamai.com/linode-api/reference/get-started#oauth-reference); minting a token
  (`post-personal-access-token`); locks, beta (`post-resource-lock`); user data's 65,535-byte cap
  (`post-linode-instance`).
- Cloud Firewall filters VPC traffic: https://techdocs.akamai.com/cloud-computing/docs/vpc. Reserved
  subnet addresses: https://techdocs.akamai.com/cloud-computing/docs/assign-a-compute-instance-to-a-vpc.
- hashicorp/random v3.9.1 and hashicorp/time v0.14.2, both MPL-2.0. They and the Linode provider
  install from registry.opentofu.org, and the module plans without real credentials, checked with
  OpenTofu 1.13.2 on 2026-10-30.
- OpenTofu's native S3 lock held on an E3 bucket in us-iad-2 with a bucket-limited key, verified
  2026-10-29. Any account can repeat it.
