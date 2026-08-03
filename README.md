# Forge — Multi-Agent SDLC Platform

Production FastAPI / **uvicorn** service. Specialized agents collaborate through a dependency DAG orchestrator with parallel workers, saga compensation, durable memory, audit traces, human approval gates, reliability metrics, and a live studio UI.

Demonstration workload: **Snipr** (URL shortener).

## Production folder structure

```text
app/
├── main.py                 # uvicorn ASGI entry → main:app
├── Makefile
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
├── .env.example
├── forge/                  # Python package
│   ├── api/                # HTTP surface re-export
│   ├── core/               # paths, runtime layout
│   ├── agents/             # SDLC agent roster
│   ├── llm/                # OpenAI-compatible adapter
│   ├── dashboard.py        # FastAPI studio + APIs
│   ├── engine.py           # agent execution primitives + legacy loop
│   ├── graph/              # LangGraph runtime + LangSmith tracing
│   └── __main__.py         # CLI: python -m forge …
├── config/
│   ├── playbooks/          # workflow DAGs (YAML)
│   ├── prompts/            # agent system prompts
│   ├── examples/           # sample PRDs
│   └── scenarios/
├── deploy/
│   ├── k8s/                # Deployment / Service / PVC
│   └── terraform/          # infra scaffold
├── ops/dashboards/         # Grafana + legacy ops assets
├── scripts/                # operational helpers
├── tests/                  # unit + integration
└── var/                    # runtime data (gitignored)
    ├── state/              # workflows, checkpoints, audit, memory
    ├── artifacts/
    ├── uploads/
    ├── workspaces/         # generated product code
    └── deliverables/       # engineering summary packs
```

Override the runtime root with `FORGE_VAR_ROOT` (defaults to `./var`).

## Setup

```bash
cd app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env — set FORGE_LLM_API_KEY (or OPENAI_API_KEY) for LLM mode
```

## Run with uvicorn (preferred)

```bash
# production-style
uvicorn main:app --host 0.0.0.0 --port 8787

# local reload
uvicorn main:app --reload --port 8787

# Makefile
make run
make run-dev
```

Studio → **http://127.0.0.1:8787/**

CLI still works:

```bash
python -m forge dashboard --port 8787
python -m forge agents
python -m forge run greenfield --workers 4
```

### LLM

| Env | Purpose |
|-----|---------|
| `FORGE_LLM_API_KEY` / `OPENAI_API_KEY` | Enables LLM mode |
| `FORGE_LLM_MODEL` | Default `gpt-4o-mini` |
| `FORGE_LLM_BASE_URL` | Optional Azure / OpenRouter / vLLM |
| `FORGE_VAR_ROOT` | Runtime data directory |

```bash
curl -s http://127.0.0.1:8787/api/health | jq .llm
```

### Access control

The Studio can start workflows, approve high-risk gates, and delete every artifact
on the host, so the API is authenticated by default.

| `FORGE_API_TOKEN` | Behaviour |
|---|---|
| unset | Loopback callers only. Remote requests get `403` — binding `0.0.0.0` stays safe. |
| set | Every `/api` request must present the token. |

```bash
# remote / shared host
export FORGE_API_TOKEN=$(openssl rand -hex 24)
curl -H "Authorization: Bearer $FORGE_API_TOKEN" http://host:8787/api/workflows

# open the UI once with the token to set an HttpOnly cookie
open "http://host:8787/?token=$FORGE_API_TOKEN"
```

`GET /api/health` stays open for liveness probes and reports the active posture.
A full state wipe (`DELETE /api/workflows/cleanup`) additionally requires
`confirm=true`.

### LangGraph + LangSmith

Studio and CLI default to the **LangGraph** orchestrator (`forge/graph/`). Agents stay in `forge/agents/`; the graph runs a ready-set scheduler with `interrupt()` at human gates (`approval.clarify`, `approval.coding`, and other gates when human mode is on).

