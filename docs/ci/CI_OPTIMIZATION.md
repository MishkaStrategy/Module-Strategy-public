# Оптимизация GitHub Actions и CI/CD

Дата состояния: 2026-08-02  
Репозиторий: `MishkaStrategy/Module-Strategy`  
Ветка: `optimization/github-actions-ci`  
Base: `integration/modular-strategy-full-rc` @ `30263f9e2bc4a972bc0027ffec40d23a0b3f2a37`

## 1. Архитектура и baseline

В integration tree находятся **59 workflow-файлов** и **70 объявленных jobs** до раскрытия matrix. Нагрузки распределены между GitHub-hosted Ubuntu и self-hosted runners с capability labels `fast`, `docker`, `macos`, `backtester`.

Контрольная выборка одного точного SHA до оптимизации:

- 50 workflow runs;
- 84 expanded jobs;
- 5 213 job-seconds = 86,9 минуты;
- 63 artifacts / 12,16 MB.

Это exact-SHA baseline, а не rolling average. Полная карта workflows находится в [`WORKFLOW_INVENTORY.md`](WORKFLOW_INVENTORY.md).

| Метрика | Baseline | Текущее состояние |
|---|---:|---:|
| Workflows | 59 | 59 |
| Declared jobs | 70 | 70 |
| Upload-artifact steps с explicit retention | 55 / 61 | 61 / 61 |
| Mutable external Action uses | 165 | 2 grandfathered |
| Direct setup-uv без embedded manifest | не контролировалось | 0, policy-enforced |
| Наблюдаемое job time | 86,9 мин | требуется rolling sample после интеграции |

## 2. Подтверждённые bottlenecks

1. Повторные загрузки Python-зависимостей и широкие cache keys, зависящие от чужих monorepo packages.
2. Managed-Python cache на persistent self-hosted runners: restore/save занимал больше времени, чем локальное provision уже установленного интерпретатора.
3. Duplicate triggers: Strategy Audit запускался на feature push и pull request для одного SHA.
4. Supply-chain drift: 165 вызовов third-party Actions использовали mutable major tags.
5. Прямые setup-uv steps зависели от live version manifest на `raw.githubusercontent.com`; подтверждённый сетевой отказ блокировал Liquidity Sweep до установки зависимостей.
6. Console npm cache восстанавливал около 56 MB за 42–60 секунд, тогда как locked `npm ci` выполнялся менее секунды.
7. GitHub artifact storage мог вернуть `ECONNRESET` после успешных тестов и превратить substantive success в красный workflow.
8. Большой накопленный PR diff повторно запускает path-filtered workflows на каждом commit; поэтому stale-run cancellation и стабильный head критичны для очереди self-hosted runners.

Самые длинные jobs в исходной выборке: Wave Analysis Python 3.13 — 337 s; Strategy Audit Python 3.12 — 200 s; Liquidity Sweep — 197/190 s; persistent dataset registration — 176 s; Market Regime quality — 155 s.

## 3. Shared Python preflight

`.github/actions/python-runner-preflight/action.yml` теперь:

- проверяет capability role, OS, архитектуру, disk и RAM;
- использует immutable setup-uv Action SHA;
- закрепляет `uv 0.11.31`;
- передаёт embedded immutable release manifest с официальными platform-specific SHA-256;
- имеет checksum-verified fallback загрузки официального uv archive;
- включает retries и transport timeouts;
- создаёт точное Python 3.12/3.13 окружение под `RUNNER_TEMP`;
- использует workload-specific dependency cache keys;
- не кеширует managed Python на persistent self-hosted runners;
- публикует cache diagnostics в job summary.

Managed-Python cache отключён осознанно: на измеренном Fast checks run provision уже установленного Python занял около 4 секунд, а сохранение Python cache добавляло около 8–12 секунд post-job работы и сетевого трафика.

## 4. Immutable setup-uv manifest policy

Подтверждённый Liquidity Sweep failure показал, что даже immutable setup-uv Action и фиксированная версия не исключают live lookup version metadata.

Все прямые `astral-sh/setup-uv` вызовы в `.github/workflows/**` и `.github/actions/**` теперь обязаны иметь:

1. immutable 40-character Action SHA;
2. literal semantic `version`;
3. literal embedded `data:application/x-ndjson` manifest.

Policy реализован в:

- `scripts/audit_setup_uv_manifest.py`;
- `tests/test_setup_uv_manifest_policy.py`;
- `.github/workflows/runner-routing-policy.yml`.

Repository-level тест сканирует весь текущий tree. Новый прямой setup-uv step без manifest, фиксированной версии или immutable Action SHA блокирует policy gate.

Manifest-hardening применён к Liquidity Sweep, FVG fast/release, Breakout Return, Wave Analysis, Decision Router compatibility, v0.6 acceptance, Signal Bot integration, Research Runner fast/full/heavy/release/security, Position Scaling, Risk Sizing и Session Range hardening.

## 5. Supply-chain и permissions

- **163 third-party Action references** закреплены по immutable commit SHA.
- Mutable external Action references сокращены с **165 до 2**.
- Оба grandfathered исключения находятся в `.github/workflows/backtest-engine-fast.yml`, принадлежащем параллельному Draft PR #287.
- `scripts/audit_action_pinning.py` и `.github/action-pinning-baseline.json` не позволяют добавить новый mutable ref или увеличить число исключений.
- Checkout credentials отключены в read-only workflows, где push не требуется.
- Workflow permissions остаются explicit и read-only по умолчанию.
- Untrusted PR не получил write-capable или secret-bearing выполнение.

## 6. Dependency cache scope

