from cistell import ConfigBase


class ConfigPiceliBase(ConfigBase):
    """Base Config for the Pynenc Application"""

    TOML_CONFIG_ID: str = "piceli"
    ENV_PREFIX: str = "PICELI"
    ENV_SEP: str = "__"
    ENV_FILEPATH: str = "FILEPATH"
    IGNORE_CLASS_NAME_SUBSTR: str = "Config"
