# Secret rotation runbook

This runbook treats a secret change as a controlled deployment. The process changes the injected file and restarts the processes that own connections; it does not reload credentials inside a live database pool. The secret value itself must never appear in logs, tickets, release evidence, or shell history.

## Before the change

1. Confirm a tested recovery point and record the current image digest, deployment revision, active migration role, runtime role, and the processes that will be restarted.
2. Create the replacement secret in the target secret manager. Prefer a new projected file or versioned secret object so the old input remains available for rollback.
3. Copy the deployment env file outside the repository and point it at the candidate secret files. Run the preflight without contacting external systems:

   ```bash
   python deploy/deployment_preflight.py \
     --env-file /secure/path/aftercare-candidate.env \
     --output /secure/path/aftercare-candidate-preflight.json
   ```

4. Confirm the report is `pass`, the image digest is unchanged unless this is also a release, and the migration/runtime secret paths remain different. Do not use the migration secret for API or Worker processes.

## Rotation order

For an application credential such as an OIDC client secret or model key, update the provider first while the old credential remains valid, then update the projected file and restart the owning processes. Restart the API, wait for `/readyz`, and then restart Workers so no new work is claimed while the API is unavailable. If the provider supports overlapping credentials, keep the old credential until all instances report healthy.

For PostgreSQL credentials, prefer a new runtime login role with the same grants rather than changing the password in place. The safe sequence is:

1. Create the replacement runtime role and grant the exact runtime permissions.
2. Write a new runtime DSN file outside the repository and run the candidate preflight.
3. Stop Workers so they stop claiming work; recreate the API with the candidate secret.
4. Wait for API `/readyz` and run `aftercare-migrate --check` with the candidate runtime DSN.
5. Recreate Workers, verify they can reach `idle` or process a controlled test Run, and inspect that `DATABASE_URL` is absent from container environment metadata.
6. Revoke the old runtime role only after every API/Worker instance is healthy and the rollback window has been accepted.

The migration role is a short-lived deployment identity. Rotate it separately, use it only for the migration Job, and never mount it into API or Worker containers. If a migration is required during the same release, complete the migration before switching long-lived runtime credentials.

## Acceptance and rollback

Record the candidate preflight report, the restart order, readiness responses, migration check result, Worker ownership/lease result, and the time at which the old credential was revoked. These records contain identifiers and outcomes, not secret values.

Rollback means restoring the previous secret projection and restarting the same process set in the reverse order. For PostgreSQL, retain the old runtime role until the acceptance window closes; for OIDC or model providers, reactivate the old version only if the provider explicitly supports it. A failed readiness check, authentication error, unexpected lease loss, or inability to run the migration check is a failed rotation and must keep the old credential available.

The local Compose profile can exercise file parsing and process recreation, but it does not prove the target secret manager's projection, file ownership, TLS, provider overlap, or production RPO/RTO. Those are deployment-specific acceptance items.
