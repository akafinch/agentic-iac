# The pre-plan code check. gate/code-check.sh runs it on each .tf file of a change, through
# Conftest's HCL2 parser, before anything is planned. Planning executes the change's code with the
# pipeline's credentials, so anything that could run a program, read the pipeline's disk or reach
# beyond the change is refused here, with a REFUSED: reason the agent reads.
#
# Rego v1 in package main, Conftest v0.71.1's defaults:
# https://github.com/open-policy-agent/conftest/blob/v0.71.1/internal/commands/test.go
# The parser is hcl2json v0.6.7: blocks become keys holding lists, and every expression that isn't a
# literal stays as its source text, inside "${...}" or "%{...}", in values and object keys alike:
# https://github.com/tmccombs/hcl2json/blob/v0.6.7/convert/convert.go
package main

# The block types a change may declare. Everything else is refused: terraform (a backend could move
# the state off its lock), provider (an endpoint or credentials file of its own), module (code from
# anywhere), import, moved, removed, check and the rest. The pipeline writes the provider and the
# state backend itself, in _pipeline.tf.
# https://github.com/opentofu/opentofu/tree/v1.13.2/website/docs/language
allowed_blocks := {"resource", "data", "variable", "output", "locals"}

deny contains msg if {
	some block, _ in input
	not block in allowed_blocks
	msg := sprintf("REFUSED: block type %q. A change may declare only resource, data, variable, output and locals blocks; the pipeline supplies the provider and the state backend.", [block])
}

# Only Linode's own types. That leaves out data "external" (runs a program), terraform_data and
# null_resource (carry provisioners), terraform_remote_state (reads another state) and every other
# provider, which the pipeline's provider mirror couldn't install anyway.
deny contains msg if {
	some kind in ["resource", "data"]
	some type, _ in input[kind]
	not linode_type(type)
	msg := sprintf("REFUSED: %s %q. A change may declare only linode_ resource and data types.", [kind, type])
}

linode_type(type) if startswith(type, "linode_")

# A provisioner runs a program on the machine that applies the plan.
# https://github.com/opentofu/opentofu/blob/v1.13.2/website/docs/language/resources/provisioners/syntax.mdx
deny contains msg if {
	some type, resources in input.resource
	some name, bodies in resources
	some body in bodies
	"provisioner" in object.keys(body)
	msg := sprintf("REFUSED: %s.%s has a provisioner, which would run a program on the pipeline's machine.", [type, name])
}

# Two Linode arguments upload a file from the machine that applies the plan.
# https://github.com/linode/terraform-provider-linode/blob/v4.9.0/docs/resources/image.md,
# https://github.com/linode/terraform-provider-linode/blob/v4.9.0/docs/resources/object_storage_object.md
local_file_arguments := {"linode_image": "file_path", "linode_object_storage_object": "source"}

deny contains msg if {
	some type, argument in local_file_arguments
	some name, bodies in input.resource[type]
	some body in bodies
	argument in object.keys(body)
	msg := sprintf("REFUSED: %s.%s sets %s, which would upload a file from the pipeline's machine.", [type, name, argument])
}

# Every function whose name contains "file" (file, filebase64, templatefile, fileset, fileexists,
# filesha256 and the rest, core:: spelling included) reads the pipeline's disk at plan, and
# templatestring renders a runtime string with all of them. OpenTofu accepts spaces, newlines and
# comments between a function's name and its "(", so the pattern does too.
# https://github.com/opentofu/opentofu/blob/v1.13.2/website/docs/language/functions/templatestring.mdx
file_call := `[A-Za-z0-9_]*file[A-Za-z0-9_]*(\s|/\*[\s\S]*?\*/|(#|//)[^\n]*)*\(|templatestring`

deny contains msg if {
	some text in all_strings
	regex.match(file_call, text)
	msg := sprintf("REFUSED: %q calls a function that reads files on the pipeline's machine (file, templatefile, fileset and the like) or templatestring.", [substring(text, 0, 120)])
}

# Every string in the document: each value, and each object key on the way to it.
all_strings contains text if {
	walk(input, [_, text])
	is_string(text)
}

all_strings contains key if {
	walk(input, [path, _])
	some key in path
	is_string(key)
}
