# Agentic IaC

When a model writes the infrastructure code, where do state, policy and approval live? A working answer on Akamai Cloud, with the model served by Akamai Inference.

You ask for an infrastructure change in plain language. An agent writes the OpenTofu and proposes it
as a pull request, holding no credential that can change anything. The pipeline plans it with its
own credentials, policy judges the saved plan (it passes, is blocked, or is routed to you), and only
the pipeline applies it, then merges. Each shard's state sits in Object Storage, locked during every
plan and apply.

Two VMs on Akamai Cloud, each holding only its own credentials. The **platform** VM runs the forge
(Gitea), the pipeline (Atlantis) and the policy engine (Conftest). The **agent** VM runs the web
interface and the agents, whose model Akamai Inference serves.

| Path | What it holds |
|---|---|
| `build/infra/` | The OpenTofu module you apply once, by hand (its README lists the inventory) |
| `platform/` | Each VM's first boot (cloud-init), and the shard teardown |
| `gate/` | The pre-plan code check, the policies, and the pipeline's server-side config |
| `agent/` | The web interface, its service and the agent |
| `tests/` | Plans and policy checks. Nothing is deployed. |

## Before you start

- **Akamai Inference.** It is a closed beta (as of 2026-10-29) with a configurable endpoint,
  `inference_endpoint` in `build/infra/adp.auto.tfvars.json`. Make an API key in Cloud Manager.
- **A deploy token:** a personal access token with all scopes, from an unrestricted user. It never
  leaves your machine. As of 2026-10-29, Akamai Cloud API automation authenticates with scoped,
  expiring personal access tokens; check your account for newer options.
- **OpenTofu 1.13.2**, and an SSH key for the operator steps.

## Deploy

```bash
cd build/infra
cp terraform.tfvars.example terraform.tfvars   # your range (reader_cidr), SSH keys, inference key
export LINODE_TOKEN=...                         # the deploy token
tofu init && tofu apply
```

The plan reads as the inventory: two VMs, a firewall, a private network, the state bucket and its
key, two minted tokens that expire, and a delete lock on each VM. The delete locks use an
early-access API, which may be unavailable to some accounts or regions.

First boot takes several minutes after the apply. An operator can watch it over SSH:
`ssh root@$(tofu output -raw platform_ip) cloud-init status --wait`, then the same with `agent_ip`.
Then, from your range, over plain HTTP:

```bash
tofu output -raw web_url                 # the web interface, where you ask the agent
tofu output -raw forge_url               # the forge: sign in as reader
tofu output -raw reader_forge_password
```

## See it work

1. In the web interface, send the sample request, "Add a firewall that allows HTTPS and SSH from
   anywhere", on `shards/demo`. The agent's tool calls stream in, and its HCL appears in the forge
   as a change.
2. Policy blocks the plan, saying the SSH rule admits sources outside your range. The agent revises
   the same change to your range, the new plan passes, and the pipeline applies exactly that saved
   plan and merges it.
3. Ask "Remove that firewall". Policy routes the destroy to you, and the agent waits. In the forge,
   read policy's summary on the change and comment `atlantis approve_policies`. The pipeline then
   applies and merges.
4. While that change waits for you, ask for another change on `shards/demo`. The second agent waits
   behind the first change, calling no model, until it merges, then plans atop it.
5. Send two requests with "a new shard": each has its own state and lock, so both plan and apply in
   parallel.

Verified against Akamai Inference (beta) with GLM-5.3 as of 2026-10-29; confirm model availability
and behaviour in your own account. Verified with CrewAI 1.15.33 against Akamai Inference (beta) and
GLM-5.3 as of 2026-10-29; confirm model availability and behaviour in your own account. The demo was
verified on an Akamai Inference beta account as of 2026-10-29. Beta access and limits vary by
account; confirm your own in Cloud Manager.

**Footgun.** Re-applying after you edit anything rendered into a VM's user data, a policy included,
replaces that VM. On the platform VM that wipes the forge, and the next plan would destroy what the
agents built. Run the shard teardown first.

## Tear down

From `build/infra/`:

1. **The shards** (operator, over SSH, with no agent run in flight). This destroys everything the
   agents built, with the pipeline's own credentials, then empties the state bucket:
   ```bash
   ssh root@$(tofu output -raw platform_ip) /opt/agentic-iac/platform/teardown-shards.sh
   ```
2. **The platform:** `tofu destroy`. It lifts each delete lock before the VM it guards, and revokes
   the tokens and keys it minted.
3. **Object Storage:** cancel it in Cloud Manager, unless other buckets need it. Cancelling deletes
   any buckets left ([docs](https://techdocs.akamai.com/cloud-computing/docs/cancel-object-storage)).
4. **Your two credentials:** revoke the deploy token
   ([docs](https://techdocs.akamai.com/cloud-computing/docs/manage-personal-access-tokens)) and the
   inference key
   ([docs](https://techdocs.akamai.com/akamai-inference-beta/docs/manage-inference-api-keys)).

## Tests

`uv run pytest` plans the module and renders both VMs' user data, but never applies them. It also
runs the gate's fixtures through the real code check and policies. It needs OpenTofu 1.13.2 and
Conftest 0.71.1 on PATH. The providers download from registry.opentofu.org.
