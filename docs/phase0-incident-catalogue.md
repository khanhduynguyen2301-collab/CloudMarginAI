# CloudMargin AI — Incident Catalogue v1 (Phase 0)

Status: draft for review · Source: Blueprint v1.2, Section 8 & Appendix E ("Finalize the first four incident types and their ground-truth labels")
Phase: 0 (Product specification) · Weeks 1–2

These are the incidents Phase 1's simulator injects, and what Phase 2–3's detector and root-cause ranker are graded against. Each needs a ground-truth label: **start time, affected service, expected magnitude** — recorded at injection time so evaluation can check the model without asking a human to re-judge every run.

## Chosen for v1

### 1. Logging regression
- **Injected change:** enable verbose logs after a deployment
- **Expected signature:** log bytes and ingestion cost rise; traffic stays mostly flat
- **Safe remediation:** restore logging level
- **Ground truth to record:** injection timestamp, affected service, expected log-byte multiplier

### 2. Query regression
- **Injected change:** remove an index or introduce a duplicate query path
- **Expected signature:** queries/request, latency, and database CPU all rise alongside cost
- **Safe remediation:** rollback the query change
- **Ground truth to record:** injection timestamp, affected service, expected latency/cost delta

### 3. Idle accelerator
- **Injected change:** leave a GPU-like workload active with nothing to do
- **Expected signature:** low utilization with persistent compute cost
- **Safe remediation:** stop or schedule the resource
- **Ground truth to record:** injection timestamp, affected resource, expected idle-cost rate

### 4. Autoscaling error
- **Injected change:** raise the minimum replica count
- **Expected signature:** instance count rises while utilization falls
- **Safe remediation:** restore the scaling policy
- **Ground truth to record:** injection timestamp, affected service, expected instance-count delta

## Held for later expansion

| Incident | Injected change | Expected signature |
|---|---|---|
| Traffic abuse | Generate bot/API-key traffic | Requests and egress rise without paid-user growth |
| Storage / log retention | Disable lifecycle policy | Stored bytes and snapshot count accelerate |

## Why these four

They each have a clean, close-to-single-cause signature and a safe, reversible remediation — which matters because Phase 3's root-cause ranker is graded on placing the true cause in the top three candidates, and a noisy ground truth makes that evaluation meaningless. Traffic abuse and storage/retention are held back because they interact more with business/demand signals (traffic abuse can look like real growth) and are better added once the core four are detected reliably.

## Sign-off

- [ ] Four incident types confirmed (swap any of the above if a different set fits better)
- [ ] Ground-truth label format agreed (matches the fields listed under each incident)
- [ ] Each has a documented safe remediation + rollback before Phase 7 (action validation) needs it
