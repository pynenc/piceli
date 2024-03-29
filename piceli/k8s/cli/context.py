from dataclasses import dataclass


@dataclass
class ContextObject:
    namespace: str
    module_name: str
    module_path: str
    folder_path: str
    sub_elements: bool
