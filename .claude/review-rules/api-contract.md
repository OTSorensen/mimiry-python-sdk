# Mimiry API contract (generated — do not hand-edit)

Method, path, and request-body field names distilled from the OpenAPI
specs. Regenerate with:

```
python3 scripts/gen_api_contract.py <path-to-mimiry-documentation> > .claude/review-rules/api-contract.md
```

This is the yardstick for the api-contract review specialist: every
`MimiryClient` method must match the method and field names below.

### auth

- `GET /api/v1/api-key`
- `POST /api/v1/auth/token` · optional: `expires_in`
- `GET /api/v1/auth/token/limits`
- `GET /api/v1/me`

### compute

- `GET /api/compute/v1/availability`
- `GET /api/compute/v1/balance`
- `GET /api/compute/v1/balance/org/{id_or_name}`
- `GET /api/compute/v1/balance/user/{id_or_name}`
- `GET /api/compute/v1/catalog`
- `GET /api/compute/v1/quota`
- `GET /api/compute/v1/sessions`
- `POST /api/compute/v1/sessions` — required: `gpu`, `image`, `name` · optional: `auto_terminate`, `command`, `environment_vars`, `memory`, `org_id`, `result_storage`, `ssh_enabled`, `ssh_key_id`, `ssh_public_key`, `volume_mounts`
- `GET /api/compute/v1/sessions/{id}`
- `DELETE /api/compute/v1/sessions/{id}`
- `GET /api/compute/v1/sessions/{id}/logs`
- `GET /api/compute/v1/transactions`
- `GET /api/compute/v1/volumes`
- `POST /api/compute/v1/volumes` — required: `name`, `size_gb` · optional: `org_id`, `volume_type`
- `GET /api/compute/v1/volumes/{volume_id}`
- `PUT /api/compute/v1/volumes/{volume_id}` — required: `new_size_gb`
- `DELETE /api/compute/v1/volumes/{volume_id}`

### organizations

- `GET /api/organizations/v1`
- `POST /api/organizations/v1` — required: `name`, `slug`
- `GET /api/organizations/v1/admin/orgs`
- `GET /api/organizations/v1/admin/orgs/{id}`
- `GET /api/organizations/v1/admin/orgs/{id}/members`
- `DELETE /api/organizations/v1/admin/orgs/{id}/members/{user_id}`
- `GET /api/organizations/v1/by-slug/{slug}`
- `POST /api/organizations/v1/invitations/{token}/accept`
- `POST /api/organizations/v1/invitations/{token}/decline`
- `GET /api/organizations/v1/operations/{request_id}`
- `GET /api/organizations/v1/operations/{request_id}/stream`
- `GET /api/organizations/v1/{id}`
- `PATCH /api/organizations/v1/{id}` · optional: `allow_member_invites`, `name`, `require_2fa`
- `DELETE /api/organizations/v1/{id}`
- `GET /api/organizations/v1/{id}/invitations`
- `GET /api/organizations/v1/{id}/members`
- `POST /api/organizations/v1/{id}/members/invite` — required: `email`, `role`
- `DELETE /api/organizations/v1/{id}/members/{user_id}`
- `PATCH /api/organizations/v1/{id}/members/{user_id}/role` — required: `role`

