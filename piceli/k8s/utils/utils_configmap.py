import os
from typing import Optional


def get_configmap_data_from_files(
    filepaths: list[str], mappings: Optional[dict[str, str]] = None
) -> dict[str, str]:
    """
    loads the filepath into a string and returns a dict [filename:filecontents]

    if mappings is specified will replace...
    """
    # this could easily support an array of filepath
    data = {}
    for filepath in filepaths:
        with open(filepath, encoding="utf8") as _file:
            content = _file.read()
            if mappings:
                content = content.format(**mappings)
            data[os.path.basename(filepath)] = content
    return data
