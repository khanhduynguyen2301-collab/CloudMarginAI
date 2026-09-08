"""The fixed org/project/service/region graph for org_demo.

Transcribed exactly from docs/phase1/simulator-architecture.md, "Topology
decision" — one organization, fixed topology for v1. This file is fully
implemented: it's a direct transcription of an already-made decision, not
a design choice left open for the generators.

If you need a different topology (more services, a second org, etc.),
that's a doc change first (docs/phase1/simulator-architecture.md), then a
code change here — never the other way around.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ResourceKind(str, Enum):
    """How a service's capacity relates to demand — see workloads/capacity.py."""

    REQUEST_SERVING = "request_serving"  # instance_count tracks requests via autoscaling
    WORKER_QUEUE = "worker_queue"  # queue-driven, not directly request-rate-scaled
    BATCH_ACCELERATOR = "batch_accelerator"  # fixed schedule, not demand-driven (ml-training-job)


@dataclass(frozen=True)
class Service:
    name: str
    project: str  # "prod" | "staging"
    resource_kind: ResourceKind
    regions: tuple[str, ...]


@dataclass(frozen=True)
class Project:
    name: str
    services: tuple[Service, ...]


@dataclass(frozen=True)
class Organization:
    organization_id: str
    projects: tuple[Project, ...]

    def all_services(self) -> tuple[Service, ...]:
        return tuple(s for p in self.projects for s in p.services)

    def get_service(self, name: str, project: str = "prod") -> Service:
        for s in self.all_services():
            if s.name == name and s.project == project:
                return s
        raise KeyError(f"no service {name!r} in project {project!r}")


ORGANIZATION_ID = "org_demo"

REGIONS: tuple[str, ...] = ("us-central1", "us-east1", "europe-west1")

# prod: all 6 services. staging: a 2-service subset for noise/contrast
# (simulator-architecture.md doesn't pin exactly which 2 — web-frontend and
# checkout-api are the most representative pair: one pure request-serving
# frontend, one request-serving service with a backing DB).
_PROD_SERVICE_DEFS: tuple[tuple[str, ResourceKind], ...] = (
    ("checkout-api", ResourceKind.REQUEST_SERVING),
    ("recommendation-api", ResourceKind.REQUEST_SERVING),
    ("ingestion-worker", ResourceKind.WORKER_QUEUE),
    ("billing-worker", ResourceKind.WORKER_QUEUE),
    ("ml-training-job", ResourceKind.BATCH_ACCELERATOR),
    ("web-frontend", ResourceKind.REQUEST_SERVING),
)
_STAGING_SERVICE_NAMES: tuple[str, ...] = ("web-frontend", "checkout-api")


def build_topology() -> Organization:
    """Construct the fixed org_demo topology.

    prod: checkout-api, recommendation-api, ingestion-worker,
    billing-worker, ml-training-job, web-frontend (6).
    staging: web-frontend, checkout-api (2) — mirrors a subset of prod for
    noise/contrast, per simulator-architecture.md.
    """
    prod_services = tuple(
        Service(name=name, project="prod", resource_kind=kind, regions=REGIONS)
        for name, kind in _PROD_SERVICE_DEFS
    )
    prod_by_name = {s.name: s for s in prod_services}
    staging_services = tuple(
        Service(
            name=name,
            project="staging",
            resource_kind=prod_by_name[name].resource_kind,
            regions=REGIONS,
        )
        for name in _STAGING_SERVICE_NAMES
    )
    return Organization(
        organization_id=ORGANIZATION_ID,
        projects=(
            Project(name="prod", services=prod_services),
            Project(name="staging", services=staging_services),
        ),
    )


# Each incident type has exactly one home service, per
# docs/phase1/incident-injection-spec.md, "The four incident types,
# operationalized". Always resolved against the "prod" project.
INCIDENT_SERVICE_MAP: dict[str, str] = {
    "logging_regression": "ingestion-worker",
    "query_regression": "checkout-api",
    "idle_accelerator": "ml-training-job",
    "autoscaling_error": "recommendation-api",
}
