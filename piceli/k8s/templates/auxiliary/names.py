from pydantic import Field
from typing_extensions import Annotated

# Kubernetes spec for identifiers
# https://github.com/kubernetes/design-proposals-archive/blob/main/architecture/identifiers.md


Name = Annotated[
    str,
    Field(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63),
]
# Name: A non-empty string guaranteed to be unique within a given scope at a particular time;
# used in resource URLs; provided by clients at creation time and encouraged to be human friendly;
# intended to facilitate creation idempotence and space-uniqueness of singleton objects,
# distinguish distinct entities, and reference particular entities across operations.


DNSLabel = Annotated[
    str, Field(pattern=r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$", max_length=63)
]
# rfc1035/rfc1123 label (DNS_LABEL):
# An alphanumeric (a-z, and 0-9) string, with a maximum length of 63 characters,
# with the '-' character allowed anywhere except the first or last character,
# suitable for use as a hostname or segment in a domain name.

DNSSubdomain = Annotated[
    str,
    Field(
        pattern=r"^(?:[a-z0-9]([-a-z0-9]*[a-z0-9])?\.)*[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
        max_length=253,
    ),
]
# rfc1035/rfc1123 subdomain (DNS_SUBDOMAIN):
# One or more lowercase rfc1035/rfc1123 labels separated by '.' with a maximum length of 253 characters.

IANASvcName = Annotated[
    str, Field(pattern=r"^[a-z]([-a-z0-9]?[a-z0-9])*$", max_length=15)
]
# rfc6335 port name (IANA_SVC_NAME):
# An alphanumeric (a-z, and 0-9) string, with a maximum length of 15 characters,
# with the '-' character allowed anywhere except the first
# or the last character or adjacent to another '-' character,
# it must contain at least a (a-z) character.

UUID = Annotated[
    str,
    Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
]
# rfc4122 universally unique identifier (UUID):
# A 128 bit generated value that is extremely unlikely to collide across time and space
# and requires no central coordination.


LABEL_KEY_REGEX = (
    r"^(?:(?P<prefix>(?:[a-z0-9]([-a-z0-9]*[a-z0-9])?\.)*[a-z0-9]([-a-z0-9]*[a-z0-9])?)/)?"
    r"(?P<name>[a-z0-9A-Z]([-_.a-z0-9A-Z]*[a-z0-9A-Z])?)$"
)
FieldPath = Annotated[str, Field(pattern=LABEL_KEY_REGEX, min_length=1, max_length=316)]
# Labels are key/value pairs.
# Valid label keys have two segments: an optional prefix and name, separated by a slash (/).
# The name segment is required and must be 63 characters or less,
# beginning and ending with an alphanumeric character ([a-z0-9A-Z])
# with dashes (-), underscores (_), dots (.), and alphanumerics between.
# The prefix is optional. If specified, the prefix must be a DNS subdomain:
# a series of DNS labels separated by dots (.), not longer than 253 characters in total,
# followed by a slash (/).
