from cistell import ConfigField

from piceli.conf.config_base import ConfigPiceliBase


class ConfigK8s(ConfigPiceliBase):
    """
    Main config of the k8s module.

    :cvar ConfigField[str] namespace:
        Default namespace used by piceli k8s module opperations.


    """

    namespace = ConfigField("default")
