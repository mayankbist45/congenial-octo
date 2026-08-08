# GitHub Actions auto-deploy to GCP

This repo can now deploy itself to the existing GCP VM whenever code lands on
`main`.

The deploy path reuses `scripts/deploy_gcp.sh` and `scripts/bootstrap_vm.sh`:

- `push` updates the code on the VM
- `bootstrap` re-runs provisioning only when dependencies or schedule-building
  files changed
- `deploy` is still available through manual `workflow_dispatch` for first-time
  infrastructure creation

## GitHub workflows

- `.github/workflows/ci.yml` runs `pytest -q` on every push and pull request
- `.github/workflows/deploy-gcp.yml` auto-deploys on pushes to `main`

The deploy workflow also supports manual runs from `main` with one of these
inputs:

- `push`
- `bootstrap`
- `deploy`

## One-time GCP setup

Set these shell variables first:

```bash
export GCP_PROJECT_ID="your-project-id"
export GCP_PROJECT_NUMBER="$(gcloud projects describe "$GCP_PROJECT_ID" --format='value(projectNumber)')"
export GITHUB_REPO="mayankbist45/congenial-octo"
export WIF_POOL_ID="github-actions"
export WIF_PROVIDER_ID="github-main"
export GCP_SERVICE_ACCOUNT_ID="github-actions-deployer"
export GCP_SERVICE_ACCOUNT_EMAIL="${GCP_SERVICE_ACCOUNT_ID}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
```

Create the deployer service account:

```bash
gcloud iam service-accounts create "$GCP_SERVICE_ACCOUNT_ID" \
  --project "$GCP_PROJECT_ID" \
  --display-name "GitHub Actions deployer"
```

Grant the runtime roles the workflow needs:

```bash
gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
  --member="serviceAccount:${GCP_SERVICE_ACCOUNT_EMAIL}" \
  --role="roles/compute.instanceAdmin.v1"

gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
  --member="serviceAccount:${GCP_SERVICE_ACCOUNT_EMAIL}" \
  --role="roles/compute.osAdminLogin"
```

Grant the deployer permission to act as the VM's attached service account.
This is required because `gcloud compute scp` and `gcloud compute ssh` need
`iam.serviceAccounts.actAs` on the instance service account, not just project
level Compute and OS Login roles:

```bash
export GCP_INSTANCE="congenial-octo-fund-poc"
export GCP_ZONE="asia-south1-a"
export VM_SERVICE_ACCOUNT="$(
  gcloud compute instances describe "$GCP_INSTANCE" \
    --project "$GCP_PROJECT_ID" \
    --zone "$GCP_ZONE" \
    --format='value(serviceAccounts[0].email)'
)"

gcloud iam service-accounts add-iam-policy-binding "$VM_SERVICE_ACCOUNT" \
  --project "$GCP_PROJECT_ID" \
  --member="serviceAccount:${GCP_SERVICE_ACCOUNT_EMAIL}" \
  --role="roles/iam.serviceAccountUser"
```

Create the Workload Identity pool and provider:

```bash
gcloud iam workload-identity-pools create "$WIF_POOL_ID" \
  --project "$GCP_PROJECT_ID" \
  --location="global" \
  --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc "$WIF_PROVIDER_ID" \
  --project "$GCP_PROJECT_ID" \
  --location="global" \
  --workload-identity-pool="$WIF_POOL_ID" \
  --display-name="GitHub main deploys" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository=='${GITHUB_REPO}' && assertion.ref=='refs/heads/main'"
```

Allow that GitHub identity pool to impersonate the deployer service account:

```bash
gcloud iam service-accounts add-iam-policy-binding "$GCP_SERVICE_ACCOUNT_EMAIL" \
  --project "$GCP_PROJECT_ID" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${GCP_PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL_ID}/attribute.repository/${GITHUB_REPO}"
```

Build the provider resource name for GitHub:

```bash
export WORKLOAD_IDENTITY_PROVIDER="projects/${GCP_PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL_ID}/providers/${WIF_PROVIDER_ID}"
```

## GitHub repository variables

Add these repository variables in GitHub under Settings -> Secrets and variables
-> Actions -> Variables:

```text
GCP_PROJECT=your-project-id
GCP_ZONE=asia-south1-a
GCP_INSTANCE=fund-poc
GCP_BACKUP_BUCKET=your-optional-backup-bucket
WORKLOAD_IDENTITY_PROVIDER=projects/123456789/locations/global/workloadIdentityPools/github-actions/providers/github-main
GCP_SERVICE_ACCOUNT=github-actions-deployer@your-project-id.iam.gserviceaccount.com
```

None of these values are secrets. The production `.env` remains on the VM.

## Workflow behavior

For pushes to `main`, deploy logic is:

1. authenticate to GCP using OIDC
2. verify access with `gcloud auth list` and `gcloud compute instances describe`
3. run `./scripts/deploy_gcp.sh push`
4. run `./scripts/deploy_gcp.sh bootstrap` only if one of these changed:
   - `requirements.txt`
   - `scripts/bootstrap_vm.sh`
   - `scripts/generate_schedule.py`
   - `config/markets/**`

Use manual dispatch with `deploy` the first time, then ordinary merges to
`main` handle ongoing releases.

If deploy fails during `gcloud compute scp` or `gcloud compute ssh` with
`PERMISSION_DENIED` mentioning `iam.serviceAccounts.actAs`, the missing binding
is almost always `roles/iam.serviceAccountUser` on the VM's attached service
account.
