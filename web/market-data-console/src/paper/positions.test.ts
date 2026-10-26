import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  EMPTY_PAPER_POSITION_FILTERS,
  PaperPositionsPanel,
  PaperPositionsResults,
  filterPaperPositions,
  paperPositionFiltersReducer,
  serializePaperPositionsSnapshot,
} from "./PaperRuntimePage";
import type { PaperPositionFilters } from "./PaperRuntimePage";
import type { PaperPositionsAvailableSnapshot, PaperPositionsSnapshot } from "./contracts";

const SNAPSHOT: PaperPositionsAvailableSnapshot = {
  schema_version: 1,
  status: "available",
  owner_id: "paper-execution",
  composition_id: "composition-a",
  portfolio_id: "portfolio-a",
  owner_generation: "generation-1",
  membership_version: "membership-4",
  snapshot_version: "snapshot-8",
  state_version: "state-12",
  as_of: "2026-10-23T05:00:00Z",
  coverage: "complete",
  positions: [
    {
      schema_version: 1,
      owner_position_id: "position-a",
      instrument_id: "venue:ABC",
      instrument_version: "instrument-1",
      side: "long",
      quantity: "1.25",
      quantity_unit: "contracts",
      strategy_id: "strategy-a",
    },
    {
      schema_version: 1,
      owner_position_id: "position-b",
      instrument_id: "venue:ABC",
      instrument_version: "instrument-1",
      side: "short",
      quantity: "2",
      quantity_unit: "contracts",
      strategy_id: null,
    },
    {
      schema_version: 1,
      owner_position_id: "position-c",
      instrument_id: "venue:XYZ",
      instrument_version: "instrument-2",
      side: "long",
      quantity: "3",
      quantity_unit: "contracts",
      strategy_id: "strategy-b",
    },
  ],
};

describe("PAPER positions filters", () => {
  it("filters only by row symbol, side, and known owner attribution", () => {
    const filters: PaperPositionFilters = {
      symbol: "abc",
      side: "long",
      strategy: "known:strategy-a",
    };
    expect(filterPaperPositions(SNAPSHOT.positions, filters).map((row) => row.owner_position_id))
      .toEqual(["position-a"]);
  });

  it("renders zero matches while retaining the full snapshot count and status", () => {
    const html = renderToStaticMarkup(createElement(
      PaperPositionsPanel,
      { positions: SNAPSHOT, loading: false, failed: false },
    ));
    const noMatches = renderToStaticMarkup(createElement(PaperPositionsResults, {
      positions: SNAPSHOT.positions,
      matchingRows: [],
    }));
    expect(html).toContain("Snapshot available · 3 owner positions");
    expect(html).toContain("value=\"known:strategy-a\"");
    expect(html).toContain("Unknown attribution");
    expect(html).toContain("Clear filters");
    expect(noMatches).toContain("Showing 0 matching rows from 3 owner positions.");
    expect(noMatches).toContain("No positions match the current filters.");
  });

  it("shows the separate matching count for a complete empty snapshot", () => {
    const html = renderToStaticMarkup(createElement(PaperPositionsResults, {
      positions: [],
      matchingRows: [],
    }));
    expect(html).toContain("Showing 0 matching rows from 0 owner positions.");
    expect(html).toContain("No open positions.");
  });

  it("keeps missing strategy attribution explicit and filterable as unknown", () => {
    const unknownRows = filterPaperPositions(SNAPSHOT.positions, {
      ...EMPTY_PAPER_POSITION_FILTERS,
      strategy: "unknown",
    });
    expect(unknownRows.map((row) => row.owner_position_id)).toEqual(["position-b"]);
    const html = renderToStaticMarkup(createElement(PaperPositionsResults, {
      positions: SNAPSHOT.positions,
      matchingRows: unknownRows,
    }));
    expect(html).toContain("Unknown attribution (owner supplied no strategy ID)");
    expect(html).toContain("Showing 1 matching rows from 3 owner positions.");
  });

  it("resets symbol, side, and attribution filters together", () => {
    const active: PaperPositionFilters = {
      symbol: "ABC",
      side: "short",
      strategy: "unknown",
    };
    expect(paperPositionFiltersReducer(active, { type: "reset" })).toEqual(
      EMPTY_PAPER_POSITION_FILTERS,
    );
  });
});

describe("PAPER positions snapshot export", () => {
  it("preserves the exact complete owner snapshot and unknown attribution", () => {
    const exported = JSON.parse(serializePaperPositionsSnapshot(SNAPSHOT));

    expect(exported).toEqual(SNAPSHOT);
    expect(exported.positions[1].strategy_id).toBeNull();
    expect(exported).toMatchObject({
      owner_id: SNAPSHOT.owner_id,
      composition_id: SNAPSHOT.composition_id,
      portfolio_id: SNAPSHOT.portfolio_id,
      owner_generation: SNAPSHOT.owner_generation,
      membership_version: SNAPSHOT.membership_version,
      snapshot_version: SNAPSHOT.snapshot_version,
      state_version: SNAPSHOT.state_version,
      as_of: SNAPSHOT.as_of,
      coverage: "complete",
    });
  });

  it("exports an explicit complete empty snapshot", () => {
    const emptySnapshot = { ...SNAPSHOT, positions: [] };

    expect(JSON.parse(serializePaperPositionsSnapshot(emptySnapshot))).toEqual(emptySnapshot);
  });

  it("exports all snapshot rows independently of active table filters", () => {
    const filters = {
      ...EMPTY_PAPER_POSITION_FILTERS,
      symbol: "venue:XYZ",
      side: "long" as const,
      strategy: "known:strategy-b" as const,
    };
    expect(filterPaperPositions(SNAPSHOT.positions, filters).map((row) => row.owner_position_id))
      .toEqual(["position-c"]);

    const exported = JSON.parse(serializePaperPositionsSnapshot(SNAPSHOT));
    expect(exported.positions.map((row: { owner_position_id: string }) => row.owner_position_id))
      .toEqual(["position-a", "position-b", "position-c"]);
  });

  it.each([
    ["unavailable", { schema_version: 1, status: "unavailable", reason_code: "owner_missing" }],
    ["recovering", { schema_version: 1, status: "recovering", reason_code: "owner_recovering" }],
    ["stale", { schema_version: 1, status: "stale", reason_code: "snapshot_stale" }],
    ["incomplete", { ...SNAPSHOT, coverage: "partial" }],
  ] as const)("disables export for %s snapshots", (_label, snapshot) => {
    const html = renderToStaticMarkup(createElement(PaperPositionsPanel, {
      positions: snapshot as unknown as PaperPositionsSnapshot,
      loading: false,
      failed: false,
    }));

    expect(html).toContain("Download complete snapshot (JSON)");
    expect(html).toContain("disabled=\"\"");
  });

  it.each([
    ["loading", true, false],
    ["failed refresh", false, true],
  ])("disables export during %s", (_label, loading, failed) => {
    const html = renderToStaticMarkup(createElement(PaperPositionsPanel, {
      positions: SNAPSHOT,
      loading,
      failed,
    }));

    expect(html).toContain("disabled=\"\"");
  });
});
