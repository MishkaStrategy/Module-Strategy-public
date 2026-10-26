import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { refreshAcceptance } from "./AcceptancePage";

describe("refreshAcceptance", () => {
  it("invalidates durable state and the revision-aware component-health key family", () => {
    const queryClient = new QueryClient();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");

    refreshAcceptance(queryClient);

    expect(invalidate).toHaveBeenCalledTimes(2);
    expect(invalidate).toHaveBeenNthCalledWith(1, { queryKey: ["acceptance-current"] });
    expect(invalidate).toHaveBeenNthCalledWith(2, { queryKey: ["control-component-health"] });
  });
});
