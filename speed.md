# Performance Analysis

## Benchmark: /shop page (March 3, 2026)

Measured with curl from the server (eliminates client network latency).

| Metric | Our prod | Old site (kaminfeger.ch) |
|---|---|---|
| First byte (avg) | ~500ms | ~170ms |
| HTML size | 283 KB | 98 KB |

Our server-side rendering is ~3x slower. The bottleneck is Python + DB + template rendering, not assets or network.

## Observations

- HTML output is 3x larger — Odoo may be rendering more products/markup or sending unminified HTML
- First byte time is pure backend work (curl runs on the same server)
- Assets (JS/CSS) are not the issue — the delay is before any assets load

## Optimization ideas (not yet implemented)

### PostgreSQL tuning
Default config is very conservative. Tuning shared_buffers, effective_cache_size, work_mem for 4 GB RAM / SSD would help every query.

### Nginx static asset caching + gzip
Cache JS/CSS/images at Nginx level and enable gzip compression. Helps repeat visits and overall page weight, but won't improve first-byte time.

### HTML size investigation
Figure out why our /shop returns 283 KB vs 98 KB. Could be more products loaded per page, heavier Odoo markup, or inline assets.

### Longpolling routing
Verify Nginx routes /longpolling and /websocket to the gevent port (8070) instead of main workers, so they don't block page requests.
