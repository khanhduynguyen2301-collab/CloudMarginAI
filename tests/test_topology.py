"""Working example test — topology.py is fully implemented, so this passes
today.
"""
from simulator.topology import INCIDENT_SERVICE_MAP, ORGANIZATION_ID, build_topology


def test_organization_id():
    org = build_topology()
    assert org.organization_id == ORGANIZATION_ID


def test_prod_has_six_services():
    org = build_topology()
    prod = next(p for p in org.projects if p.name == "prod")
    assert len(prod.services) == 6


def test_staging_has_two_services():
    org = build_topology()
    staging = next(p for p in org.projects if p.name == "staging")
    assert len(staging.services) == 2


def test_every_incident_service_exists_in_prod():
    org = build_topology()
    for incident_type, service_name in INCIDENT_SERVICE_MAP.items():
        resolved = org.get_service(service_name, project="prod")
        assert resolved.name == service_name
