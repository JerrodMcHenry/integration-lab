import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { OpsMetrics } from "../types";
import { HealthSummary } from "./HealthSummary";

const METRICS: OpsMetrics = {
  tenant_id: "meridian",
  events: { pending: 0, ready: 0, retry_backlog: 0, processing: 0, expired_leases: 0, failed: 0, dead_letter: 0,
            succeeded_1h: 0, oldest_pending_s: null, latency_p50_s: null, latency_p95_s: null, latency_max_s: null },
  workers: { healthy: 2, total_seen: 2, last_heartbeat_at: null },
};

afterEach(cleanup);

describe("HealthSummary", () => {
  it("shows 'Unavailable', never zeros, when metrics couldn't be loaded", () => {
    render(<HealthSummary metrics={null} error="IntegrationLab is temporarily unavailable." />);
    expect(screen.getByText("Unavailable")).toBeTruthy();
    expect(screen.queryByText("0")).toBeNull();
  });

  it("distinguishes 'not measured' latency from a zero count", () => {
    render(<HealthSummary metrics={METRICS} error={null} />);
    expect(screen.getByText("Not available")).toBeTruthy();
    expect(screen.getByText("No successful syncs in the last hour")).toBeTruthy();
    expect(screen.getAllByText("0").length).toBeGreaterThan(0); // real zero counts are still shown as 0
  });

  it("adds failed and dead-lettered events to 'Needs attention'", () => {
    render(<HealthSummary metrics={{ ...METRICS, events: { ...METRICS.events, failed: 1, dead_letter: 2 } }} error={null} />);
    expect(screen.getByText("3")).toBeTruthy();
    expect(screen.getByText("2 retries exhausted · 1 not retryable")).toBeTruthy();
  });
});
