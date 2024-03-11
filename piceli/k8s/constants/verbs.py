from enum import Enum


class APIRequestVerb(Enum):
    """Accepted request verbs for kubernetes api permissions"""

    GET = "get"
    LIST = "list"
    CREATE = "create"
    UPDATE = "update"
    PATCH = "patch"
    WATCH = "watch"
    DELETE = "delete"
    DELETE_COLLECTION = "deletecollection"

    @classmethod
    def get_read_only(cls) -> list["APIRequestVerb"]:
        """get only the read APIRequestVerb"""
        return [cls.GET, cls.LIST, cls.WATCH]

    @classmethod
    def get_all_exc_delete_collection(cls) -> list["APIRequestVerb"]:
        """get all the APIRequestVerb"""
        return [
            cls.GET,
            cls.LIST,
            cls.CREATE,
            cls.UPDATE,
            cls.PATCH,
            cls.WATCH,
            cls.DELETE,
        ]
