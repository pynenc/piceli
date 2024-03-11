from typing import Optional

from pydantic import BaseModel, Field
from typing_extensions import Annotated

from piceli.k8s.templates.auxiliary import names

_Port = Annotated[int, Field(gt=0, lt=65536)]


class Port(BaseModel):
    """Generic port definition"""

    name: names.Name
    port: _Port
    target_port: Optional[_Port] = None
