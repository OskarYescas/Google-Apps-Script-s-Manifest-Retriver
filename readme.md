# Master Implementation Guide: Google Workspace Manifest Auditor

This is a step-by-step guide on how to create a pipeline to retrive all manifest from stand alone Apps Scripts for each user in the domain (Apps Script projects in Shared Drives Out of scope)

> [!IMPORTANT]
>**IMPORTANT DISCLAIMER**: This solution offers a recommended approach that is not exhaustive and is not intended as a final enterprise-ready solution. Customers should consult their Dev, security, and networking teams before deployment.

## Phase 1: Infrastructure & Permissions

### 1. Project Setup
1.  **Create/Select Project:** Use a dedicated project (e.g., `audit-automation-prod`).
2.  **Enable APIs:**
    * Admin SDK API
    * Google Drive API
    * Apps Script API
    * BigQuery API
    * Cloud Run API
    * Cloud Build API
    * Artifact Registry API

### 2. Identity & Access Management (IAM)
> [!IMPORTANT]
> You must configure two distinct identities: the **Runtime Service Account** (which runs the script) and the **Build Agent** (which deploys the code).

#### A. The Runtime Service Account (`script-auditor`)
1.  **Create the account:**
    * **Name:** `script-auditor`
    * **Email:** `script-auditor@[PROJECT_ID].iam.gserviceaccount.com`
2.  **Copy the Unique Client ID** (required for the next step).

#### B. Domain-Wide Delegation (Admin Console)
Go to `admin.google.com` > **Security** > **Access and data control** > **API controls** > **Manage Domain Wide Delegation**.

1.  Add the **Unique Client ID**.
2.  **Scopes:**
    ```text
    [https://www.googleapis.com/auth/admin.directory.user.readonly](https://www.googleapis.com/auth/admin.directory.user.readonly),
    [https://www.googleapis.com/auth/drive.readonly](https://www.googleapis.com/auth/drive.readonly),
    [https://www.googleapis.com/auth/script.projects.readonly](https://www.googleapis.com/auth/script.projects.readonly)
    ```

#### C. Permission Grants (The "Troubleshooting" Fixes)
Run these commands in Cloud Shell to apply all necessary roles found during testing.

```bash
# 1. Setup Variables
export PROJECT_ID=[YOUR_PROJECT_ID]
export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
export RUNTIME_SA="script-auditor@${PROJECT_ID}.iam.gserviceaccount.com"
export BUILD_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

# Grant Build Permissions 
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$BUILD_SA" --role="roles/logging.logWriter"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$BUILD_SA" \
  --role="roles/storage.objectViewer"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$BUILD_SA" \
  --role="roles/artifactregistry.writer"

# Grant Self-Signing 
# This allows the SA to sign its own tokens for Domain-Wide Delegation
gcloud iam service-accounts add-iam-policy-binding $RUNTIME_SA \
  --member="serviceAccount:$RUNTIME_SA" \
  --role="roles/iam.serviceAccountTokenCreator"

# Grant BigQuery Access 
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$RUNTIME_SA" \
  --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$RUNTIME_SA" --role="roles/bigquery.jobUser"
  ```
  
## Phase 2: BigQuery Setup
- Create Dataset: manifest_dataset
- Create Table: manifest_audit_log
- Schema:
    - script_id (STRING)
    - script_name (STRING)
    - owner_email (STRING)
    - manifest_content (STRING)
    - extraction_date (TIMESTAMP)

## Phase 3: Deployment
### 1. Deploy to Cloud Run
Run this command from the folder containing your code. This uses Source Deploy and injects the cleaned environment variables.

```bash
gcloud run deploy manifest-auditor \
  --source . \
  --region us-central1 \
  --platform managed \
  --memory 2Gi \
  --cpu 2 \
  --timeout 3600 \
  --concurrency 1 \
  --no-allow-unauthenticated \
  --service-account script-auditor@[PROJECT_ID].iam.gserviceaccount.com \
  --set-env-vars PROJECT_ID=[PROJECT_ID] \
  --set-env-vars DATASET_ID=manifest_dataset \
  --set-env-vars MANIFEST_TABLE_ID=manifest_audit_log \
  --set-env-vars ADMIN_USER_EMAIL=[ADMIN_EMAIL] \
  --set-env-vars SERVICE_ACCOUNT_EMAIL=script-auditor@[PROJECT_ID].iam.gserviceaccount.com
```

## Phase 4: Automation (Scheduler)
This configuration prevents the 403 and 401 errors by strictly enforcing OIDC authentication.
### 1. Grant Invoker Permission
Allows the Service Account to "knock on the door" of the private Cloud Run service.

```bash
gcloud run services add-iam-policy-binding manifest-auditor \
  --region us-central1 \
  --member="serviceAccount:script-auditor@[PROJECT_ID].iam.gserviceaccount.com" \
  --role="roles/run.invoker"

```

### 2. Create the Scheduler Job
Configures the job with the OIDC Token, which is the "ID Badge" required to enter.

