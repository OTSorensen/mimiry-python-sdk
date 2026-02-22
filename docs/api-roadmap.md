# API Roadmap & Architecture

## Executive Summary

This document outlines the architecture and roadmap for a cloud-agnostic programmatic API that enables users to deploy GPU instances, submit batch jobs, and manage infrastructure across multiple cloud providers through a single unified interface.

The core differentiator is provider abstraction -- users interact with one API regardless of whether their workload runs on DataCrunch, Lambda Cloud, CoreWeave, or any future provider.

---

## Phase 1: Foundation (Current)

### Public API with Provider Abstraction

A RESTful API (/public-api) authenticated via API keys (not Supabase JWT). Users generate keys from the dashboard and use them in "Authorization: Bearer <key>" headers.

Endpoints:

| Method | Path              | Description                          |
|--------|-------------------|--------------------------------------|
| GET    | /jobs             | List user's batch jobs               |
| POST   | /jobs             | Submit a new batch job               |
| GET    | /jobs/:id         | Get job status and details           |
| DELETE | /jobs/:id         | Cancel a job                         |
| GET    | /instances        | List available instance types        |
| GET    | /availability     | Check instance availability          |
| GET    | /availability/:t  | Check specific instance availability |
| GET    | /locations        | List available locations             |
| GET    | /images           | List available OS images             |
| GET    | /ssh-keys         | List user's SSH keys                 |
| POST   | /ssh-keys         | Add a new SSH key                    |
| DELETE | /ssh-keys/:id     | Delete an SSH key                    |

### SSH Key Management

Users can manage SSH keys entirely through the API, removing the need to use the DataCrunch dashboard directly.

**List SSH keys:**

    curl https://your-project.supabase.co/functions/v1/public-api/ssh-keys \
      -H "Authorization: Bearer mky_abc12345..."

**Create a new SSH key:**

    curl -X POST https://your-project.supabase.co/functions/v1/public-api/ssh-keys \
      -H "Authorization: Bearer mky_abc12345..." \
      -H "Content-Type: application/json" \
      -d '{
        "name": "my-laptop-key",
        "public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIG... user@laptop"
      }'

Response: `{ "id": "local-uuid", "name": "my-laptop-key", "datacrunch_id": "dc-uuid", "created_at": "..." }`

**Delete an SSH key:**

    curl -X DELETE https://your-project.supabase.co/functions/v1/public-api/ssh-keys/local-uuid \
      -H "Authorization: Bearer mky_abc12345..."

**Auto-Resolution:** When submitting jobs, you can use either local Mimiry key IDs or DataCrunch UUIDs in `ssh_key_ids`. The API automatically resolves local IDs to DataCrunch UUIDs before deployment.

Example Usage:

    # Submit a job
    curl -X POST https://your-project.supabase.co/functions/v1/public-api/jobs \
      -H "Authorization: Bearer mky_abc12345..." \
      -H "Content-Type: application/json" \
      -d '{
        "name": "protein-folding-run",
        "provider": "datacrunch",
        "instance_type": "1V100.6V",
        "image": "ubuntu-22.04-cuda-12.0",
        "location": "FIN-01",
        "ssh_key_ids": ["key-uuid"],
        "startup_script": "#!/bin/bash\npip install alphafold\npython run_fold.py",
        "auto_shutdown": true,
        "heartbeat_timeout_seconds": 1800,
        "max_runtime_seconds": 7200
      }'

Optional timeout fields for POST /jobs:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| heartbeat_timeout_seconds | integer | 600 | Seconds without a heartbeat before the job is marked stale. Increase for GPU workloads that may delay heartbeats (e.g., 1800 for training runs). |
| max_runtime_seconds | integer | null | Hard deadline for the job. If set, the job is marked failed after this duration + 20% buffer. Useful for cost control on GPU instances. |

    # Check job status
    curl https://your-project.supabase.co/functions/v1/public-api/jobs/job-uuid \
      -H "Authorization: Bearer mky_abc12345..."

    # List available GPUs
    curl https://your-project.supabase.co/functions/v1/public-api/instances?currency=usd \
      -H "Authorization: Bearer mky_abc12345..."

### API Key Management

Users generate and manage API keys from the dashboard UI:

- Keys are shown once at creation, stored as SHA-256 hashes
- Scopes: jobs:read, jobs:write, instances:read, ssh_keys:read, ssh_keys:write
- Keys can be named, revoked, and have optional expiration dates
- Key prefix (mky_abc1...) shown for identification

### Multi-Provider Architecture (Adapter Pattern)

The API routes through a provider registry that maps provider slugs to adapter implementations:

    Request --> Router --> Auth --> Provider Registry --> Adapter --> Cloud API

    Available Adapters:
      - DataCrunch Adapter (implemented)
      - Lambda Adapter (future)
      - CoreWeave Adapter (future)

CloudProvider Interface:

Every provider implements:
- authenticate() -- Get provider access token
- listInstanceTypes(currency) -- Available GPU types with pricing
- listLocations() -- Available datacenters
- checkAvailability(instanceType?) -- Real-time availability
- deployInstance(config) -- Launch an instance
- getInstanceStatus(id) -- Current instance state
- terminateInstance(id) -- Shut down an instance
- listImages(instanceType?) -- Available OS images

Adding a new provider requires:
1. One new adapter file implementing the interface
2. One line in the provider registry
3. One row in the "providers" database table

No changes to routing, authentication, job management, or frontend.

### Instance Agent ("Phone Home")

