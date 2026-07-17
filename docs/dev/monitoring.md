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

**Decoupled from the app stack:** the monitoring stack has no shared network with
`friday` and no startup-order dependency — it can be brought up/down independently
in either order. Prometheus scrapes the app services (`governance`, `review-server`,
`sre-stub`, …) via `host.docker.internal` and their host-published ports, since the
main `friday` compose already exposes all of them to the host (`extra_hosts:
host.docker.internal:host-gateway` on the `prometheus` service). If `friday` isn't
running, those targets just show as down in Prometheus.

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

## Gotchas

**otel-collector restart inflates `increase()` for ~24h.** The `deltatocumulative` processor holds cumulative counter state in memory. When the monitoring stack restarts (`make monitoring-up` or `docker compose down/up`), the processor resets to 0 while Prometheus still has the old (large) counter values. Prometheus sees a counter reset and `increase()` over any window that spans the restart will be inflated by the pre-restart total. Values return to normal ~24h after the last restart. Workaround: clear the Prometheus volume (`docker volume rm friday-monitoring_prometheus-data`) to start fresh if the inflation is a problem.

**Grafana bargauge auto-scales from the full time-series max, not the reduced value.** When a bargauge panel uses `reduceOptions.calcs: ["lastNotNull"]`, you might expect the bar scale to be set from the lastNotNull values. It isn't — Grafana uses the max across every data point in the range query, including inflated intermediate values. Fix: add a `reduce` transformation (reducers: `["lastNotNull"]`) followed by a `rowsToFields` transformation (nameField: `"Field"`, valueField: `"Last (not null)"`) to convert the data to scalar fields before the panel renders. This forces the auto-scale to use the reduced values only.

**`reduce` transformation alone collapses multiple series into one bar.** The `reduce` transformation outputs a table (models as rows, value as a column). A bargauge treats that single numeric column as one bar. You must follow it with `rowsToFields` to pivot model rows into separate fields so the bargauge renders one bar per model.

**cAdvisor doesn't work on OrbStack.** OrbStack's Docker runtime uses a containerd snapshotter (`driver-type: io.containerd.snapshotter.v1`) instead of classic Docker overlay2. cAdvisor's container-detection code looks for the overlay2 graphdriver's `layerdb/mounts/<id>/mount-id` file to identify each container's read-write layer; that file doesn't exist under the containerd snapshotter, so cAdvisor fails to attach to any container and only reports root-cgroup totals — no per-container CPU/mem series at all. Don't reach for cAdvisor on this stack. Instead, per-service CPU/mem is exposed the way `governance` and `review-server` already did it: each service's own `/metrics` route (`prometheus_client.generate_latest()`) automatically emits `process_cpu_seconds_total` and `process_resident_memory_bytes` via `prometheus_client`'s default `ProcessCollector` — no extra instrumentation needed beyond wiring up the route. See `services/grafana/dashboards/container-resource-usage.json`.

**`prometheus_client`'s ProcessCollector is Linux-only.** It reads `/proc`, so `process_cpu_seconds_total` / `process_resident_memory_bytes` are silently absent when a service's `/metrics` route is exercised in a unit test running on a macOS host (e.g. via httpx's ASGI transport) — the endpoint still returns 200 with valid Prometheus text, just without those series. They appear correctly once scraped from the actual (Linux) Docker container. Don't assert on process-level metrics in host-run unit tests; assert on something platform-agnostic instead (e.g. `python_info`), and verify the process metrics with `curl` against the running container if needed.

**Prometheus (and Postgres) data lived in anonymous volumes and was wiped on every restart — fixed 2026-07-13.** Neither service declared a named `volumes:` entry; each only got the image's implicit `VOLUME` (e.g. `/prometheus`, `/var/lib/postgresql/data`), which is tied to the specific container instance. `docker compose down` removes that container, and the next `up` creates a fresh container with a brand-new empty anonymous volume — the old one is orphaned on disk (visible via `docker volume ls -f dangling=true`) but never reattached. Since the documented restart workflow is `docker compose down && docker compose up -d`, this meant Prometheus's TSDB (and Postgres's data dir) were silently reset on essentially every stack bounce. Fix: both `docker-compose.yml` (`postgres` → `postgres-data:/var/lib/postgresql/data`) and `docker-compose.monitoring.yml` (`prometheus` → `prometheus-data:/prometheus`) now mount named volumes, so `down`/`up` cycles preserve data. Orphaned anonymous volumes from before the fix are harmless leftovers — `docker volume prune` to reclaim the disk space once you're sure you don't need them. Grafana's own state (`/var/lib/grafana`) is still on an anonymous volume; low priority since dashboards/datasources are provisioned from files in git, not stored there.
