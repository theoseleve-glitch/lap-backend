// T230 — k6 load test for /api/v1/sessions/ingest at 50 rps.
//
// Run via:
//   k6 run \
//     -e BASE_URL=https://staging.lap-backend.example \
//     -e BEARER=<clerk-bearer> \
//     scripts/k6/sessions_ingest.js
//
// Sprint contract: 50 rps sustained for 1m, p95 latency <500ms.
// The threshold makes the load test fail fast on regression
// rather than passing silently with degraded perf.
//
// constant-arrival-rate is the right executor here: VUs scale
// to keep the rate steady regardless of how slow individual
// responses are, so the SLO measures behavior at offered load,
// not at "whatever load the server happened to handle."
//
// The dummy multipart body matches the ingest contract's shape
// without exercising real chunk validation — the goal is to
// measure the framework + auth path latency, not telemetry
// processing. T-future can extend with a representative payload.

import http from 'k6/http';
import { check } from 'k6';

export const options = {
  scenarios: {
    ingest_50rps: {
      executor: 'constant-arrival-rate',
      rate: 50,
      timeUnit: '1s',
      duration: '1m',
      preAllocatedVUs: 50,
      maxVUs: 200,
    },
  },
  thresholds: {
    // p95 latency must stay under 500ms — sprint SLO.
    http_req_duration: ['p(95)<500'],
    // Defensive: fail the run if more than 1% of requests error.
    http_req_failed: ['rate<0.01'],
  },
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const BEARER = __ENV.BEARER || '';

export default function () {
  const url = `${BASE_URL}/api/v1/sessions/ingest`;
  const payload = {
    manifest: JSON.stringify({
      schema_version: 1,
      session_uid: `k6-${__VU}-${__ITER}`,
      car_id: '00000000-0000-7000-8000-000000000001',
      track_id: '00000000-0000-7000-8000-000000000002',
      track_layout: 'gp',
      started_at: new Date().toISOString(),
      sample_rate_hz: 60,
      channels: ['speed', 'rpm', 'throttle', 'brake'],
      chunks: [],
    }),
  };
  const params = {
    headers: {
      Authorization: `Bearer ${BEARER}`,
    },
  };
  const res = http.post(url, payload, params);
  check(res, {
    'status is 2xx or 4xx (not 5xx)': (r) => r.status < 500,
  });
}
