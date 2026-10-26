import { describe, expect, it } from "vitest";

import {
  consumeJobEventStream,
  parseJobEvent,
  parseSseFrame,
  splitSseFrames,
} from "./jobEvents";

const eventPayload = (eventId: number) =>
  JSON.stringify({
    schema_version: 1,
    event_id: eventId,
    job_id: "mdi_job_test",
    event_type: "state",
    state: "DOWNLOADING",
    timestamp: "2026-07-29T00:00:00Z",
    payload: { message: "ok" },
  });

describe("SSE frame parsing", () => {
  it("keeps an incomplete trailing frame", () => {
    const parsed = splitSseFrames(`id: 1\ndata: ${eventPayload(1)}\n\nid: 2\ndata:`);
    expect(parsed.frames).toHaveLength(1);
    expect(parsed.remainder).toBe("id: 2\ndata:");
  });

  it("joins multiline data fields", () => {
    const frame = parseSseFrame("id: 7\nevent: log\ndata: first\ndata: second");
    expect(frame).toEqual({ id: "7", event: "log", data: "first\nsecond" });
  });

  it("rejects malformed job events", () => {
    expect(() => parseJobEvent({ id: "1", event: "state", data: "{}" })).toThrow(
      "does not match the v1 contract",
    );
  });
});

describe("consumeJobEventStream", () => {
  it("decodes ordered events from a fetch response", async () => {
    const response = new Response(
      `id: 1\nevent: state\ndata: ${eventPayload(1)}\n\n` +
        `id: 2\nevent: state\ndata: ${eventPayload(2)}\n\n`,
      { headers: { "Content-Type": "text/event-stream" } },
    );
    const received: number[] = [];
    const count = await consumeJobEventStream(
      response,
      (event) => received.push(event.event_id),
      new AbortController().signal,
    );
    expect(count).toBe(2);
    expect(received).toEqual([1, 2]);
  });
});
