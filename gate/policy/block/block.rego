# Policy set "block": a firewall must not admit SSH (TCP 22) from outside the admin range, the
# reader's own source range. The set has no owners, and Atlantis has no top-level policy owners, so
# nobody can approve a failure: the plan stands, the change keeps its shard's lock, and the agent
# revises. Atlantis runs it on the saved plan's JSON (tofu show -json) as Conftest sees it:
#   conftest test -p /opt/agentic-iac/gate/policy/block <plan>.json
#
# Fail closed: anything not known to be safe at plan counts as admitting SSH from anywhere.
# Rego v1 in package main, Conftest v0.71.1's defaults. Negations go through helper rules, because a
# failing built-in inside a "not" (upper() of a null, say) makes the whole rule silently not fire.
# Plan JSON: https://github.com/opentofu/opentofu/blob/v1.13.2/website/docs/internals/json-format.mdx
# Firewall arguments: https://github.com/linode/terraform-provider-linode/blob/v4.9.0/docs/resources/firewall.md
package main

# Atlantis passes its environment to Conftest, which hands it to the policy as opa.runtime().env.
# https://github.com/runatlantis/atlantis/blob/v0.49.0/server/core/runtime/models/exec.go,
# https://github.com/open-policy-agent/conftest/blob/v0.71.1/policy/engine.go
admin_range := trim_space(object.get(opa.runtime().env, "ADMIN_RANGE", ""))

# Every reason names the admin range. With none set, no source may have SSH.
outside := sprintf("outside the admin range %s", [admin_range]) if admin_range != ""

else := "and the admin range is not set (ADMIN_RANGE is empty), so no source may have SSH"

# Firewalls this plan creates or updates; a replace includes a create.
firewalls contains rc if {
	some rc in input.resource_changes
	rc.type == "linode_firewall"
	some action in rc.change.actions
	action in {"create", "update"}
}

# An inbound_policy of ACCEPT (or one unknown until apply) admits whatever no rule drops: SSH from
# everywhere.
deny contains msg if {
	some fw in firewalls
	not drops(fw.change.after.inbound_policy)
	msg := sprintf("BLOCK: %s does not set inbound_policy = \"DROP\", so it admits SSH (TCP 22) from everywhere, %s.", [fw.address, outside])
}

# Rules generated from values unknown until apply can't be judged.
deny contains msg if {
	some fw in firewalls
	fw.change.after_unknown.inbound == true
	msg := sprintf("BLOCK: %s has inbound rules unknown until apply, so it may admit SSH (TCP 22) from anywhere, %s.", [fw.address, outside])
}

deny contains msg if {
	some fw in firewalls
	some i, rule in fw.change.after.inbound
	admits_ssh(rule)
	some source in sources_outside(fw, i, rule)
	msg := sprintf("BLOCK: %s rule %q admits SSH (TCP 22) from %s, %s.", [fw.address, label(rule), source, outside])
}

# A rule admits SSH unless it is known not to: a DROP action, a protocol other than TCP (TCP, ALL
# or protocol number 6 carry it), or ports that leave out 22.
admits_ssh(rule) if {
	not drops(rule.action)
	not other_protocol(rule.protocol)
	covers_22(ports(rule))
}

drops(action) if upper(action) == "DROP"

other_protocol(protocol) if upper(protocol) in {"UDP", "ICMP", "IPENCAP"}

other_protocol(protocol) if {
	regex.match(`^[0-9]+$`, protocol)
	to_number(protocol) != 6
}

# Missing or unknown ports mean all ports.
ports(rule) := rule.ports if is_string(rule.ports)

else := ""

covers_22(text) if trim_space(text) == ""

# "22", "1-65535", "22,80" or "80-90, 91": a list of ports and ranges.
covers_22(text) if {
	some part in split(text, ",")
	ends := split(trim_space(part), "-")
	to_number(trim_space(ends[0])) <= 22
	22 <= to_number(trim_space(ends[count(ends) - 1]))
}

# The sources a rule admits from outside the admin range: IPv4 ranges it doesn't contain, every
# IPv6 range (the admin range is IPv4), and any source unknown until apply, which the plan shows as
# null in "after" and true in "after_unknown".
sources_outside(fw, i, rule) := (ipv4_outside | ipv6) | unknown if {
	ipv4_outside := {s | some s in cidrs(rule.ipv4); is_string(s); not in_admin_range(s)}
	ipv6 := {s | some s in cidrs(rule.ipv6); is_string(s)}
	unknown := {"sources unknown until apply" | sources_unknown(fw, i)}
}

cidrs(value) := value if is_array(value)

else := []

in_admin_range(cidr) if {
	admin_range != ""
	net.cidr_contains(admin_range, cidr)
}

sources_unknown(fw, i) if {
	some field in ["ipv4", "ipv6"]
	walk(fw.change.after_unknown.inbound[i][field], [_, true])
}

label(rule) := rule.label if is_string(rule.label)

else := "(unlabelled)"
