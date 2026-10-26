import type { JobEvent } from "./contracts";

const MAX_SSE_BUFFER_BYTES = 1_048_576;

export interface SseFrame {
  id: string | undefined;
  event: string | undefined;
  data: string;
}

export function splitSseFrames(buffer: string): { frames: string[]; remainder: string } {
  const frames: string[] = [];
  let remainder = buffer;
  let boundary = remainder.indexOf("\n\n");
  while (boundary >= 0) {
    frames.push(remainder.slice(0, boundary));
    remainder = remainder.slice(boundary + 2);
    boundary = remainder.indexOf("\n\n");
  }
  return { frames, remainder };
}

export function parseSseFrame(raw: string): SseFrame | null {
  let id: string | undefined;
  let event: string | undefined;
  const data: string[] = [];

  for (const line of raw.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    let value = separator < 0 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "id") id = value;
    else if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }

  if (data.length === 0) return null;
  return { id, event, data: data.join("\n") };
}

export function parseJobEvent(frame: SseFrame): JobEvent {
  const value: unknown = JSON.parse(frame.data);
  if (!value || typeof value !== "object") throw new Error("SSE job event must be an object");
  const candidate = value as Partial<JobEvent>;
  if (
    !Number.isInteger(candidate.event_id) ||
    typeof candidate.job_id !== "string" ||
    typeof candidate.event_type !== "string" ||
    typeof candidate.state !== "string" ||
    typeof candidate.timestamp !== "string" ||
    !candidate.payload ||
    typeof candidate.payload !== "object" ||
    Array.isArray(candidate.payload)
  ) {
    throw new Error("SSE job event does not match the v1 contract");
  }
  return candidate as JobEvent;
}

export async function consumeJobEventStream(
  response: Response,
  onEvent: (event: JobEvent) => void,
  signal: AbortSignal,
): Promise<number> {
  if (!response.body) throw new Error("SSE response body is unavailable");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let count = 0;

  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      if (buffer.length > MAX_SSE_BUFFER_BYTES) {
        throw new Error("SSE buffer exceeded the 1 MiB safety limit");
      }
      const parsed = splitSseFrames(buffer);
      buffer = parsed.remainder;
      for (const raw of parsed.frames) {
        const frame = parseSseFrame(raw);
        if (!frame) continue;
        onEvent(parseJobEvent(frame));
        count += 1;
      }
      if (done) break;
    }
  } finally {
    await reader.cancel().catch(() => undefined);
  }

  return count;
}

export function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const timeout = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    const onAbort = () => {
      window.clearTimeout(timeout);
      reject(new DOMException("Aborted", "AbortError"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