Shared preflight cache keys теперь разделены по Python и фактическому dependency set.

Примеры:

- Fast checks: root + Risk Sizing metadata;
- Market Data ingestion: только `modules/market_data_ingestion/pyproject.toml`;
- Strategy Factory soak: только реально устанавливаемые factory/audit/risk/results package metadata;
- Signal Bot heavy audit и Historical Backtest: workload-specific metadata;
- Position Scaling: module + выбранный Core under test;
- Risk Sizing: root + Risk Sizing package metadata.

Это уменьшает ложные cache misses из-за изменений в неиспользуемых monorepo packages.

## 7. Queue и concurrency

- Actionlint выполняется на `ubuntu-latest`, не занимая self-hosted queue.
- Central policy workflows используют PR/ref concurrency с `cancel-in-progress: true`.
- Fast checks отменяет stale runs при новом commit.
- Strategy Audit duplicate feature-branch push удалён; PR и post-merge integration trigger сохранены.
- Signal Bot и Backtest Engine heavy audits дедуплицируются по exact SHA.
- Strategy Factory soak отменяет устаревший run и выполняется на capability class `backtester` после подтверждённого внешнего cancellation на 2 GiB fast runner.
- Checkout сделан shallow там, где history не нужен.

## 8. Artifact policy и transport isolation

Все **61** шага `actions/upload-artifact` имеют explicit literal retention в диапазоне 1–90 дней.

- 7–14 дней: обычные diagnostics, JUnit/coverage и component evidence;
- 30 дней: security/supply-chain и долгие stress evidence;
- существующие более длительные release/readiness сроки сохранены, где они были частью процесса.

`scripts/audit_artifact_retention.py` блокирует отсутствие retention, expression вместо integer и значения вне диапазона.

Для diagnostic artifacts доставка отделена от substantive gate, когда GitHub artifact service не является release deliverable. В частности, Market Data ingestion больше не становится красным после успешных 413 тестов, coverage, MyPy, security, build и clean-wheel smoke только из-за `ECONNRESET` при upload.

## 9. Node и browser workflows

Market Data Web Console:

- получает deterministic Node 22.12;
- использует read-only checkout;
- выполняет locked `npm ci`;
- не восстанавливает контрпродуктивный 56 MB npm cache;
- повторяет только распознанные registry/network failures npm audit;
- actual vulnerability findings fail immediately;
- сохраняет audit-attempt logs как evidence;
- сохраняет TypeScript, unit-test и production-build gates.

Playwright retries также ограничены transport failures и остаются fail-closed.

## 10. Подтверждённые измерения

### Fast checks

После отключения redundant managed-Python caching:

- setup/preflight: примерно 62,6 s → 6,4 s;
- post-cache: примерно 12,4 s → 4,1 s;
- полный job: примерно 76 s → 41 s;
- экономия: около 35 s на run.

### Strategy Factory restart soak

Подтверждённые runs:

- 100 cold-process iterations;
- 5 restart scenarios per iteration;
- 500 total checks;
- 74–79 секунд elapsed;
- zero transient empty-process retries;
- historical 1800-second target met.

### Market Data Web Console

- удалён restore около 56 MB, занимавший 42–60 секунд;
- locked `npm ci`: менее секунды;
- production audit: 0 vulnerabilities;
- 15 tests passed;
- production build passed.

## 11. Сохранённые quality/security gates

Ни один test suite, coverage threshold, security audit, clean-wheel/sdist check, benchmark или restart gate не удалён.

В частности сохранены:

- Core и Backtest matrices;
- v0.6 executable acceptance;
- Wave/Breakout/Liquidity central repository compatibility;
- Liquidity one-million-event segmented restart stress;
- Strategy Factory 100-cycle cold-process soak;
- package reproducibility и exact-head evidence;
- Bandit, detect-secrets, pip-audit и CycloneDX SBOM gates.

Research Runner full-mode дополнительно исправлен: ошибочный текст `run: |` внутри Ruff shell-блока удалён, отдельные Ruff, MyPy и coverage gates восстановлены.

## 12. Обязательная validation matrix

1. actionlint;
2. immutable Action policy;
3. immutable setup-uv manifest policy;
4. artifact-retention policy;
5. runner-routing и coverage-policy tests;
6. provisioning shell syntax;
7. Fast checks;
8. Core CI Python 3.12/3.13;
9. affected component matrices;
10. clean wheel/sdist и security gates;
11. корректность path/branch conditional skips.

Exact-head run IDs и финальный SHA фиксируются в описании Draft PR.

## 13. Rollback plan

Изменения разделены на обратимые CI-only commits:

- shared preflight/cache commits;
- immutable Action pinning и baseline;
- setup-uv manifest hardening и policy;
- retention policy;
- concurrency/trigger changes;
- workflow-specific transport isolation.

Rollback не требует migration данных, release rollback или изменения production Python.

## 14. Координация и оставшиеся риски

- Default branch: `main`.
- Integration base: `integration/modular-strategy-full-rc` @ `30263f9e2bc4a972bc0027ffec40d23a0b3f2a37`.
- PR #287 владеет `.github/workflows/backtest-engine-fast.yml`; файл намеренно не изменяется этим потоком.
- Live API ранее показал отсутствие branch protection для `main` и `integration/modular-strategy-full-rc`, а также отсутствие repository rulesets.
- Required checks, block force-push/delete и conversation resolution требуют административной настройки репозитория.
- Полный online/idle inventory self-hosted runners не доступен через подключённый интерфейс.
- Rolling post-merge runtime/cache-hit sample остаётся эксплуатационным наблюдением, а не блокером CI-кода.