```bash
# Get the URL automatically
export SERVICE_URL=$(gcloud run services describe manifest-auditor --platform managed --region us-central1 --format 'value(status.url)')

# Create the job (Runs every Sunday at 2 AM)
gcloud scheduler jobs create http audit-weekly \
  --schedule="0 2 * * 0" \
  --uri=$SERVICE_URL \
  --http-method=POST \
  --oidc-service-account-email="script-auditor@[PROJECT_ID].iam.gserviceaccount.com" \
  --location=us-central1

```

# Terraform automation
If you want to avoid the manual set-up, then you can use the folowing terraform code to automate it.

## Prerequisites
- Google Cloud Shell: We will execute everything here.
- Workspace Admin Access: You need access to admin.google.com for the manual Domain-Wide Delegation step.
- Application Files: Ensure your main.py, mani.tf, requirements.txt, and Procfile are ready in the directory

## Phase 0: Environment Preparation
1. Check & Upgrade Terraform (Optional but Recommended)
Cloud Shell comes with Terraform installed, but it is often an older version (e.g., v1.5.7). 

### Check version
``` bash
terraform -version
```

### Upgrade version
``` bash
# 1. Download the latest binary (Update version number if needed)
wget https://releases.hashicorp.com/terraform/1.10.0/terraform_1.10.0_linux_amd64.zip

# 2. Unzip it
unzip terraform_1.10.0_linux_amd64.zip

# 3. Move it to your local bin (overwriting the old one)
sudo mv terraform /usr/bin/terraform

# 4. Verify
terraform -version
```
### Manual enablement 
```bash
# Set your project ID
export PROJECT_ID=$(gcloud config get-value project)

# Enable the Resource Manager API manually
gcloud services enable cloudresourcemanager.googleapis.com
```
## Phase 1: Terraform Infrastructure

### 1. Create the Directory & Files
Upload your files in this directory
```bash
# Create a fresh directory
mkdir manifest-auditor-infra
cd manifest-auditor-infra
```

### 2. Initialize & Apply
Execute the following in cloud shell

```bash
# 1. Initialize Terraform
terraform init

# 2. Apply the configuration
# Replace [YOUR_ADMIN_EMAIL] with the actual email
terraform apply \
  -var="project_id=$PROJECT_ID" \
  -var="admin_email=[YOUR_ADMIN_EMAIL]"
*Type `yes` when prompted.*
```

### 3. Configure DwD
```
### **Phase 2: Manual Domain-Wide Delegation**
Terraform will output a `dwd_client_id` (Green text at the bottom). You must use this now.

1.  Open **Google Admin Console** (`admin.google.com`).
2.  Navigate to **Security > Access and data control > API controls**.
3.  Click **Manage Domain Wide Delegation** (at the bottom).
4.  Click **Add new**.
5.  **Client ID:** Paste the `dwd_client_id` from Terraform.
6.  **Scopes:** Paste the `dwd_scopes` from Terraform.
7.  Click **Authorize**.
```

### 4. Application Deployment

### 5. Prepare App Files**
Ensure your `main.py`, `requirements.txt`, and `Procfile` are in the current directory (`manifest-auditor-infra`).

### 6. Deploy to Cloud Run**
We use `gcloud` here because Terraform has prepared the "Stage" (APIs & Permissions), but `gcloud` is the best tool for the "Actor" (Source Code).
```
# Set your Admin Email Variable
export ADMIN_EMAIL="[YOUR_ADMIN_EMAIL]"

# Deploy
gcloud run deploy manifest-auditor \
  --source . \
  --region us-central1 \
  --platform managed \
  --memory 2Gi \
  --cpu 2 \
  --timeout 3600 \
  --concurrency 1 \
  --no-allow-unauthenticated \
  --service-account script-auditor@${PROJECT_ID}.iam.gserviceaccount.com \
  --set-env-vars PROJECT_ID=${PROJECT_ID} \
  --set-env-vars DATASET_ID=manifest_dataset \
  --set-env-vars MANIFEST_TABLE_ID=manifest_audit_log \
  --set-env-vars ADMIN_USER_EMAIL=${ADMIN_EMAIL} \
  --set-env-vars SERVICE_ACCOUNT_EMAIL=script-auditor@${PROJECT_ID}.iam.gserviceaccount.com
```

### 7. Automation (Weekly Trigger)**

Run this to create the Cloud Scheduler job that wakes up your script every week.

```bash
# 1. Grant "Invoker" permission to the Service Account
gcloud run services add-iam-policy-binding manifest-auditor \
  --region us-central1 \
  --member="serviceAccount:script-auditor@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

# 2. Get the Service URL
export SERVICE_URL=$(gcloud run services describe manifest-auditor --platform managed --region us-central1 --format 'value(status.url)')

# 3. Create the Job (Runs Sundays at 2 AM)
gcloud scheduler jobs create http audit-weekly \
  --schedule="0 2 * * 0" \
  --uri=$SERVICE_URL \
  --http-method=POST \
  --oidc-service-account-email="script-auditor@${PROJECT_ID}.iam.gserviceaccount.com" \
  --location=us-central1
```