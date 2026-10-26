# Architecture

The component is deliberately separate from Strategy Factory and Trading Runner. Strategy Factory may select and package it; Trading Runner only schedules it through Core's dependency planner.

## Safety rules

- only current closed-bar features are accepted;
- entry and protection identities, symbol, side, setup and price must agree;
- instrument and portfolio evidence are mandatory by default;
- request IDs are content-derived and duplicate-safe;
- state is bounded, hash-protected and configuration-bound;
- market entries use the current closed-bar close as an explicit research/paper assumption;
- no live execution, risk sizing, portfolio mutation or order creation occurs here.
