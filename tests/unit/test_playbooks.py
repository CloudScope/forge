"""
Static integrity of the shipped playbooks.

A playbook is executable configuration: a broken dependency or a missing stage is a
production defect, and it should fail here rather than halfway through a run.
"""

from __future__ import annotations

import yaml
import pytest

from forge.agents import REGISTRY
from forge.core.paths import paths as forge_paths
from forge.models import NodeType, RiskTier

PLAYBOOK_FILES = sorted(forge_paths().playbooks.glob("*.yaml"))
PLAYBOOK_IDS = [p.stem for p in PLAYBOOK_FILES]


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(params=PLAYBOOK_FILES, ids=PLAYBOOK_IDS)
def playbook(request):
    return load(request.param)


def test_playbooks_are_discovered():
    assert PLAYBOOK_FILES, "no playbooks found"


class TestSchema:
    def test_has_identity_and_nodes(self, playbook):
        assert playbook["id"]
        assert playbook["nodes"]

    def test_node_ids_are_unique(self, playbook):
        ids = [n["id"] for n in playbook["nodes"]]
        assert len(ids) == len(set(ids))

    def test_declared_types_are_valid(self, playbook):
        for node in playbook["nodes"]:
            NodeType(node.get("type", "COMPUTE"))

    def test_declared_risk_tiers_are_valid(self, playbook):
        for node in playbook["nodes"]:
            RiskTier(node.get("risk_tier", "LOW"))

    def test_every_agent_is_registered(self, playbook):
        for node in playbook["nodes"]:
            agent = node.get("agent", "barrier")
            assert agent in REGISTRY, f"{node['id']} references unknown agent {agent!r}"


class TestGraphIntegrity:
    def test_every_dependency_exists(self, playbook):
        ids = {n["id"] for n in playbook["nodes"]}
        for node in playbook["nodes"]:
            for dep in node.get("deps", []):
                assert dep in ids, f"{node['id']} depends on missing node {dep!r}"

    def test_no_node_depends_on_itself(self, playbook):
        for node in playbook["nodes"]:
            assert node["id"] not in node.get("deps", [])

    def test_the_graph_is_acyclic(self, playbook):
        deps = {n["id"]: list(n.get("deps", [])) for n in playbook["nodes"]}
        resolved: set[str] = set()
        # Repeatedly peel off nodes whose dependencies are all resolved.
        progress = True
        while progress:
            progress = False
            for node_id, node_deps in deps.items():
                if node_id in resolved:
                    continue
                if all(d in resolved for d in node_deps):
                    resolved.add(node_id)
                    progress = True
        unresolved = set(deps) - resolved
        assert not unresolved, f"cycle or unreachable nodes: {sorted(unresolved)}"

    def test_there_is_at_least_one_entry_node(self, playbook):
        roots = [n["id"] for n in playbook["nodes"] if not n.get("deps")]
        assert roots, "no entry node — nothing can ever become ready"


class TestGovernanceCoverage:
    def test_approval_nodes_use_the_human_agent(self, playbook):
        for node in playbook["nodes"]:
            if node.get("type") == "APPROVAL":
                assert node.get("agent") == "human_approval"

    def test_approval_nodes_are_high_risk(self, playbook):
        """A gate worth stopping for is never LOW risk."""
        for node in playbook["nodes"]:
            if node.get("type") == "APPROVAL":
                tier = node.get("risk_tier", "LOW")
                assert tier in {"HIGH", "CRITICAL"}, f"{node['id']} is {tier}"


class TestGatePrerequisites:
    """
    A playbook containing an automated validation stage must also contain the
    agents whose artifacts its blocking gates require. Regression guard: the
    `ambiguous` playbook shipped without a security_scan node and could therefore
    never pass its own `sec.scan` gate.
    """

    # gate → agent that publishes the artifact it checks
    REQUIRED_PRODUCERS = {
        "sec.scan": "security_scan",
        "sec.threat_model": "security",
        "code.test_scaffold": "testing",
        "risk.register_current": ("planner", "risk"),
        "perf.budget": ("architecture", "performance"),
        "arch.capacity_present": "architecture",
    }

    # Gates that `validation.evaluate` deliberately skips for a given stage.
    # Stage selection mirrors `OrchestrationEngine._run_validate`.
    STAGE_EXEMPTIONS = {"brownfield": {"sec.scan"}}

    @staticmethod
    def _stage(playbook) -> str:
        playbook_id = playbook["id"]
        if "brownfield" in playbook_id:
            return "brownfield"
        if "production_sdlc" in playbook_id:
            return "quality_gate"
        return "pre_release"

    def test_validation_stage_has_its_producers(self, playbook):
        node_agents = {n.get("agent", "barrier") for n in playbook["nodes"]}
        has_validation = any(
            n["id"].startswith("validate.") for n in playbook["nodes"]
        )
        if not has_validation:
            pytest.skip("playbook has no automated validation stage")

        exempt = self.STAGE_EXEMPTIONS.get(self._stage(playbook), set())
        for gate, producer in self.REQUIRED_PRODUCERS.items():
            if gate in exempt:
                continue
            options = (producer,) if isinstance(producer, str) else producer
            assert node_agents & set(options), (
                f"gate {gate} requires one of {options}, "
                f"but the playbook runs none of them"
            )

    def test_validation_runs_before_the_release_gate(self, playbook):
        """Go/No-Go must not be reachable without the automated gates first."""
        nodes = {n["id"]: n for n in playbook["nodes"]}
        release = next(
            (n for n in playbook["nodes"] if n["id"].startswith("approval.release")),
            None,
        )
        if release is None:
            pytest.skip("playbook has no release gate")

        seen: set[str] = set()
        stack = list(release.get("deps", []))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(nodes.get(current, {}).get("deps", []))

        assert any(dep.startswith("validate.") for dep in seen), (
            "release gate does not depend on the automated validation stage"
        )
