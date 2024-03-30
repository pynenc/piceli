# Quantity Validation

In Kubernetes resource definitions managed by Piceli, ensuring that resource quantities (such as CPU and memory requests and limits) adhere to Kubernetes' format is crucial. This document describes the validation of string representations of quantities, utilizing Kubernetes' own parsing logic to validate them.

## Overview

Quantities in Kubernetes allow specifying resources like memory and CPU in various units. For example, memory can be specified in bytes (`200Mi` for 200 mebibytes) and CPU in millicores (`500m` for half a CPU core). Correct specification of these quantities is vital for resource allocation and scheduling in Kubernetes.

The `Quantity` type in Piceli uses Pydantic's validation to ensure that quantities specified in the resource definitions are valid according to Kubernetes' standards.

## Validation Function: `check_quantity`

This function validates a string to ensure it represents a valid Kubernetes quantity.

### Parameters

- `v`: The string representation of the quantity to be validated.

### Returns

- The original string if it is a valid Kubernetes quantity.

### Raises

- `ValueError` if the string is not a valid Kubernetes quantity.

The function leverages `parse_quantity` from `kubernetes.utils.quantity` to parse the string and validate its format. This approach ensures that resources defined in Piceli templates are correctly formatted and interpretable by Kubernetes.

## Usage

The `Quantity` type is annotated with `AfterValidator(check_quantity)`, applying this validation automatically to any field in a Pydantic model that specifies resource quantities.
