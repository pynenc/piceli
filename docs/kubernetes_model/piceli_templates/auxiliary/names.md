# Kubernetes Identifiers and Names

This document outlines the naming conventions and patterns used within Piceli templates, adhering to Kubernetes' specifications for identifiers. These conventions ensure compatibility and uniqueness across Kubernetes resources managed by Piceli, facilitating their creation, management, and interoperability.

## Name

A `Name` in Kubernetes is a non-empty string that must be unique within a given scope at a particular time. It is used in resource URLs and is provided by clients at creation time. The naming convention encourages human-friendly names and is intended to facilitate creation idempotence, the space-uniqueness of singleton objects, distinguish distinct entities, and reference particular entities across operations.

- **Pattern**: `^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`
- **Max Length**: 63 characters

## DNSLabel

The `DNSLabel` follows the RFC 1035/RFC 1123 label standards, suitable for use as a hostname or segment in a domain name.

- **Pattern**: `^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$`
- **Max Length**: 63 characters

## DNSSubdomain

The `DNSSubdomain` encompasses one or more lowercase RFC 1035/RFC 1123 labels separated by '.', adhering to the maximum length requirement.

- **Pattern**: `^(?:[a-z0-9]([-a-z0-9]*[a-z0-9])?\.)*[a-z0-9]([-a-z0-9]*[a-z0-9])?$`
- **Max Length**: 253 characters

## IANASvcName

The `IANASvcName` is based on the RFC 6335 port naming convention, accommodating alphanumeric strings suitable for service names.

- **Pattern**: `^[a-z]([-a-z0-9]?[a-z0-9])*$`
- **Max Length**: 15 characters

## UUID

The `UUID` is a universally unique identifier following the RFC 4122 standard, designed to be highly unique across time and space without the need for central coordination.

- **Pattern**: `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`

## Labels and Field Paths

Kubernetes labels are key/value pairs with specific requirements for the keys. The `FieldPath` type is used to validate keys in Piceli, ensuring they adhere to Kubernetes' label key conventions.

- **Pattern for FieldPath**: Given pattern accommodates optional prefixes and names, conforming to Kubernetes' label syntax.
- **Max Length for FieldPath**: 316 characters

These naming and labeling conventions are integrated throughout the Piceli templates to ensure that Kubernetes resources are defined with valid and meaningful identifiers, supporting effective resource management and operations within Kubernetes clusters.
