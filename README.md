# Mimiry Python SDK

Python client for the [Mimiry GPU Cloud API](https://mimiryprimary.lovable.app). Deploy GPU instances, submit batch jobs, and manage cloud resources programmatically.

## Installation

```bash
pip install mimiry
```

Or install from source:

```bash
git clone https://github.com/mimiry-com/mimiryprimary
cd public/sdk/python
pip install .
```

## Quick Start

```python
from mimiry import MimiryClient

client = MimiryClient(api_key="mky_your_key_here")

# List available GPUs
for gpu in client.list_instance_types():
    print(f"{gpu['instance_type']} — ${gpu['price_per_hour']}/hr")

# Check availability
available = client.check_availability("1V100.6V")

# Submit a job
job = client.submit_job(
    name="training-run",
    instance_type="1V100.6V",
    image="ubuntu-22.04-cuda-12.0",
    location="FIN-01",
    ssh_key_ids=["your-key-uuid"],
    startup_script="#!/bin/bash\npython train.py",
    auto_shutdown=True,
)
print(f"Job {job['id']} submitted — status: {job['status']}")
```

## Authentication

1. Create an API key from the [Mimiry Dashboard](https://mimiryprimary.lovable.app) → **API Keys**
2. Pass it to the client:

```python
client = MimiryClient(api_key="mky_your_key_here")
```

API keys require scopes for the endpoints you want to access:

| Scope | Endpoints |
|-------|-----------|
| `jobs:read` | List/get jobs |
| `jobs:write` | Submit/cancel jobs |
| `instances:read` | List GPUs, locations, availability, images, providers |
| `ssh_keys:read` | List SSH keys |
| `ssh_keys:write` | Add/delete SSH keys |

## API Reference

### Jobs

```python
# List all jobs
jobs = client.list_jobs()

# Get job details
job = client.get_job("job-uuid")

# Submit a job
job = client.submit_job(
    name="my-job",
    instance_type="1V100.6V",
    image="ubuntu-22.04-cuda-12.0",
    location="FIN-01",
    ssh_key_ids=["key-uuid"],
    startup_script="#!/bin/bash\nnvidia-smi",
    auto_shutdown=True,
    heartbeat_timeout_seconds=1800,  # optional, default 600
    max_runtime_seconds=7200,        # optional, no default
)

# Cancel a job
client.cancel_job("job-uuid")

# Wait for a job to finish (polls every 10s, timeout 1h)
result = client.wait_for_job("job-uuid", poll_interval=10, timeout=3600)

# Submit and wait in one call
result = client.submit_job_and_wait(
    name="my-job",
    instance_type="1V100.6V",
    image="ubuntu-22.04-cuda-12.0",
    location="FIN-01",
    ssh_key_ids=["key-uuid"],
    startup_script="#!/bin/bash\npython train.py",
)
```

### Instance Types

```python
# List all GPU types with pricing
gpus = client.list_instance_types(currency="usd")

# Filter by provider
gpus = client.list_instance_types(provider="datacrunch")
```

### Availability

```python
# Check all availability
available = client.check_availability()

# Check specific instance type
available = client.check_availability(instance_type="1V100.6V")
```

### Locations

```python
locations = client.list_locations()
```

### OS Images

```python
# All images
images = client.list_images()

# Images compatible with a specific GPU type
images = client.list_images(instance_type="1V100.6V")
```

### Providers

```python
providers = client.list_providers()
```

### SSH Keys

```python
# List keys
keys = client.list_ssh_keys()

# Add a key
key = client.add_ssh_key("my-laptop", open("~/.ssh/id_ed25519.pub").read())

# Delete a key
client.delete_ssh_key("key-uuid")
```

## Error Handling

The SDK raises typed exceptions for API errors:

```python
from mimiry import MimiryClient, AuthenticationError, InsufficientCreditsError

client = MimiryClient(api_key="mky_...")

try:
    job = client.submit_job(...)
except AuthenticationError:
    print("Invalid API key")
except InsufficientCreditsError:
    print("Not enough credits — top up at the dashboard")
except MimiryError as e:
    print(f"API error [{e.status_code}]: {e.message}")
```

| Exception | HTTP Status | Meaning |
|-----------|-------------|---------|
| `AuthenticationError` | 401 | Invalid or missing API key |
| `InsufficientCreditsError` | 402 | Not enough credits |
| `InsufficientScopeError` | 403 | API key lacks required scope |
| `NotFoundError` | 404 | Resource not found |
| `RateLimitError` | 429 | Too many requests |
| `ServerError` | 5xx | Server-side error |
| `MimiryError` | other | Catch-all base exception |

## Context Manager

The client can be used as a context manager to ensure connections are closed:

```python
with MimiryClient(api_key="mky_...") as client:
    jobs = client.list_jobs()
```

## Configuration

```python
client = MimiryClient(
    api_key="mky_...",
    base_url="https://custom-endpoint.example.com",  # override API URL
    timeout=60.0,       # request timeout in seconds (default 30)
    max_retries=5,      # retry count for transient failures (default 3)
)
```

## Requirements

- Python ≥ 3.8
- `httpx` ≥ 0.24.0

## License

MIT
