resource "random_id" "bucket" {
  byte_length = 3
}

# One bucket holds every shard's state. OpenTofu's native S3 lockfile was verified on an E3
# endpoint in us-iad-2 on 2026-10-29, with a key limited to one bucket. Versioning stays off, so
# emptying the bucket before teardown takes plain deletes. The API deletes only an empty bucket.
resource "linode_object_storage_bucket" "state" {
  label         = "${var.name}-state-${random_id.bucket.hex}"
  region        = var.region
  endpoint_type = var.state_endpoint_type
}

# The pipeline's keys: read-write on the state bucket and nothing else.
resource "linode_object_storage_key" "state" {
  label = "${var.name}-state"

  bucket_access {
    bucket_name = linode_object_storage_bucket.state.label
    region      = linode_object_storage_bucket.state.region
    permissions = "read_write"
  }
}

# A second bucket holds the files each VM runs, from your checkout, since user data can't
# (compute.tf). It holds no secret, and nothing but this module writes it: each VM reads it with a
# read-only key of its own and checks every file against the SHA-256 its user data lists. It uses the
# state bucket's endpoint type, where a bucket-limited key was verified.
resource "linode_object_storage_bucket" "files" {
  label         = "${var.name}-files-${random_id.bucket.hex}"
  region        = var.region
  endpoint_type = var.state_endpoint_type
}

# The module's own key, which uploads the files, and each VM's, which first boot fetches them with.
# None reaches the state bucket.
resource "linode_object_storage_key" "files_writer" {
  label = "${var.name}-files-writer"

  bucket_access {
    bucket_name = linode_object_storage_bucket.files.label
    region      = linode_object_storage_bucket.files.region
    permissions = "read_write"
  }
}

resource "linode_object_storage_key" "files_reader" {
  for_each = toset(["platform", "agent"])
  label    = "${var.name}-files-${each.key}"

  bucket_access {
    bucket_name = linode_object_storage_bucket.files.label
    region      = linode_object_storage_bucket.files.region
    permissions = "read_only"
  }
}

# One object per file, keyed by its path in your checkout; a changed file is uploaded again. A new key
# can take 30 seconds to work on an E3 endpoint, and the provider retries each upload until it does.
# https://github.com/linode/terraform-provider-linode/blob/v4.9.0/linode/obj/helpers.go (as-of 2026-11-04)
resource "linode_object_storage_object" "file" {
  for_each = merge(local.platform_files, local.agent_files)

  bucket     = linode_object_storage_bucket.files.label
  region     = linode_object_storage_bucket.files.region
  key        = each.key
  source     = "${local.repo_root}/${each.key}"
  etag       = filemd5("${local.repo_root}/${each.key}")
  access_key = linode_object_storage_key.files_writer.access_key
  secret_key = linode_object_storage_key.files_writer.secret_key
}
