terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = "us-central1"
}

variable "project_id" {
  description = "The ID of the GCP project"
  type        = string
}

variable "admin_email" {
  description = "The Google Workspace Admin email"
  type        = string
}

# ===================================================================
# PHASE 1: APIs
# ===================================================================
resource "google_project_service" "enabled_apis" {
  for_each = toset([
    "admin.googleapis.com",             # Admin SDK
    "drive.googleapis.com",             # Drive API
    "script.googleapis.com",            # Apps Script API
    "bigquery.googleapis.com",          # BigQuery API
    "run.googleapis.com",               # Cloud Run API
    "cloudbuild.googleapis.com",        # Cloud Build API
    "artifactregistry.googleapis.com",  # Artifact Registry API
    "cloudscheduler.googleapis.com",    # For the automation phase
    "iam.googleapis.com",               # For IAM operations
    "compute.googleapis.com"            # REQUIRED: Creates the Default Compute SA
  ])
  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

# ===================================================================
# PHASE 1: Identity (Runtime Service Account)
# ===================================================================
resource "google_service_account" "script_auditor" {
  account_id   = "script-auditor"
  display_name = "Script Auditor Runtime SA"
  project      = var.project_id
  depends_on   = [google_project_service.enabled_apis]
}

# ===================================================================
# PHASE 1: IAM Permissions (Runtime SA Fixes)
# ===================================================================

# FIX 2: Grant Self-Signing to the Runtime SA
# "roles/iam.serviceAccountTokenCreator" on itself
resource "google_service_account_iam_member" "sa_self_impersonation" {
  service_account_id = google_service_account.script_auditor.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.script_auditor.email}"
}

# FIX 3: Grant BigQuery Access to Runtime SA
resource "google_project_iam_member" "sa_bq_editor" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.script_auditor.email}"
}

resource "google_project_iam_member" "sa_bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.script_auditor.email}"
}

# ===================================================================
# PHASE 1: IAM Permissions (Build Agent Fixes)
# ===================================================================

# FIX 1: Grant Build Permissions to Default Compute SA
# We first fetch the project number to construct the email
data "google_project" "project" {}

locals {
  # The default compute service account used by Cloud Build/Source Deploy
  build_sa = "${data.google_project.project.number}-compute@developer.gserviceaccount.com"
}

resource "google_project_iam_member" "build_sa_logging" {
  project    = var.project_id
  role       = "roles/logging.logWriter"
  member     = "serviceAccount:${local.build_sa}"
  # CRITICAL: Wait for APIs to enable (creates the SA) before assigning roles
  depends_on = [google_project_service.enabled_apis]
}

resource "google_project_iam_member" "build_sa_storage" {
  project    = var.project_id
  role       = "roles/storage.objectViewer"
  member     = "serviceAccount:${local.build_sa}"
  # CRITICAL: Wait for APIs to enable (creates the SA) before assigning roles
  depends_on = [google_project_service.enabled_apis]
}

resource "google_project_iam_member" "build_sa_artifact" {
  project    = var.project_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${local.build_sa}"
  # CRITICAL: Wait for APIs to enable (creates the SA) before assigning roles
  depends_on = [google_project_service.enabled_apis]
}

# ===================================================================
# PHASE 2: BigQuery Setup
# ===================================================================
resource "google_bigquery_dataset" "manifest_dataset" {
  dataset_id  = "manifest_dataset"
  location    = "US"
  description = "Dataset for Manifest Auditor logs"
  project     = var.project_id
  depends_on  = [google_project_service.enabled_apis]
}

resource "google_bigquery_table" "manifest_audit_log" {
  dataset_id = google_bigquery_dataset.manifest_dataset.dataset_id
  table_id   = "manifest_audit_log"
  project    = var.project_id

  # Schema defined in JSON format
  schema = <<EOF
[
  {
    "name": "script_id",
    "type": "STRING",
    "mode": "REQUIRED"
  },
  {
    "name": "script_name",
    "type": "STRING",
    "mode": "NULLABLE"
  },
  {
    "name": "owner_email",
    "type": "STRING",
    "mode": "NULLABLE"
  },
  {
    "name": "manifest_content",
    "type": "STRING",
    "mode": "NULLABLE"
  },
  {
    "name": "extraction_date",
    "type": "TIMESTAMP",
    "mode": "NULLABLE"
  }
]
EOF
}

# ===================================================================
# OUTPUTS (For Manual DWD)
# ===================================================================
output "dwd_client_id" {
  value       = google_service_account.script_auditor.unique_id
  description = "The Unique ID to copy into the Admin Console for Domain-Wide Delegation."
}

output "dwd_scopes" {
  value = <<EOT
https://www.googleapis.com/auth/admin.directory.user.readonly,
https://www.googleapis.com/auth/drive.readonly,
https://www.googleapis.com/auth/script.projects.readonly
EOT
  description = "Comma-separated scopes for Domain-Wide Delegation."
}