import os
import datetime
import concurrent.futures
import signal
import sys
import logging
from google.cloud import bigquery
from google.auth import default
from google.auth import iam
from google.auth.transport import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import retry, retry_if_exception_type, wait_exponential, stop_after_attempt
from google.api_core import exceptions as google_api_exceptions

# ==============================================================================
# LOGGING CONFIGURATION
# ==============================================================================
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ==============================================================================
# CONFIGURATION & ENVIRONMENT
# ==============================================================================

PROJECT_ID = os.environ.get('PROJECT_ID')
DATASET_ID = os.environ.get('DATASET_ID')
MANIFEST_TABLE_ID = os.environ.get('MANIFEST_TABLE_ID', 'manifest_audit_log') 
ADMIN_USER_EMAIL = os.environ.get('ADMIN_USER_EMAIL')
SERVICE_ACCOUNT_EMAIL = os.environ.get('SERVICE_ACCOUNT_EMAIL')

# --- SMART SANITIZATION (The Fix) ---
# This block cleans the variables. If you provided "project.dataset", 
# it strips it down to "dataset" so the code doesn't duplicate the project ID.
if DATASET_ID and '.' in DATASET_ID:
    DATASET_ID = DATASET_ID.split('.')[-1]

if MANIFEST_TABLE_ID and '.' in MANIFEST_TABLE_ID:
    MANIFEST_TABLE_ID = MANIFEST_TABLE_ID.split('.')[-1]
# ------------------------------------

if not all([PROJECT_ID, ADMIN_USER_EMAIL, SERVICE_ACCOUNT_EMAIL]):
    logger.critical("Missing required environment variables. Exiting.")
    sys.exit(1)

try:
    BQ_CLIENT = bigquery.Client(project=PROJECT_ID)
except Exception as e:
    logger.critical(f"Failed to initialize BigQuery Client: {e}")
    sys.exit(1)

MAX_WORKERS = 8 
BQ_BATCH_SIZE = 500

SCOPES = [
    'https://www.googleapis.com/auth/admin.directory.user.readonly',
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/script.projects.readonly'
]

# ==============================================================================
# SIGNAL HANDLING
# ==============================================================================

def handle_sigterm(signum, frame):
    logger.warning(f"🛑 Received Signal {signum}. Initiating graceful shutdown...")
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)

# ==============================================================================
# AUTHENTICATION HELPERS
# ==============================================================================

def get_impersonated_service(api_name, api_version, subject_email):
    try:
        creds, _ = default()
        request = requests.Request()
        iam_signer = iam.Signer(request, credentials=creds, service_account_email=SERVICE_ACCOUNT_EMAIL)

        dwd_creds = service_account.Credentials(
            signer=iam_signer,
            service_account_email=SERVICE_ACCOUNT_EMAIL,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
            subject=subject_email
        )
        service = build(api_name, api_version, credentials=dwd_creds, cache_discovery=False)
        return service
    except Exception as e:
        logger.error(f"Auth Error for {subject_email} on API {api_name}: {e}")
        return None

# ==============================================================================
# BIGQUERY OPERATIONS
# ==============================================================================

def execute_merge_query(table_id, rows_to_insert):
    if not rows_to_insert: return
    
    # Logic: Since we sanitized inputs, we can safely reconstruct the path
    full_table_id = f"{PROJECT_ID}.{DATASET_ID}.{table_id}"
    temp_table_id = f"{table_id}_staging_{int(datetime.datetime.now().timestamp())}_{os.getpid()}"
    temp_table_ref = BQ_CLIENT.dataset(DATASET_ID).table(temp_table_id)
    target_table_ref = BQ_CLIENT.dataset(DATASET_ID).table(table_id)

    try:
        job_config = bigquery.LoadJobConfig(
            schema=BQ_CLIENT.get_table(target_table_ref).schema,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE
        )
        job = BQ_CLIENT.load_table_from_json(rows_to_insert, temp_table_ref, job_config=job_config)
        job.result() 

        query = f"""
        MERGE `{full_table_id}` AS T 
        USING `{PROJECT_ID}.{DATASET_ID}.{temp_table_id}` AS S
        ON T.script_id = S.script_id
        WHEN MATCHED THEN 
            UPDATE SET 
                T.manifest_content = S.manifest_content,
                T.extraction_date = S.extraction_date,
                T.script_name = S.script_name,
                T.owner_email = S.owner_email
        WHEN NOT MATCHED THEN 
            INSERT (script_id, script_name, owner_email, manifest_content, extraction_date)
            VALUES (S.script_id, S.script_name, S.owner_email, S.manifest_content, S.extraction_date)
        """
        
        BQ_CLIENT.query(query).result()
        logger.info(f"✅ Merged {len(rows_to_insert)} manifests into {table_id}")

    except Exception as e:
        logger.error(f"❌ BigQuery Merge Failed for {table_id}: {e}")
    finally:
        BQ_CLIENT.delete_table(temp_table_ref, not_found_ok=True)

