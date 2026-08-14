# webapp

A Streamlit chat UI for the AgentCore demo. Users log in with a Cognito
username/password (no AWS credentials required), then chat with the
`chat_agent` AgentCore Runtime.

## Configure

```bash
cp .env.example .env
```

Fill in `.env` with the deployed resource IDs. Get fresh values with:

```bash
terraform -chdir=../infra/acdemo output
```

- `AGENTCORE_RUNTIME_ID` ← `agentcore_runtime_id` output
- `COGNITO_CLIENT_ID` ← `cognito_client_id` output
- `ACCOUNT_ID` ← run `aws sts get-caller-identity --query Account --output text`
  (with the `jose-sso-dev` profile, or whichever profile owns the deployment)
- `AWS_REGION` ← `us-east-1` unless the stack was deployed elsewhere

## Run

From the repo root:

```bash
uv sync --all-packages
uv run streamlit run webapp/app.py
```

(`--all-packages` is needed because this repo is a uv workspace with multiple
members — a plain `uv sync` only installs the closest project's own
dependencies. `uv sync --package webapp` also works if you only want the
webapp's deps in the shared venv.)

## Notes

- Login uses Cognito's `USER_PASSWORD_AUTH` flow. If a user was created via
  `aws cognito-idp admin-create-user` and hasn't set a password yet, Cognito
  returns a `NEW_PASSWORD_REQUIRED` challenge — the login form handles this
  as a second step.
- Chat responses stream token-by-token from the AgentCore runtime's
  `text/event-stream` response.
- No AWS credentials are needed to run this app — both Cognito auth and the
  AgentCore invoke call use bearer-token/unsigned auth, not SigV4.
