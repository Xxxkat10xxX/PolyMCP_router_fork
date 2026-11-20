import pytest

from polymcp.agents.planner import PlanError, validate
from polymcp.contracts import Plan, PlanStep

SERVERS = ["postgres", "shell"]


def plan(*steps: tuple[int, str, list[int]]) -> Plan:
    return Plan(reasoning="", steps=[
        PlanStep(id=i, server=s, goal="g", tool_hint="h", depends_on=d) for i, s, d in steps
    ])


def test_valid_dag():
    assert len(validate(plan((1, "postgres", []), (2, "shell", [1]), (3, "shell", [])), SERVERS)) == 3


@pytest.mark.parametrize("bad", [
    plan((1, "github", [])),                       # неизвестный сервер
    plan((1, "postgres", [2]), (2, "shell", [])),  # зависимость вперёд
    plan((1, "postgres", []), (1, "shell", [])),   # дубликат id
    plan((1, "postgres", [1])),                    # зависимость от себя
    plan(),                                        # пустой план
])
def test_invalid_plans_rejected(bad):
    with pytest.raises(PlanError):
        validate(bad, SERVERS)


def test_replan_ids_continue_and_may_depend_on_done_steps():
    extra = plan((3, "shell", [1]))
    assert validate(extra, SERVERS, first_id=3, known_ids={1, 2})
    with pytest.raises(PlanError):
        validate(plan((2, "shell", [])), SERVERS, first_id=3, known_ids={1, 2})
