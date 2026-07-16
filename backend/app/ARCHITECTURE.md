# Backend module boundaries

The backend is organized by responsibility rather than by framework entrypoint:

- `api/routes/`: HTTP validation and response mapping only. Routers do not own persistence.
- `api/streaming.py`: bounded adapter between synchronous jobs and NDJSON responses.
- `services/domain_tree_jobs.py`: background task lifecycle and cancellation.
- `services/model_client.py`, `services/mineru.py`, `services/providers/`: external service clients.
- `services/model_config.py`, `services/embedding_store.py`, catalog services: persistence and configuration stores.
- `agents/`: research workflows and domain orchestration.
- `schemas/`: transport and domain data models.

`app/main.py` is the composition root. New endpoints belong in a feature router;
new network integrations belong in a service client; durable state belongs in a
store/repository; domain decisions belong in an agent or domain service.

## Deprecated implementations

The unused `services/minure.py` and `services/mineru_convert.py` prototypes were
removed. `services/mineru.py` is the sole supported MinerU integration.
