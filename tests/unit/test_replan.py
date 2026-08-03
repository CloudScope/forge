"""Dynamic re-planning: impact analysis when an upstream decision changes."""

from __future__ import annotations

from forge.models import TaskStatus
from forge.replan import descendants, graft_nodes, invalidate
from tests.conftest import make_node


def _diamond(make_workflow):
    """root → (left, right) → join"""
    return make_workflow(
        [
            make_node("root"),
            make_node("left", deps=["root"]),
            make_node("right", deps=["root"]),
            make_node("join", deps=["left", "right"]),
            make_node("unrelated"),
        ]
    )


class TestImpactAnalysis:
    def test_descendants_walks_the_whole_downstream_cone(self, make_workflow):
        wf = _diamond(make_workflow)

        assert descendants(wf, ["root"]) == {"root", "left", "right", "join"}

    def test_descendants_of_a_leaf_is_just_itself(self, make_workflow):
        wf = _diamond(make_workflow)

        assert descendants(wf, ["join"]) == {"join"}

    def test_unrelated_branches_are_excluded(self, make_workflow):
        wf = _diamond(make_workflow)

        assert "unrelated" not in descendants(wf, ["left"])


class TestInvalidation:
    def test_impacted_nodes_are_reset_for_re_execution(self, make_workflow):
        wf = _diamond(make_workflow)
        for t in wf.tasks.values():
            t.status = TaskStatus.SUCCEEDED
        wf.tasks["left"].error = "stale"
        wf.tasks["left"].outputs = {"x": 1}

        impacted, preserved = invalidate(wf, ["left"])

        assert impacted == {"left", "join"}
        assert wf.tasks["left"].status == TaskStatus.PENDING
        assert wf.tasks["join"].status == TaskStatus.PENDING
        assert wf.tasks["left"].error is None
        assert wf.tasks["left"].outputs == {}

    def test_untouched_successes_are_preserved(self, make_workflow):
        wf = _diamond(make_workflow)
        for t in wf.tasks.values():
            t.status = TaskStatus.SUCCEEDED

        _, preserved = invalidate(wf, ["left"])

        assert preserved == {"root", "right", "unrelated"}
        assert wf.tasks["right"].status == TaskStatus.SUCCEEDED

    def test_artifact_history_survives_invalidation(self, make_workflow):
        """Decision lineage is append-only — re-planning must not erase it."""
        from forge.agents._common import publish

        wf = _diamond(make_workflow)
        wf.tasks["left"].status = TaskStatus.SUCCEEDED
        publish(wf, wf.tasks["left"], "hld", {"v": 1})

        invalidate(wf, ["left"])

        assert len(wf.artifact_history) == 1
        assert wf.artifact_history[0].content == {"v": 1}


class TestGrafting:
    def test_new_nodes_join_the_running_graph(self, engine, make_workflow):
        wf = _diamond(make_workflow)

        graft_nodes(wf, [make_node("extra", deps=["root"])])

        assert "extra" in wf.tasks
        assert descendants(wf, ["root"]) >= {"extra"}

    def test_grafted_nodes_become_schedulable(self, engine, make_workflow):
        wf = make_workflow([make_node("root")])
        wf.tasks["root"].status = TaskStatus.SUCCEEDED

        graft_nodes(wf, [make_node("extra", deps=["root"])])

        assert [n.id for n in engine.ready_nodes(wf)] == ["extra"]


class TestSecurityReplan:
    def test_replan_targets_security_and_build_nodes_by_agent(self, engine, make_workflow):
        """Replan must find nodes by role, not by playbook-specific ids."""
        wf = make_workflow(
            [
                make_node("security.review", agent="security"),
                make_node("scan.stage", agent="security_scan", deps=["security.review"]),
                make_node("be.build", agent="backend", deps=["scan.stage"]),
                make_node("docs", agent="documentation"),
            ]
        )
        for t in wf.tasks.values():
            t.status = TaskStatus.SUCCEEDED

        engine._replan_security_failure(wf)

        assert wf.tasks["security.review"].status == TaskStatus.PENDING
        assert wf.tasks["scan.stage"].status == TaskStatus.PENDING
        assert wf.tasks["be.build"].status == TaskStatus.PENDING
        assert wf.tasks["docs"].status == TaskStatus.SUCCEEDED

    def test_replan_runs_at_most_once(self, engine, make_workflow):
        wf = make_workflow([make_node("security.review", agent="security")])
        wf.tasks["security.review"].status = TaskStatus.SUCCEEDED

        engine._replan_security_failure(wf)
        wf.tasks["security.review"].status = TaskStatus.SUCCEEDED
        engine._replan_security_failure(wf)

        assert wf.tasks["security.review"].status == TaskStatus.SUCCEEDED

    def test_replan_never_reopens_human_approvals(self, engine, make_workflow):
        from forge.models import NodeType

        wf = make_workflow(
            [
                make_node("security.review", agent="security"),
                make_node(
                    "approval.arch",
                    agent="human_approval",
                    node_type=NodeType.APPROVAL,
                    deps=["security.review"],
                ),
            ]
        )
        for t in wf.tasks.values():
            t.status = TaskStatus.SUCCEEDED

        engine._replan_security_failure(wf)

        assert wf.tasks["approval.arch"].status == TaskStatus.SUCCEEDED, (
            "re-planning must not silently discard a human decision"
        )

    def test_replan_is_audited_with_impact_and_preserved_sets(self, engine, make_workflow):
        wf = make_workflow([make_node("security.review", agent="security")])
        wf.tasks["security.review"].status = TaskStatus.SUCCEEDED

        engine._replan_security_failure(wf)

        event = next(e for e in wf.events if e["type"] == "REPLAN_SECURITY")
        assert "impacted" in event["payload"]
        assert "preserved" in event["payload"]
