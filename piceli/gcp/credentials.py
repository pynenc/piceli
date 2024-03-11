import base64
import json
import logging

import google.auth
from google.oauth2 import service_account

from piceli.settings import GCE_SA_INFO

logger = logging.getLogger(__name__)


def get_credentials(gce_sa_info: str | None = None) -> service_account.Credentials:
    gce_sa_info = gce_sa_info or GCE_SA_INFO
    if gce_sa_info:
        # remove the first two chars and the last char in the key
        _credentials = json.loads(base64.b64decode(gce_sa_info).decode("utf-8"))
        return service_account.Credentials.from_service_account_info(
            _credentials, scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    credentials, default_project_id = google.auth.default()
    logger.debug(f"Obtained GCP credentials ({default_project_id=})")
    return credentials