A lightweight bash agent injected into every startup script:

1. Preamble: Reports "started" event, begins heartbeat loop
2. Customer script: Runs unmodified
3. Epilogue: Reports "completed" or "failed", optionally triggers shutdown

The agent communicates with a dedicated callback endpoint using a one-time, job-scoped token (not the user's API key). This enables:

- Real-time job status without polling the cloud provider
- Accurate billing (exact start/stop times)
- Auto-shutdown to prevent idle GPU billing
- Log streaming
- Webhook triggers on completion

---

## Phase 2: Advanced Features

### Webhooks (1-2 weeks)

What: Users register callback URLs to receive HTTP POST notifications when jobs change state.

Database: New webhook_endpoints table with url, events (array), secret (for HMAC signing), is_active.

Implementation:
- When job-callback receives a status change, check for matching webhook subscriptions
- POST to the user's URL with a signed payload
- Retry logic with exponential backoff (3 attempts)
- Webhook delivery log for debugging

Complexity: Low-Medium. The callback infrastructure already exists; this adds a fan-out step.

### Auto-Shutdown (2-3 weeks)

What: Instances automatically terminate when the job script finishes, preventing idle GPU charges.

Implementation:
- The instance agent epilogue calls "shutdown -h now" after phoning home
- The callback endpoint triggers provider-specific termination as backup
- User opt-in via auto_shutdown: true in job submission
- Grace period option (e.g., keep alive for 5 minutes after completion for debugging)

Complexity: Medium-High. Requires reliable detection of script completion across providers, handling edge cases (script crashes, network failures), and graceful fallback if the agent fails.

### Job Queuing & Retry (3-4 weeks)

What: When requested GPU types are not available, jobs enter a queue and automatically deploy when capacity opens up.

Implementation:
- pg_cron job polls availability every 60 seconds for queued jobs
- Priority queue based on submission time
- Configurable retry limits and timeout
- Notification when a queued job starts or times out

Complexity: High. Requires careful state management, race condition handling (multiple queued jobs competing for the same slot), and user-facing queue position visibility.

### Billing Integration (2-3 weeks)

What: Automatic credit metering based on actual GPU usage time.

Implementation:
- Capture exact started_at and completed_at from instance agent
- Calculate cost: duration_hours x hourly_rate
- Deduct from user credits automatically
- Daily reconciliation against provider invoices
- Usage reports and spending alerts

Complexity: Medium. The metering data comes from the agent; the challenge is accurate rate mapping across providers and handling edge cases (interrupted jobs, provider billing discrepancies).

### SDKs (2-3 weeks each)

Python SDK:

    from mimiryk import Client

    client = Client(api_key="mky_abc12345...")

    job = client.jobs.create(
        name="protein-folding",
        provider="datacrunch",
        instance_type="1V100.6V",
        image="ubuntu-22.04-cuda-12.0",
        location="FIN-01",
        script="pip install alphafold && python run_fold.py",
        auto_shutdown=True
    )

    status = client.jobs.get(job.id)

Node.js SDK:

    import { Mimiryk } from '@mimiryk/sdk';

    const client = new Mimiryk({ apiKey: 'mky_abc12345...' });

    const job = await client.jobs.create({
      name: 'training-run',
      provider: 'datacrunch',
      instanceType: '1V100.6V',
      autoShutdown: true,
      script: '#!/bin/bash\npython train.py'
    });

Complexity: Low-Medium per SDK. Thin wrappers around the REST API with typed responses, error handling, and convenience methods.

---

## Phase 2 Timeline

| Feature             | Estimated Duration | Dependencies              |
|---------------------|-------------------|---------------------------|
| Webhooks            | 1-2 weeks         | Phase 1 complete          |
| Auto-Shutdown       | 2-3 weeks         | Instance agent            |
| Job Queuing         | 3-4 weeks         | Webhooks (for notifs)     |
| Billing Integration | 2-3 weeks         | Auto-shutdown (for timing)|
| Python SDK          | 2-3 weeks         | Public API stable         |
| Node.js SDK         | 2-3 weeks         | Public API stable         |

Total estimated: 10-15 weeks (some features can be parallelized)

---

## Security Model

| Layer                | Mechanism                                              |
|----------------------|--------------------------------------------------------|
| API Authentication   | SHA-256 hashed API keys with scoped permissions        |
| Instance Agent       | One-time callback tokens, invalidated after completion |
| Webhook Delivery     | HMAC-SHA256 signed payloads                            |
| Provider Credentials | Stored as Supabase secrets, never exposed to users     |
| Database Access      | Row-Level Security policies on all tables              |

---

## Architecture Diagram

    User / SDK                Public API              Provider Registry
    (API Key Auth)            Edge Function
         |                         |                        |
         +--- HTTP Request ------->+                        |
                                   |                        |
                              [ Router ]                    |
                              [ Auth   ]                    |
                              [ Validate ]                  |
                                   |                        |
                                   +--- get provider ------>+
                                                            |
                                                   [ DataCrunch Adapter ]
                                                   [ Lambda Adapter     ] (future)
                                                   [ CoreWeave Adapter  ] (future)
                                                            |
                                                            v
                                                      Cloud Provider API


    GPU Instance              Job Callback
    (Phone-Home Agent)        Edge Function
         |                         |
         +--- started ------------>+--- update batch_jobs
         +--- heartbeat --------->+--- update last_heartbeat
         +--- completed --------->+--- mark done, invalidate token
         +--- failed ------------>+--- mark failed, record error
