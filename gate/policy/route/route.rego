# Policy set "route": a plan that destroys anything goes to a human. The set is owned by the
# reader's forge identity alone, with prevent_self_approve, so it fails until the reader (never the
# change's author) comments `atlantis approve_policies`; the next plan, after any new commit,
# discards that approval. Each reason is policy's own summary of the plan, which the approver reads
# instead of the model's account of it.
# Atlantis runs it on the saved plan's JSON (tofu show -json) as Conftest sees it:
#   conftest test -p /opt/agentic-iac/gate/policy/route <plan>.json
#
# "delete" appears in a delete, ["delete"], and in both replace orders, ["delete", "create"] and
# ["create", "delete"]: https://github.com/opentofu/opentofu/blob/v1.13.2/website/docs/internals/json-format.mdx
# ("forget" comes only from a removed block, which the code check refuses.)
package main

deny contains msg if {
	some rc in input.resource_changes
	"delete" in rc.change.actions
	msg := sprintf("ROUTE: %s will be %s. The reader must approve this plan before the pipeline applies it.", [rc.address, outcome(rc.change.actions)])
}

outcome(actions) := "destroyed" if {
	actions == ["delete"]
} else := "replaced (destroyed, then created)" if {
	actions == ["delete", "create"]
} else := "replaced (created, then destroyed)"
