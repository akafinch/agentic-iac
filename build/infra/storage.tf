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
