# Resources Arithmetic in Kubernetes

This document outlines the functionality provided by the `Resources` class within Piceli for managing and performing arithmetic operations on Kubernetes resource quantities (CPU, memory, ephemeral storage).

## Overview

Managing resources effectively is crucial in Kubernetes to ensure optimal application performance and resource utilization. The `Resources` class provides a robust framework for defining, validating, and manipulating resource quantities, supporting operations like addition, subtraction, multiplication, and division.

## Features

- **Resource Definition**: Define quantities for memory, CPU, and ephemeral storage using Kubernetes-compatible string representations.
- **Arithmetic Operations**: Perform addition, subtraction, multiplication, and division on resource quantities, facilitating easy manipulation and comparison of resources.
- **Conversion Utilities**: Convert between dictionaries and `Resources` objects, supporting both string and numeric (kilo, mega, etc.) representations.
