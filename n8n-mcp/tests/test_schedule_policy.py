import unittest

from mcp_server import (
    _schedule_trigger_policy_violations,
    _validate_workflow_definition_body,
)


def _schedule_node(parameters, *, node_id="cron1", name="Cron"):
    return {
        "id": node_id,
        "name": name,
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1,
        "position": [0, 0],
        "parameters": parameters,
    }


class ScheduleTriggerPolicyTests(unittest.TestCase):
    def test_validate_rejects_minute_based_interval(self):
        valid, errors, warnings = _validate_workflow_definition_body(
            name="Test",
            nodes=[
                _schedule_node({"rule": {"interval": [{"field": "minutes", "minutesInterval": 30}]}}),
            ],
            connections={},
            settings={"executionOrder": "v1"},
        )

        self.assertFalse(valid)
        self.assertTrue(any("more often than every 2 hours" in error for error in errors), errors)
        self.assertTrue(warnings)

    def test_validate_rejects_one_hour_interval(self):
        valid, errors, _ = _validate_workflow_definition_body(
            name="Test",
            nodes=[
                _schedule_node({"rule": {"interval": [{"field": "hours", "hoursInterval": 1}]}}),
            ],
            connections={},
            settings={"executionOrder": "v1"},
        )

        self.assertFalse(valid)
        self.assertTrue(any("more often than every 2 hours" in error for error in errors), errors)

    def test_validate_allows_two_hour_interval(self):
        valid, errors, _ = _validate_workflow_definition_body(
            name="Test",
            nodes=[
                _schedule_node({"rule": {"interval": [{"field": "hours", "hoursInterval": 2}]}}),
            ],
            connections={},
            settings={"executionOrder": "v1"},
        )

        self.assertTrue(valid, errors)
        self.assertEqual([], errors)

    def test_validate_rejects_cron_every_minute(self):
        valid, errors, _ = _validate_workflow_definition_body(
            name="Test",
            nodes=[
                _schedule_node({"rule": {"cronExpression": "* * * * *"}}),
            ],
            connections={},
            settings={"executionOrder": "v1"},
        )

        self.assertFalse(valid)
        self.assertTrue(any("more often than every 2 hours" in error for error in errors), errors)

    def test_validate_allows_cron_every_two_hours(self):
        valid, errors, _ = _validate_workflow_definition_body(
            name="Test",
            nodes=[
                _schedule_node({"rule": {"cronExpression": "0 */2 * * *"}}),
            ],
            connections={},
            settings={"executionOrder": "v1"},
        )

        self.assertTrue(valid, errors)
        self.assertEqual([], errors)

    def test_update_check_ignores_unchanged_legacy_schedule(self):
        node = _schedule_node({"rule": {"interval": [{"field": "minutes", "minutesInterval": 1}]}})

        violations = _schedule_trigger_policy_violations([node], previous_nodes=[node])

        self.assertEqual([], violations)

    def test_update_check_rejects_changed_invalid_schedule(self):
        previous = _schedule_node({"rule": {"interval": [{"field": "hours", "hoursInterval": 6}]}})
        current = _schedule_node({"rule": {"interval": [{"field": "hours", "hoursInterval": 1}]}})

        violations = _schedule_trigger_policy_violations([current], previous_nodes=[previous])

        self.assertTrue(violations)
        self.assertIn("more often than every 2 hours", violations[0])


if __name__ == "__main__":
    unittest.main()