| Env | Purpose |
|-----|---------|
| `FORGE_ORCHESTRATOR` | `langgraph` (default) or `legacy` |
| `FORGE_CHECKPOINTER` | `sqlite` (default, durable) or `memory` |
| `LANGSMITH_API_KEY` | Enables LangSmith traces |
| `LANGSMITH_PROJECT` | Default `forge-sdlc` |
| `LANGSMITH_TRACING` | `true`/`false` (on when key set) |

Checkpoints are written to `var/state/langgraph/checkpoints.sqlite`, so a workflow
paused at a human gate survives a process restart. Both orchestrators execute the
same scheduler (`OrchestrationEngine.tick`); LangGraph adds `interrupt()` gates and
tracing on top, so scheduling and governance semantics cannot diverge between them.

```bash
# .env
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PROJECT=forge-sdlc

curl -s http://127.0.0.1:8787/api/health | jq '{orchestrator, langsmith}'
```

Traces appear in [LangSmith](https://smith.langchain.com) under the project name. Each scheduler step and approval interrupt is a graph node.

## Testing

```bash
make test              # full suite
make test-unit         # scheduler, gates, saga, replan, metrics, codegen
make test-integration  # API security, LangGraph runtime, end-to-end playbooks
```

Tests run with no LLM key: agents take their heuristic path, so the suite is
deterministic and offline. `tests/conftest.py` redirects `FORGE_VAR_ROOT` to a
temporary directory before importing `forge`.

**Approach.** Unit tests cover the orchestration primitives in isolation
(readiness, barriers, conditions, gate forcing, compensation ordering, invalidation,
MTTR/success-rate maths, contract validation, codegen). Integration tests drive
real playbooks end to end and assert on the resulting artifacts, validation report,
audit trace and reliability metrics. Playbooks are also checked statically — every
dependency resolves, the graph is acyclic, every agent is registered, and any
playbook with an automated validation stage must actually run the agents its
blocking gates require.

**What is verified vs. asserted.** Quality gates check evidence, not self-reports:
the OpenAPI contract is structurally validated (refs resolve, path parameters are
declared, no duplicate operationIds), every generated Python file must parse, and
API coverage is computed from the contract against named test cases. Gates that
cannot apply at a stage report `SKIP` (non-blocking) — never a green `PASS`.

**Limitation.** The generated workspace is parsed, not executed: there is no
runtime smoke test of the emitted service, and generated tests are a plan rather
than an executed suite. `code.compiles` is therefore a syntax guarantee, not a
behavioural one.

## AWS deployment (serverless)

Two planes, matched to their shapes:

```text
  Studio UI + API          Workflow execution
  ───────────────          ──────────────────
  Lambda (Web Adapter)     Step Functions (Standard)
  API Gateway HTTP API      └─ ECS Fargate task per segment
  scales to zero               └─ waitForTaskToken at every human gate
                                  parks free, up to 1 year

  State: DynamoDB (workflows, checkpoints, memory, audit) + S3 (artifacts,
  workspaces, uploads). No RDS ⇒ no VPC for Lambda ⇒ no NAT gateway.
```

A *segment* is the work between two human decisions. A Fargate task rehydrates the
run, ticks the engine until it reaches a gate or a terminal state, syncs generated
code to S3 and exits — nothing is billed while a workflow waits for a human.

### State

Remote state lives in `s3://terraform-backend-bucket-085960855786` under
`forge/<environment>/terraform.tfstate`, locked with the S3 backend's **native
lockfile** (`use_lockfile`) rather than a DynamoDB table — one fewer resource,
and no way for the lock table and the state to drift apart. Requires Terraform
≥ 1.11.

```bash
cd deploy/terraform
terraform init                                          # dev
terraform init -reconfigure \
  -backend-config="key=forge/prod/terraform.tfstate"    # any other environment
```

### Manual deploy

The Lambda and the ECS task definition reference images by tag, so the registries
must exist before the first full apply:

```bash
terraform apply -target=aws_ecr_repository.api -target=aws_ecr_repository.worker
terraform output -raw push_images | sh                  # build + push both images
terraform apply -var="image_tag=$(git rev-parse HEAD)" \
  -var="llm_api_key=sk-..." -var="api_token=$(openssl rand -hex 24)"
terraform output studio_url
```

### CI/CD

| Workflow | Trigger | Does |
|---|---|---|
| [`ci.yml`](.github/workflows/ci.yml) | every push / PR | pyflakes, pytest, `terraform fmt`+`validate`, builds both images |
| [`deploy.yml`](.github/workflows/deploy.yml) | PR → plan · push to `main` → deploy dev · manual → any environment | bootstrap ECR → push SHA-tagged images → apply → smoke test |

Authentication is **GitHub OIDC** — no AWS keys are stored in the repo. Images are
tagged with the commit SHA, never `latest`, so a rollback is an apply with an
older tag. Applies are serialised by a `concurrency` group so the state lockfile
is a backstop rather than the mechanism.

Repository configuration:

| Kind | Name | Notes |
|---|---|---|
| Secret | `AWS_DEPLOY_ROLE_ARN` | Role trusting `token.actions.githubusercontent.com`, scoped to this repo |
| Secret | `FORGE_LLM_API_KEY` | Seeds the SSM parameter on first apply |
| Secret | `FORGE_API_TOKEN` | Studio bearer token |
| Variable | `AWS_REGION` | Optional; defaults to `us-east-1` |
| Environment | `prod` | Add required reviewers here to gate promotion |

Terraform seeds the two secrets into Parameter Store on create only
(`ignore_changes = [value]`), so rotating them there does not require a redeploy —
[forge/secrets.py](forge/secrets.py) resolves the parameter at cold start. If a
parameter is unreadable the process still boots: a missing LLM key degrades agents
to their heuristic path and a missing token leaves the stricter loopback-only auth
posture in place.

The deploy is not considered done until `/api/health` answers and reports
`storage.backend=aws`, `execution.mode=stepfunctions` and `auth.token_configured=true`.

| Variable | Default | Purpose |
|---|---|---|
| `FORGE_STORAGE` | `local` | `aws` switches to S3 + DynamoDB |
| `FORGE_S3_BUCKET` | — | Artifacts, workspaces, uploads |
| `FORGE_DDB_TABLE_PREFIX` | — | Table name prefix |
| `FORGE_EXECUTION` | `thread` | `stepfunctions` for segmented execution |
| `FORGE_STATE_MACHINE_ARN` | — | Required in `stepfunctions` mode |

Both backends implement the same contracts (`forge/storage/`, `forge/execution.py`),
so local development, the test suite and AWS run identical engine code.
`GET /api/health` reports which backends are live.

**Cost** at ~100 runs and ~20k requests/month: roughly **$1.50/mo** of infra —
LLM tokens (~$0.30/run) dominate by 20×. An always-on Fargate service with an ALB
would be ~$54/mo before a single token. `terraform output estimated_monthly_cost_usd`
has the breakdown.

**Trade-offs.** Fargate tasks run in public subnets with an egress-only security
group rather than behind a NAT gateway ($33/mo) or interface endpoints (~$44/mo) —
those would cost more than everything else combined. Fargate Spot is the default
for execution; an interruption is safe because state is durable and Step Functions
retries the segment, which the engine resumes from its last checkpoint.

## Docker / Kubernetes

```bash
docker compose up --build
kubectl apply -f deploy/k8s/deployment.yaml
```

## Demo scenarios

```bash
python -m forge run greenfield --workers 4
python -m forge run brownfield
python -m forge run from_document --document config/examples/tinyurl-requirements.md
FORGE_INJECT_FAIL=test.coverage_critical python -m forge run greenfield
```
