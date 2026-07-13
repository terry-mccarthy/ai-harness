# Monitoring & Observability

## Monitoring stack (Phase 5)

The monitoring stack (otel-collector, Prometheus, Grafana) lives in its own compose
file, `docker-compose.monitoring.yml`, as a separate compose project
(`friday-monitoring`) so it can be brought up/down independently of the app stack.

```bash
make monitoring-up     # docker compose -f docker-compose.monitoring.yml up -d
make monitoring-down   # docker compose -f docker-compose.monitoring.yml down
# starts prometheus:9090, grafana:3000, and otel-collector:4317
# Grafana: admin/admin — dashboards are pre-provisioned (see services/grafana/dashboards/)
```

**Shared network:** Prometheus scrapes the app services by their compose DNS names
(`governance:8090`, `review-server:9003`, `sre-stub:9005`, …). Those containers run
in the main `friday` stack, so the monitoring stack attaches to that stack's network
as an external network (`friday_default`). Bring the main stack up first — there is
nothing to scrape otherwise, and until then Prometheus reports the app targets as
down. Config: `docker-compose.monitoring.yml` (`networks: friday_default: external: true`).

Governance exposes `GET /metrics`. Metrics: `harness_tool_calls_total`, `harness_tool_call_latency_ms`. (`harness_rate_limit_rejections_total` was removed — rate limiting delegated to CF.)

## Claude Code OTEL telemetry pipeline

Claude Code emits OTLP metrics natively. The pipeline:

```
Claude Code (Mac host)
  → OTLP gRPC :4317
    → otel-collector (Docker, monitoring profile)
      → Prometheus scrapes :8889
        → Grafana dashboards
```

**To activate:** start the monitoring stack then set this env var before launching Claude:

```bash
make monitoring-up
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 claude
```

**Config files:**
- `services/otel-collector/otel-collector.yml` — collector config
- `services/grafana/dashboards/claude-code-telemetry.json` — overview (sessions, cost, tokens)
- `services/grafana/dashboards/claude-code-by-project.json` — per-project/branch breakdown

**Delta → cumulative gotcha:** Claude Code emits delta temporality; Prometheus requires cumulative. The `deltatocumulative` processor in `otel-collector.yml` converts automatically — no env var needed on the Claude side. Alternatively, `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative` bypasses the processor.

**Resource attribute labels:** `resource_to_telemetry_conversion: enabled: true` in the exporter causes OTEL resource attributes (service.name, project_name, project_branch, host.arch, etc.) to appear as Prometheus labels — this is what makes the per-project dashboard's template variables work.

**Key metrics:** `claude_code_session_count_total`, `claude_code_cost_usage_USD_total`, `claude_code_token_usage_tokens_total`.
