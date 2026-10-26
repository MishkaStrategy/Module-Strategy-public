# Canonical Strategy Variant 2 decision

## Owner authorization

- Owner: `mishkacher`
- Approved at: `2026-08-03T03:40:00Z`
- GitHub authority: `https://github.com/MishkaStrategy/Module-Strategy/issues/312#issuecomment-5162067264`
- Composition: `FVG-RT Final 15m + Frozen 1H Short + Frozen 4H Short`

The owner selected Variant 2: create a new deterministic canonical candidate rather than reverse-engineer the historical 395-trade result. Historical counts `283 / 91 / 21 / 395` remain comparison references only.

## Version identities

- `fvg_rt_final_15m@2.0.0`
- `frozen_1h_short@1.0.0`
- `frozen_4h_short@1.0.0`
- output contract: `canonical-strategy-order-plan-v1`
- state contract: `canonical-strategy-state-v1`

## Portfolio arbitration

Priority is Final 15m, then Frozen 1H, then Frozen 4H. The BTCUSDT portfolio permits one active limit and one open position. A higher-priority signal may cancel a lower-priority pending order before fill; no signal may preempt an open position. Data gaps invalidate pending orders and close positions at the first available conservative market price before indicator state is reset.

## Dataset lineage

New evidence must be generated from public Binance USDⓈ-M Futures klines using `https://fapi.binance.com` and `GET /fapi/v1/klines`, with BTCUSDT, 5m and UTC. This approval does not retroactively prove the source endpoint of the retained Drive files.

## Safety

Research and paper execution only. No private API, credentials, live trading, release, deployment, merge or Ready transition is authorized.
