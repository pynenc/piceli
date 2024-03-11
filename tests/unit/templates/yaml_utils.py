import os

import yaml


def get_yaml_dict(yaml_file: str) -> dict:
    """returns a dict from the yaml file"""
    filepath = os.path.join("tests", "unit", "templates", "resources", yaml_file)
    with open(filepath, encoding="utf8") as _file:
        return yaml.safe_load(_file)
