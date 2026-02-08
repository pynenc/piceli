# pylint: disable=no-member
from dataclasses import dataclass
from typing import Optional, Any

from google.oauth2 import service_account
from googleapiclient import discovery

from googleapiclient.errors import HttpError

# from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed, after_log

import logger

log = logger.get_logger(__name__)


class RetryException(Exception):
    """Exception to indicate tenacity.retry that should try again"""


@dataclass
class DirectoryClient:
    """Client for GCP Directory API"""

    credentials: service_account.Credentials

    def __post_init__(self) -> None:
        self._api: Optional[Any] = None
        # Cache project_id -> role_name -> Role
        # self._role_cache: dict[str, dict[str, Any]] = defaultdict(dict)

    @property
    def api(self) -> Any:
        """lazy evaluatio of Directory API"""
        if not self._api:
            scoped_credentials = self.credentials.with_scopes(
                ["https://www.googleapis.com/auth/admin.directory.group.readonly"]
            )  # .with_subject("admin@firm.com")
            self._api = discovery.build(
                "admin", "directory_v1", credentials=scoped_credentials
            )
        return self._api

    def get_group_members(self, group_email: str) -> list[str]:
        """Get the members of a group"""
        try:
            members = self.api.members().list(groupKey=group_email).execute()
            return [member["email"] for member in members["members"]]
        except HttpError as ex:
            if ex.resp.status == 404:
                return []
            raise ex

    def apply_group(self, group_email: str, description: str, only_check: bool) -> None:
        """check/apply the group and description"""
        raise NotImplementedError("Require permissions from Google Directory")
        # try:
        #     group = self.api.groups().get(groupKey=group_email).execute()
        #     log.info(f"Group {group_email} already exists.")
        #     if group["description"] != description:
        #         if only_check:
        #             log.warning(
        #                 f"Group {group_email} description is different and will be modifyed from '{group['description']}' to '{description}'"
        #             )
        #             return
        #         group["description"] = description
        #         _ = self.api.groups().update(groupKey=group_email, body=group).execute()
        #         log.warning(f"Group {group_email} description updated '{description}'")
        # except HttpError as ex:
        #     if ex.resp.status == 404:
        #         if only_check:
        #             log.warning(f"Group {group_email} not found, will be created")
        #             return
        #         group = {
        #             "email": group_email,
        #             "description": description,
        #         }
        #         _ = self.api.groups().insert(body=group).execute()
        #         log.warning(f"Group {group_email} created.")
        #     raise ex

    def exists_group(self, group_email: str) -> bool:
        """Check if the project exists"""
        raise NotImplementedError("Require permissions from Google Directory")
        # try:
        #     _ = self.api.groups().get(groupKey=group_email).execute()
        #     return True
        # except HttpError as ex:
        #     if ex.resp.status == 404:
        #         return False
        #     raise ex
