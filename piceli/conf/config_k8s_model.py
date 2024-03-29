from cistell import ConfigField

from piceli.conf.config_base import ConfigPiceliBase


class ConfigK8sModel(ConfigPiceliBase):
    """
    Config to specify the location of the specification of the k8s model

    :cvar ConfigField[str] module_name:
        python import notation to the module containing the k8s model specification
        the module can contain piceli templates or objects from the
        official (kubernetes python library)["https://github.com/kubernetes-client/python"]

    :cvar ConfigField[str] module_path:
        absoluth or relative path to the module containing the k8s model specification
        the module can contain piceli templates or objects from the
        official (kubernetes python library)["https://github.com/kubernetes-client/python"]

    :cvar ConfigField[str] folder_path:
        absoluth or relative path to folder containing the files specifying the k8s model
        piceli supports yaml and json, it will load any file in the folder/subfolders
    """

    module_name = ConfigField("")
    module_path = ConfigField("")
    folder_path = ConfigField("")
    sub_elements = ConfigField(True)
