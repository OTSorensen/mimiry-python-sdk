# Mimiry API contract (generated — do not hand-edit)

Method, path, request-body fields and parameters distilled from the
OpenAPI specs. Regenerate with:

```
python3 scripts/gen_api_contract.py <path-to-mimiry-documentation> \
    -o .claude/review-rules/api-contract.md
```

This is the yardstick for the api-contract review specialist: every
`MimiryClient` method must match the method, path and field names below.

### auth

- `GET /api/v1/api-key`
- `POST /api/v1/auth/token` — optional: `expires_in` · params required: `X-SSH-Fingerprint(header)`, `X-SSH-Nonce(header)`, `X-SSH-Signature(header)`, `X-SSH-Timestamp(header)`
- `GET /api/v1/auth/token/limits`
- `GET /api/v1/me`

### compute

- `GET /api/compute/v1/availability` · params: `available_only(query)`, `detail(query)`, `form_factor(query)`, `gpu_family(query)`, `location(query)`, `max_age(query)`, `min_gpu_count(query)`, `min_vram_gb(query)`, `provider(query)`
- `GET /api/compute/v1/balance`
- `GET /api/compute/v1/balance/org/{id_or_name}` · params required: `id_or_name(path)`
- `GET /api/compute/v1/balance/user/{id_or_name}` · params required: `id_or_name(path)`
- `GET /api/compute/v1/catalog`
- `GET /api/compute/v1/quota`
- `GET /api/compute/v1/sessions` · params: `limit(query)`, `offset(query)`, `operation(query)`, `operation_not(query)`, `state(query)`, `state_not(query)`, `updated_after(query)`, `updated_before(query)`
- `POST /api/compute/v1/sessions` — required: `gpu`, `image`, `name` · optional: `auto_terminate`, `command`, `environment_vars`, `memory`, `org_id`, `result_storage`, `ssh_enabled`, `ssh_key_id`, `ssh_public_key`, `volume_mounts`
- `GET /api/compute/v1/sessions/{id}` · params required: `id(path)` · params: `events_tail(query)`
- `DELETE /api/compute/v1/sessions/{id}` · params required: `id(path)`
- `GET /api/compute/v1/sessions/{id}/logs` · params required: `id(path)` · params: `since(query)`, `tail(query)`, `timestamps(query)`
- `GET /api/compute/v1/transactions` · params: `limit(query)`, `offset(query)`
- `GET /api/compute/v1/volumes` · params: `limit(query)`, `offset(query)`, `operation(query)`, `operation_not(query)`, `org_id(query)`, `state(query)`, `state_not(query)`, `updated_after(query)`, `updated_before(query)`
- `POST /api/compute/v1/volumes` — required: `name`, `size_gb` · optional: `org_id`, `volume_type`
- `GET /api/compute/v1/volumes/{volume_id}` · params required: `volume_id(path)`
- `PUT /api/compute/v1/volumes/{volume_id}` — required: `new_size_gb` · params required: `volume_id(path)`
- `DELETE /api/compute/v1/volumes/{volume_id}` · params required: `volume_id(path)`

### organizations

- `GET /api/organizations/v1`
- `POST /api/organizations/v1` — required: `name`, `slug`
- `GET /api/organizations/v1/admin/orgs`
- `GET /api/organizations/v1/admin/orgs/{id}` · params required: `id(path)`
- `GET /api/organizations/v1/admin/orgs/{id}/members` · params required: `id(path)`
- `DELETE /api/organizations/v1/admin/orgs/{id}/members/{user_id}` · params required: `id(path)`, `user_id(path)`
- `GET /api/organizations/v1/by-slug/{slug}` · params required: `slug(path)`
- `POST /api/organizations/v1/invitations/{token}/accept` · params required: `token(path)`
- `POST /api/organizations/v1/invitations/{token}/decline` · params required: `token(path)`
- `GET /api/organizations/v1/operations/{request_id}` · params required: `request_id(path)`
- `GET /api/organizations/v1/operations/{request_id}/stream` · params required: `request_id(path)`
- `GET /api/organizations/v1/{id}` · params required: `id(path)`
- `PATCH /api/organizations/v1/{id}` — optional: `allow_member_invites`, `name`, `require_2fa` · params required: `id(path)`
- `DELETE /api/organizations/v1/{id}` · params required: `id(path)`
- `GET /api/organizations/v1/{id}/invitations` · params required: `id(path)`
- `GET /api/organizations/v1/{id}/members` · params required: `id(path)`
- `POST /api/organizations/v1/{id}/members/invite` — required: `email`, `role` · params required: `id(path)`
- `DELETE /api/organizations/v1/{id}/members/{user_id}` · params required: `id(path)`, `user_id(path)`
- `PATCH /api/organizations/v1/{id}/members/{user_id}/role` — required: `role` · params required: `id(path)`, `user_id(path)`

