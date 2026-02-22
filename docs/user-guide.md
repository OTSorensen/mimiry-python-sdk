# Mimiry User Guide

Welcome to Mimiry! This guide walks you through everything you need — from creating your account to submitting your first GPU batch job. No prior cloud-computing experience required.

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Creating Your Mimiry Account](#2-creating-your-mimiry-account)
3. [Creating an API Key](#3-creating-an-api-key)
4. [API Reference Quick Card](#4-api-reference-quick-card)
5. [Generating an SSH Key](#5-generating-an-ssh-key-if-you-dont-have-one)
6. [Adding Your SSH Key via the API](#6-adding-your-ssh-key-via-the-api)
7. [Adding Credits](#7-adding-credits)
8. [Exploring Available Resources](#8-exploring-available-resources)
9. [Submitting Your First Batch Job](#9-submitting-your-first-batch-job)
10. [Monitoring Your Job](#10-monitoring-your-job)
11. [Cancelling a Job](#11-cancelling-a-job)
12. [Troubleshooting / FAQ](#12-troubleshooting--faq)

---

## 1. Introduction

**Mimiry** is a cloud-agnostic GPU compute platform. It lets you deploy GPU instances and run batch jobs through a simple dashboard and API — without needing to manage infrastructure yourself.

### What you can do with Mimiry

- **Deploy GPU instances** — spin up powerful GPU machines on demand.
- **Run batch jobs** — submit a script that runs on a GPU, then shuts down automatically when it's done.
- **Manage SSH keys** — securely access your instances via SSH.

### What you'll need

- A computer with a **terminal** (macOS Terminal, Windows PowerShell, or Linux shell) or an API client like [Postman](https://www.postman.com/).
- An **SSH key pair** (we'll show you how to generate one if you don't have one yet).

---

## 2. Creating Your Mimiry Account

1. Open your browser and go to [https://mimiryprimary.lovable.app/](https://mimiryprimary.lovable.app/).
2. Click **Sign Up**.
3. Enter your **email address** and choose a **password**.
4. Click **Create Account**.
5. Check your email inbox for a **verification email** from Mimiry.
6. Click the verification link in the email.
7. Return to Mimiry and **log in** with your email and password.

You're in! You should see the Mimiry dashboard.

---

## 3. Creating an API Key

An API key lets you interact with Mimiry programmatically — for example, to submit batch jobs from your terminal.

1. In the left sidebar, click **Developer → API Keys**.
2. Click the **Create API Key** button.
3. Give your key a name — for example, `my-first-key`.
4. Select the **scopes** (permissions) your key needs. For this guide, select all of these:
   - `jobs:read` — view your batch jobs
   - `jobs:write` — create and cancel batch jobs
   - `instances:read` — view available GPU types
   - `ssh_keys:read` — view your SSH keys
   - `ssh_keys:write` — add and delete SSH keys
5. Click **Create**.
6. **Copy your API key immediately.** It starts with `mky_` and is only shown once. If you lose it, you'll need to create a new one.
7. Store it somewhere safe — for example, in a password manager or a secure note.

> **⚠️ Important:** Never share your API key or commit it to a public repository.

---

## 4. API Reference Quick Card

Now that you have an API key, here's what you can do with it. All endpoints use the base URL:

```
https://ypoycmbljujlkmjuhfif.supabase.co/functions/v1/public-api
```

Every request must include your API key in the `Authorization` header:

```
Authorization: Bearer mky_your_api_key_here
```

| Method   | Path               | Required Scope   | Description                            |
| -------- | ------------------ | ---------------- | -------------------------------------- |
| `GET`    | `/instances`       | `instances:read` | List all GPU instance types and prices |
| `GET`    | `/availability`    | `instances:read` | Check which GPUs are available now     |
| `GET`    | `/locations`       | `instances:read` | List datacenter locations              |
| `GET`    | `/images`          | `instances:read` | List available OS images               |
| `GET`    | `/providers`       | `instances:read` | List compute providers                 |
| `GET`    | `/ssh-keys`        | `ssh_keys:read`  | List your SSH keys                     |
| `POST`   | `/ssh-keys`        | `ssh_keys:write` | Add a new SSH key                      |
| `DELETE` | `/ssh-keys/:id`    | `ssh_keys:write` | Delete an SSH key                      |
| `GET`    | `/jobs`            | `jobs:read`      | List all your batch jobs               |
| `POST`   | `/jobs`            | `jobs:write`     | Submit a new batch job                 |
| `GET`    | `/jobs/:id`        | `jobs:read`      | Get details of a specific job          |
| `DELETE` | `/jobs/:id`        | `jobs:write`     | Cancel a running job                   |

Don't worry about memorising this — you can come back to this table any time.

---

## 5. Generating an SSH Key (if you don't have one)

SSH keys let you securely connect to your GPU instances. If you already have an SSH key pair, skip to the next section.

### macOS / Linux

Open your terminal and run:

```bash
ssh-keygen -t ed25519 -C "your-email@example.com"
```

- Press **Enter** to accept the default file location (`~/.ssh/id_ed25519`).
- Optionally enter a passphrase (recommended) or press **Enter** for no passphrase.

Your **public key** is now at:

```
~/.ssh/id_ed25519.pub
```

To view it, run:

```bash
cat ~/.ssh/id_ed25519.pub
```

Copy the entire output — you'll need it in the next step.

### Windows (PowerShell)

Open **PowerShell** and run:

```powershell
ssh-keygen -t ed25519 -C "your-email@example.com"
```

- Press **Enter** to accept the default file location (`C:\Users\YourName\.ssh\id_ed25519`).
- Optionally enter a passphrase or press **Enter** to skip.

To view your public key:

```powershell
Get-Content "$env:USERPROFILE\.ssh\id_ed25519.pub"
```

Copy the entire output.

---

## 6. Adding Your SSH Key via the API

Now let's register your public key with Mimiry so it can be attached to your instances.

### Via the API (recommended)

Replace `YOUR_API_KEY` with your actual API key, and paste your public key where indicated:

```bash
curl -X POST "https://ypoycmbljujlkmjuhfif.supabase.co/functions/v1/public-api/ssh-keys" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-laptop-key",
    "public_key": "ssh-ed25519 AAAA... your-email@example.com"
  }'
```

You'll get a response like:

```json
{
  "id": "a1b2c3d4-...",
  "name": "my-laptop-key",
  "datacrunch_id": "12345"
}
```

Save the `id` — you'll need it when submitting jobs.

### Via the Dashboard (alternative)

1. In the left sidebar, click **Compute → SSH Keys**.
2. Click **Add Key**.
3. Enter a name and paste your public key.
4. Click **Save**.

---

## 7. Adding Credits

Before you can run batch jobs, you need credits in your account.

1. Go to the **Dashboard** (click the Home icon in the sidebar).
2. Look for the **Credit Balance** card.
3. Click **Add Credits**.
4. Choose an amount and complete the payment flow.
5. Once payment is confirmed, your new balance will appear on the dashboard.

> **💡 Tip:** You can check your balance at any time from the dashboard header.

---

## 8. Exploring Available Resources

Before submitting a job, you'll want to know what hardware and software is available.

### List GPU instance types

```bash
curl -X GET "https://ypoycmbljujlkmjuhfif.supabase.co/functions/v1/public-api/instances" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

This returns a list of GPU types with specs and pricing — for example, `1V100.6V`, `8A100.80G`, etc.

### Check availability

```bash
curl -X GET "https://ypoycmbljujlkmjuhfif.supabase.co/functions/v1/public-api/availability" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

This tells you which GPU types have capacity right now.

### List datacenter locations

```bash
curl -X GET "https://ypoycmbljujlkmjuhfif.supabase.co/functions/v1/public-api/locations" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

### List OS images

```bash
curl -X GET "https://ypoycmbljujlkmjuhfif.supabase.co/functions/v1/public-api/images" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

Pick an image that includes the software you need (e.g., `ubuntu-22.04-cuda-12.0` for CUDA workloads).

---

## 9. Submitting Your First Batch Job

This is the exciting part! Let's submit a GPU job.

### Example request

```bash
curl -X POST "https://ypoycmbljujlkmjuhfif.supabase.co/functions/v1/public-api/jobs" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-first-job",
    "instance_type": "1V100.6V",
    "image": "ubuntu-22.04-cuda-12.0",
    "location": "FIN-01",
    "ssh_key_ids": ["YOUR_SSH_KEY_ID"],
    "startup_script": "#!/bin/bash\nnvidia-smi\necho \"Hello from Mimiry!\"",
    "auto_shutdown": true
  }'
```

### Field-by-field explanation

| Field              | Required | Description                                                                 |
| ------------------ | -------- | --------------------------------------------------------------------------- |
| `name`             | No       | A friendly name for your job. Defaults to `job-<short-id>` if omitted.      |
| `instance_type`    | **Yes**  | The GPU type to use (from `GET /instances`).                                |
| `image`            | **Yes**  | The OS image to boot (from `GET /images`).                                  |
| `location`         | No       | Datacenter code (from `GET /locations`). Omit to use any available location.|
| `ssh_key_ids`      | No       | Array of SSH key IDs to attach. Lets you SSH into the instance.             |
| `startup_script`   | No       | A bash script to run when the instance boots.                               |
| `auto_shutdown`    | No       | If `true`, the instance shuts down automatically when your script finishes. |

### Example response

```json
{
  "id": "f8e7d6c5-...",
  "name": "my-first-job",
  "status": "queued",
  "instance_type": "1V100.6V",
  "image": "ubuntu-22.04-cuda-12.0",
  "location": "FIN-01",
  "created_at": "2025-01-15T10:30:00Z"
}
```

Your job is now **queued** and will start provisioning shortly.

---

## 10. Monitoring Your Job

### Check a specific job

```bash
curl -X GET "https://ypoycmbljujlkmjuhfif.supabase.co/functions/v1/public-api/jobs/YOUR_JOB_ID" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

### Job status lifecycle

Your job moves through these stages:

```
queued → provisioning → running → completed
                                 ↘ failed
```

| Status           | What's happening                                           |
| ---------------- | ---------------------------------------------------------- |
| `queued`         | Your job is in the queue, waiting for a GPU to become free |
| `provisioning`   | A GPU instance is being set up for your job                |
| `running`        | Your script is executing on the GPU                        |
| `completed`      | Your script finished successfully                          |
| `failed`         | Something went wrong — check the `error_message` field     |

### List all your jobs

```bash
curl -X GET "https://ypoycmbljujlkmjuhfif.supabase.co/functions/v1/public-api/jobs" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

---

## 11. Cancelling a Job

If you need to stop a running job:

```bash
curl -X DELETE "https://ypoycmbljujlkmjuhfif.supabase.co/functions/v1/public-api/jobs/YOUR_JOB_ID" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

> **Note:** Cancelling a job will terminate the underlying GPU instance. Any unsaved work will be lost.

---

## 12. Troubleshooting / FAQ

### Common errors

| Error message                  | What it means                                                         | How to fix it                                                              |
| ------------------------------ | --------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `Invalid or missing API key`   | Your API key is missing, mistyped, or has been revoked.               | Check you're passing the key in the `Authorization: Bearer ...` header.    |
| `Insufficient credits`         | Your account balance is too low to start a job.                       | Add credits from the dashboard (see [Section 7](#7-adding-credits)).       |
| `Insufficient scope`           | Your API key doesn't have permission for this action.                 | Create a new API key with the required scopes.                             |
| `SSH key not found`            | The SSH key ID you provided doesn't exist in your account.            | Add your key first (see [Section 6](#6-adding-your-ssh-key-via-the-api)).  |
| `No available instances`       | The requested GPU type has no capacity right now.                     | Try a different `instance_type` or `location`, or wait and retry.          |

### Common mistakes

- **Forgetting the `Content-Type` header** — POST requests must include `Content-Type: application/json`.
- **Using the wrong key format** — Make sure you're pasting the **public** key (ending in `.pub`), not the private key.
- **Not copying the API key** — The key is only shown once when created. If you lost it, revoke the old key and create a new one.

### Need more help?

Visit the **Support** page in the sidebar or check the [API Roadmap](/docs/api-roadmap.md) for upcoming features.

---

*Last updated: February 2026*
