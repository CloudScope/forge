# Forge LangGraph architecture

## One scheduler, two drivers

`OrchestrationEngine.tick(wf, step)` is the single implementation of a scheduler
step. Both runtimes call it, so scheduling, gating, retry, compensation and
failure semantics cannot drift between them.

```text
                    ┌──────────────────────────────┐
                    │ OrchestrationEngine.tick()   │
                    │  safe-stop → gate → ready-set│
                    │  → parallel fan-out → settle │
                    └───────┬──────────────┬───────┘
         TickOutcome        │              │        TickOutcome
   ("continue"|"done"|      │              │
    "await_approval")       │              │
                    ┌───────▼──────┐  ┌────▼─────────────┐
                    │ engine.run() │  │ graph.step_node  │
                    │ CLI / legacy │  │ LangGraph        │
                    │ pause+persist│  │ interrupt()      │
                    └──────────────┘  └──────────────────┘
```

`tick` never blocks on a human. An open gate is *returned* as
`action="await_approval"`; each driver surfaces it in its own idiom — the legacy
loop persists and returns control, the graph node raises a LangGraph `interrupt()`.

| Concern | Owner |
|---------|-------|
| Ready-set, barriers, conditions, parallel pool | `engine.tick` |
| Retry, circuit breaker, compensation saga | `engine.execute_node` / `_rollback_on_failure` |
| Human gate presentation + resume | driver (`run()` vs `step_node`) |
| Durable checkpoints (`checkpoints.sqlite`) | `graph/checkpointing.py` |
| Terminal metrics + artifact/DAG write | `engine.finalize_run` |
| LangSmith traces | `graph/tracing.py` |

## Graph shape

```text
START → start → step ⇄ step
                 │
                 ├─ (done) → finalize → END
                 └─ (failed path) → compensate → finalize → END
```

Human gates call `interrupt(approval_payload)`. Studio `POST /approve` maps to `LangGraphRuntime.resume_approval`.

Checkpoints persist to `var/state/langgraph/checkpoints.sqlite`, so a gate paused
overnight is still resumable after a restart. `resume_approval` falls back to
applying the decision directly to the `Workflow` when no live interrupt is found
(pruned checkpoint, or `FORGE_CHECKPOINTER=memory`).

## Agents

Unchanged: `forge/agents` REGISTRY callables. LangGraph orchestrates; it does not rewrite agent prompts.

## Switch back

```bash
FORGE_ORCHESTRATOR=legacy
```
