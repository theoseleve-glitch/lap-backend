// T200 (sprint2-backend) — load test pass 2: 500 concurrent users.
//
// Pass 1 was T230 (ingest 50rps) + T231 (/me 500rps), each
// scoped to one endpoint and the offered-rate executor. Pass 2
// shifts the executor to `constant-vus` because the contract is
// 500 *concurrent users*, not 500 req/s — concurrency surfaces
// connection-pool, lock-contention, and DB-pool issues that
// arrival-rate tests don't.
//
// Endpoint mix is the cheap-read class only (per
// docs/error-budget.md): /me (auth+DB), /catalog/tracks
// (public-ish read), /coaching/rules (cheap rule list).
// Write/upload paths run on their own dedicated tests at T230/
// T231 with the heavier 500ms p95 budget. Mixing classes here
// would either dilute the signal (if averaged) or mask
// regressions (if alarms trip on the slower class).
//
// Run via:
//   k6 run \
//     -e BASE_URL=https://staging.lap-backend.example \
//     -e BEARER=<clerk-bearer> \
//     scripts/k6/all_endpoints_500vu.js
//
// Sprint contract: 500 VUs sustained for 1m, p95 <300ms across
// the cheap-read mix, error rate <1%. The 300ms is intentionally
// tighter than the 500ms heavy-read budget so a regression in
// the cheap path can't hide in the same threshold.

import http from 'k6/http';
import { check } from 'k6';

export const options = {
  scenarios: {
    cheap_reads_500vu: {
      executor: 'constant-vus',
      vus: 500,
      duration: '1m',
    },
  },
  thresholds: {
    // p95 latency across the cheap-read mix must stay under
    // 300ms — sprint pass-2 SLO.
    http_req_duration: ['p(95)<300'],
    // Defensive: fail the run if more than 1% of requests error.
    http_req_failed: ['rate<0.01'],
  },
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const BEARER = __ENV.BEARER || '';

const ENDPOINTS = [
  '/api/v1/me',
  '/api/v1/catalog/tracks',
  '/api/v1/coaching/rules',
];

export default function () {
  // Round-robin across the cheap-read mix per-iteration so
  // every endpoint sees roughly equal load. Picking by VU+iter
  // keeps the distribution deterministic regardless of how
  // long any one response takes.
  const idx = (__VU + __ITER) % ENDPOINTS.length;
  const url = `${BASE_URL}${ENDPOINTS[idx]}`;
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
