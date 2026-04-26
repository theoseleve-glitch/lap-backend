// T231 — k6 load test for driver profile read at 500 rps.
//
// Sprint label "/driver/profile" maps to the actual route
// `GET /api/v1/me` — the cheap-read endpoint for driver-self
// data. No new endpoint added; this benchmarks the existing
// framework + auth + simple-read path.
//
// Run via:
//   k6 run \
//     -e BASE_URL=https://staging.lap-backend.example \
//     -e BEARER=<clerk-bearer> \
//     scripts/k6/driver_profile.js
//
// Sprint contract: 500 rps sustained for 1m, p95 latency
// <200ms. Tighter than the ingest budget (T230) because this
// is a single-row read with no upload work — if /me can't hold
// 200ms p95 at 500rps, the framework or auth path is the
// bottleneck and that needs investigation, not compensation.
//
// constant-arrival-rate: VUs scale to keep the offered rate
// steady regardless of response latency, so the SLO measures
// what users see, not what the server happened to handle.

import http from 'k6/http';
import { check } from 'k6';

export const options = {
  scenarios: {
    profile_500rps: {
      executor: 'constant-arrival-rate',
      rate: 500,
      timeUnit: '1s',
      duration: '1m',
      preAllocatedVUs: 200,
      maxVUs: 1000,
    },
  },
  thresholds: {
    // p95 latency must stay under 200ms — sprint SLO.
    http_req_duration: ['p(95)<200'],
    // Defensive: fail the run if more than 1% of requests error.
    http_req_failed: ['rate<0.01'],
  },
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const BEARER = __ENV.BEARER || '';

export default function () {
  const url = `${BASE_URL}/api/v1/me`;
  const params = {
    headers: {
      Authorization: `Bearer ${BEARER}`,
    },
  };
  const res = http.get(url, params);
  check(res, {
    'status is 2xx or 4xx (not 5xx)': (r) => r.status < 500,
  });
}