class BigQueryBatcher:
    def __init__(self, table_id):
        self.table_id = table_id
        self.buffer = []

    def add(self, rows):
        if not rows: return
        self.buffer.extend(rows)
        if len(self.buffer) >= BQ_BATCH_SIZE:
            self.flush()

    def flush(self):
        if not self.buffer: return
        logger.info(f"🚀 Flushing batch of {len(self.buffer)} items to {self.table_id}...")
        execute_merge_query(self.table_id, self.buffer)
        self.buffer = [] 

# ==============================================================================
# MANIFEST RETRIEVAL LOGIC
# ==============================================================================

@retry(retry=retry_if_exception_type((google_api_exceptions.TooManyRequests, HttpError)), 
       wait=wait_exponential(multiplier=1, min=2, max=10), 
       stop=stop_after_attempt(3))
def get_manifest_content(script_id, script_service):
    try:
        content_resp = script_service.projects().getContent(scriptId=script_id).execute()
        files = content_resp.get('files', [])
        for f in files:
            if f.get('name') == 'appsscript' and f.get('type') == 'JSON':
                return f.get('source') 
        return None 
    except HttpError as e:
        if e.resp.status in [403, 404]:
            logger.warning(f"Access/Found Error for script {script_id}: {e}")
        raise e 
    except Exception as e:
        logger.error(f"Unknown error getting manifest for {script_id}: {e}")
        return None

def get_all_domain_users(directory_service):
    logger.info("Fetching list of all domain users...")
    users = []
    page_token = None
    try:
        while True:
            results = directory_service.users().list(
                customer='my_customer', maxResults=500, pageToken=page_token
            ).execute()
            batch_users = [u for u in results.get('users', []) if not u.get('suspended')]
            users.extend(batch_users)
            page_token = results.get('nextPageToken')
            if not page_token: break
        logger.info(f"Total active users found: {len(users)}")
        return users
    except Exception as e:
        logger.error(f"Error fetching users: {e}")
        return []

def scan_user_for_manifests(user_email):
    drive_service = get_impersonated_service('drive', 'v3', user_email)
    script_service = get_impersonated_service('script', 'v1', user_email)
    
    if not drive_service or not script_service: 
        logger.warning(f"Skipping user {user_email} due to Auth failure.")
        return []

    manifest_rows = []
    query = "mimeType='application/vnd.google-apps.script' and 'me' in owners and trashed=false"
    page_token = None
    
    try:
        while True:
            res = drive_service.files().list(
                q=query, spaces='drive',
                fields='nextPageToken, files(id, name)',
                pageSize=100, pageToken=page_token
            ).execute()

            files = res.get('files', [])
            for f in files:
                script_id = f.get('id')
                script_name = f.get('name')
                try:
                    manifest_json = get_manifest_content(script_id, script_service)
                    if manifest_json:
                        manifest_rows.append({
                            "script_id": script_id,
                            "script_name": script_name,
                            "owner_email": user_email,
                            "manifest_content": manifest_json,
                            "extraction_date": datetime.datetime.utcnow().isoformat()
                        })
                except Exception:
                    continue
            page_token = res.get('nextPageToken')
            if not page_token: break
        
        if manifest_rows:
            logger.info(f"User Scan: Retrieved {len(manifest_rows)} manifests for {user_email}.")
            
    except Exception as e:
        logger.error(f"Error scanning user {user_email}: {e}")

    return manifest_rows

# ==============================================================================
# MAIN WORKFLOW
# ==============================================================================

def main_handler(request):
    try:
        logger.info(f"Starting Manifest Audit Pipeline (Workers: {MAX_WORKERS})")
        
        dir_service = get_impersonated_service('admin', 'directory_v1', ADMIN_USER_EMAIL)
        if not dir_service: return ("Admin Auth Failure", 500)

        bq_batcher = BigQueryBatcher(MANIFEST_TABLE_ID)
        all_users = get_all_domain_users(dir_service)
        
        if not all_users:
            logger.warning("No users found. Exiting.")
            return ("No Users Found", 200)

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_email = {executor.submit(scan_user_for_manifests, user.get('primaryEmail')): user.get('primaryEmail') for user in all_users}
            for future in concurrent.futures.as_completed(future_to_email):
                email = future_to_email[future]
                try:
                    results = future.result()
                    bq_batcher.add(results)
                except Exception as exc:
                    logger.error(f"Worker exception for user {email}: {exc}")

        bq_batcher.flush()
        logger.info("Pipeline completed successfully.")
        return ("Success", 200)

    except Exception as e:
        logger.critical(f"Pipeline Critical Failure: {e}", exc_info=True)
        return (f"Failed: {e}", 500)