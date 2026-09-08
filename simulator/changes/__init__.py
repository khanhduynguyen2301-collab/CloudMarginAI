"""Background (non-incident) deployment and configuration-change generators.

Feeds `deployments` and `resource_changes`. Incident-causing events are
injected separately by simulator/incidents/ using a reserved
changed_component/change_type vocabulary — see
docs/phase1/incident-injection-spec.md and the causal-event tagging
convention referenced there. Keep these two modules incident-agnostic.
"""
