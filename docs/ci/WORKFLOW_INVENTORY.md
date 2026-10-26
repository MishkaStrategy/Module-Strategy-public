# Полная карта GitHub Actions workflows

Состояние: 2026-08-01. Источник: integration tree плюс изменения ветки `optimization/github-actions-ci`.

Обозначения: cache `shared` — общий Python preflight, `direct` — собственный setup-uv cache, `no` — явный dependency cache не найден; artifact/secrets/concurrency: Y/N; timeout в минутах. Среднее время доступно только для выборочно измеренных jobs и приведено в основном отчёте.

| Файл | Triggers | Jobs | Runner | Matrix | Needs | Cache | Artifact | Secrets | Permissions | Timeout | Concurrency | Класс |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `actionlint.yml` | pull_request,push,workflow_dispatch | validate | ubuntu-latest | - | - | no | N | N | contents:read | validate:10 | Y | security |
| `backtest-engine-fast.yml` | pull_request | fast | [self-hosted,fast] | python-version:2 | - | shared | Y | N | contents:read | fast:20 | Y | required/PR |
| `backtest-engine-heavy-trigger.yml` | pull_request | heavy-audit | reusable | - | - | no | N | N | contents:read | heavy-audit:- | N | benchmark/manual |
| `backtest-engine-heavy.yml` | workflow_call,workflow_dispatch | heavy-audit | [self-hosted,backtester] | - | - | shared | Y | N | contents:read | heavy-audit:180 | N | benchmark/manual |
| `breakout-return-hardening-ci.yml` | pull_request,workflow_dispatch | quality | [self-hosted,fast] | python-version:2 | - | no | Y | Y | contents:read | quality:150 | Y | required/PR |
| `ci.yml` | push,pull_request | test | [self-hosted,fast] | python-version:2 | - | shared | Y | N | contents:read | test:150 | Y | required/PR |
| `decision-router-core-v042-compatibility.yml` | pull_request | router-core-v042 | [self-hosted,fast] | - | - | no | Y | N | contents:read | router-core-v042:35 | Y | required/PR |
| `decision-router-final-gate.yml` | pull_request | final-router-gate | [self-hosted,fast] | - | - | no | Y | N | contents:read | final-router-gate:45 | Y | release |
| `docker-runner-policy.yml` | pull_request,push,workflow_dispatch | audit-dependency-routing | [self-hosted,fast] | - | - | shared | N | N | contents:read | audit-dependency-routing:15 | Y | security |
| `entry-model-ci.yml` | pull_request,workflow_dispatch | fast-quality<br>full-test<br>packaging<br>security<br>performance | [self-hosted,fast] | python-version:2<br>python-version:2 | full-test<-fast-quality<br>packaging<-full-test<br>security<-fast-quality<br>performance<-full-test | no<br>no<br>no<br>no<br>no | Y | Y | contents:read | fast-quality:30<br>full-test:45<br>packaging:35<br>security:35<br>performance:50 | Y | required/PR |
| `fast-checks.yml` | push,pull_request,workflow_dispatch | fast-checks | [self-hosted,fast] | - | - | shared | Y | N | contents:read | fast-checks:30 | N | required/PR |
| `fibonacci-module-ci.yml` | push,pull_request | test | [self-hosted,fast] | python-version:2 | - | shared | Y | N | contents:read | test:45 | Y | required/PR |
| `full-rc-release-readiness-ci.yml` | pull_request,workflow_dispatch | audit | [self-hosted,fast] | - | - | shared | Y | N | contents:read | audit:180 | Y | release |
| `fvg-module-ci.yml` | push,pull_request | test | [self-hosted,fast] | - | - | no | Y | N | contents:read | test:45 | Y | required/PR |
| `fvg-release-validation.yml` | pull_request | materialize-format<br>exact-head | [self-hosted,fast] | python-version:2 | exact-head<-materialize-format | no<br>no | Y | N | contents:read | materialize-format:30<br>exact-head:120 | Y | release |
| `heavy-backtest.yml` | workflow_dispatch | historical-backtest | [self-hosted,backtester] | - | - | shared | N | N | contents:read | historical-backtest:720 | N | benchmark/manual |
| `historical-data-backtester-bridge-ci.yml` | pull_request,push | bridge | [self-hosted,fast] | python-version:2 | - | shared | N | N | contents:read | bridge:50 | Y | data |
| `historical-data-persistent-registration-smoke-v2.yml` | pull_request,workflow_dispatch | register-and-smoke | [self-hosted,macos] | - | - | shared | Y | N | contents:read | register-and-smoke:480 | Y | data |
| `integration-ci.yml` | pull_request,push | executable-acceptance | [self-hosted,fast] | python-version:2 | - | no | Y | N | contents:read | executable-acceptance:150 | Y | required/PR |
| `liquidity-sweep-hardening-ci.yml` | pull_request,workflow_dispatch | quality | [self-hosted,fast] | python-version:2 | - | no | Y | Y | contents:read | quality:180 | Y | required/PR |
| `local-pretest-tooling-ci.yml` | pull_request,push | validate | [self-hosted,fast] | python-version:2 | - | no | Y | N | contents:read | validate:20 | Y | required/PR |
| `market-data-cli-parity-audit-ci.yml` | pull_request,push | parity-audit | [self-hosted,fast] | python-version:2 | - | shared | Y | N | contents:read | parity-audit:15 | Y | data |
| `market-data-control-plane-security-ci.yml` | pull_request,push | security-core | [self-hosted,fast] | python-version:2 | - | shared | Y | N | contents:read | security-core:35 | Y | security |
| `market-data-download-config-migration-ci.yml` | pull_request,push | migrate-download-config | [self-hosted,fast] | python-version:2 | - | shared | Y | N | contents:read | migrate-download-config:30 | Y | data |
| `market-data-durable-backfill-ci.yml` | pull_request,push | durable-backfill | [self-hosted,fast] | python-version:2 | - | shared | Y | N | contents:read | durable-backfill:30 | Y | data |
| `market-data-durable-update-ci.yml` | pull_request,push | durable-update | [self-hosted,fast] | python-version:2 | - | shared | Y | N | contents:read | durable-update:30 | Y | data |
| `market-data-exchange-read-parity-ci.yml` | pull_request,push | exchange-read-parity | [self-hosted,fast] | python-version:2 | - | shared | Y | N | contents:read | exchange-read-parity:20 | Y | data |
| `market-data-export-artifacts-ci.yml` | push,pull_request,workflow_dispatch | export-artifacts | [self-hosted,fast] | python-version:2 | - | shared | Y | N | contents:read | export-artifacts:30 | Y | data |
| `market-data-external-corpus-ci.yml` | pull_request,push | external-corpus | [self-hosted,fast] | python-version:2 | - | shared | N | N | contents:read | external-corpus:35 | Y | data |
| `market-data-external-corpus-drive-acceptance.yml` | pull_request,workflow_dispatch | verify-synced-drive-corpus | [self-hosted,backtester] | - | - | shared | Y | N | contents:read | verify-synced-drive-corpus:240 | Y | data |
| `market-data-external-corpus-registration-ci.yml` | pull_request,workflow_dispatch | register-verified-external-corpus | [self-hosted,backtester] | - | - | shared | Y | N | contents:read | register-verified-external-corpus:480 | Y | data |
| `market-data-ingestion-binance-acceptance.yml` | workflow_dispatch | public-binance-smoke | [self-hosted,fast] | - | - | shared | Y | N | contents:read | public-binance-smoke:30 | Y | data |
| `market-data-ingestion-ci.yml` | pull_request,push | interface-foundation<br>quality-package | [self-hosted,fast] | python-version:2<br>python-version:2 | - | shared<br>shared | Y | N | contents:read | interface-foundation:25<br>quality-package:35 | Y | data |
| `market-data-measured-progress-ci.yml` | pull_request,push | measured-progress | [self-hosted,fast] | python-version:2 | - | shared | Y | N | contents:read | measured-progress:30 | Y | data |
| `market-data-web-console-ci.yml` | push,pull_request,workflow_dispatch | console | [self-hosted,fast] | - | - | no | Y | N | contents:read | console:20 | Y | data |
| `market-data-web-console-e2e.yml` | push,pull_request,workflow_dispatch | browser-e2e | [self-hosted,fast] | - | - | shared | Y | Y | contents:read | browser-e2e:30 | Y | data |
| `market-regime-ci.yml` | push,pull_request | fast-quality | [self-hosted,fast] | python-version:2 | - | no | Y | N | contents:read | fast-quality:20 | Y | required/PR |
| `paper-execution-ci.yml` | pull_request,push,workflow_dispatch | component | [self-hosted,fast] | python-version:2 | - | no | Y | N | contents:read | component:90 | Y | required/PR |
| `position-exit-management-ci.yml` | push,pull_request,workflow_dispatch | fast-quality<br>full-release-gates<br>report-push-result | [self-hosted,fast] | python-version:2 | report-push-result<-fast-quality,full-release-gates | no<br>no<br>no | Y | Y | contents:read,issues:write | fast-quality:20<br>full-release-gates:180<br>report-push-result:10 | Y | required/PR |
| `position-scaling-hardening-ci.yml` | pull_request,workflow_dispatch | quality | [self-hosted,fast] | include:2 | - | direct | Y | Y | contents:read | quality:90 | Y | required/PR |
| `protection-target-ci.yml` | workflow_dispatch,push,pull_request | verify | [self-hosted,fast] | python-version:2 | - | no | Y | Y | contents:read | verify:150 | Y | required/PR |
| `research-runner-ci.yml` | push,pull_request,workflow_dispatch | fast<br>full | [self-hosted,fast] | python-version:2 | - | no<br>no | Y | N | contents:read | fast:90<br>full:120 | Y | required/PR |
| `research-runner-heavy.yml` | workflow_dispatch,pull_request | stress | [self-hosted,fast] | - | - | no | Y | N | contents:read | stress:360 | Y | benchmark/manual |
| `research-runner-release.yml` | workflow_dispatch,pull_request | release | [self-hosted,fast] | python-version:2 | - | no | Y | N | contents:read | release:90 | Y | release |
| `research-runner-security.yml` | workflow_dispatch,pull_request | security | [self-hosted,fast] | - | - | no | Y | Y | contents:read | security:45 | Y | security |
| `results-aggregator-fast.yml` | pull_request,workflow_dispatch | fast | [self-hosted,fast] | python-version:2 | - | no | Y | N | contents:read | fast:60 | Y | required/PR |
| `risk-sizing-ci.yml` | pull_request,push,workflow_dispatch | quality | [self-hosted,fast] | python-version:2 | - | direct | Y | Y | contents:read | quality:90 | Y | required/PR |
| `runner-routing-policy.yml` | pull_request,push,workflow_dispatch | audit | ubuntu-latest | - | - | shared | N | N | contents:read | audit:15 | Y | security |
| `session-range-ci.yml` | pull_request,push | test | [self-hosted,fast] | python-version:2 | - | no | Y | N | contents:read | test:90 | Y | required/PR |
| `session-range-hardening.yml` | pull_request,workflow_dispatch | quality<br>security<br>performance | [self-hosted,fast] | python-version:2 | - | no<br>no<br>no | Y | Y | contents:read | quality:60<br>security:45<br>performance:45 | Y | required/PR |
| `setup-adapter-ci.yml` | pull_request,workflow_dispatch | release-gates | [self-hosted,fast] | - | - | no | Y | Y | contents:read | release-gates:90 | Y | required/PR |
| `signal-bot-heavy-audit.yml` | workflow_dispatch | audit | [self-hosted,backtester] | - | - | shared | Y | Y | contents:read | audit:180 | N | benchmark/manual |
| `signal-bot-v0-6-integration.yml` | pull_request,push | integration | [self-hosted,fast] | python-version:2 | - | no | Y | N | contents:read | integration:120 | Y | required/PR |
| `strategy-audit-validation.yml` | pull_request,push,workflow_dispatch | package | [self-hosted,fast] | python-version:2 | - | shared | N | N | contents:read | package:45 | Y | required/PR |
| `strategy-factory-ci.yml` | pull_request,push,workflow_dispatch | quality | [self-hosted,fast] | python-version:2 | - | shared | Y | N | contents:read | quality:150 | Y | required/PR |
| `strategy-factory-hardening.yml` | pull_request,push,workflow_dispatch | hardening | [self-hosted,fast] | - | - | shared | Y | N | contents:read | hardening:120 | Y | required/PR |
| `strategy-factory-soak.yml` | pull_request,push,workflow_dispatch | extended-restart-soak | [self-hosted,fast] | - | - | shared | Y | N | contents:read | extended-restart-soak:90 | Y | benchmark/manual |
| `trading-runner-ci.yml` | pull_request,push,workflow_dispatch | component | [self-hosted,fast] | python-version:2 | - | no | Y | N | contents:read | component:45 | Y | required/PR |
| `wave-analysis.yml` | pull_request,workflow_dispatch | quality | [self-hosted,fast] | python-version:2 | - | no | Y | Y | contents:read | quality:150 | Y | required/PR |
