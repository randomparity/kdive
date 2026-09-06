"""Stateful external-firewall boundary used only by the Ansible regression harness."""

import json
import os
from pathlib import Path

# Loaded by the pinned Ansible harness environment, outside the project Python environment.
from ansible.plugins.action import ActionBase  # ty: ignore[unresolved-import]


class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        result = super().run(tmp, task_vars)
        root = Path(os.environ["FAKE_AUTHORITY_FIREWALL_ROOT"])
        rules_path = root / "rules.json"
        rules = json.loads(rules_path.read_text()) if rules_path.exists() else []
        args = self._task.args.copy()
        remove = args.pop("state", "enabled") == "disabled" or args.pop("delete", False)
        for key in ("permanent", "immediate", "insert"):
            args.pop(key, None)
        rule = {"module": self._task.action, "args": args}
        before = rules.copy()
        if remove:
            rules = [entry for entry in rules if entry != rule]
        elif rule not in rules:
            rules.append(rule)
        rules_path.write_text(json.dumps(rules))
        with (root / "calls.jsonl").open("a") as output:
            output.write(json.dumps({"remove": remove, **rule}) + "\n")
        result["changed"] = before != rules
        grant = args.get("rule") == "allow" or args.get("rich_rule", "").endswith(" accept")
        if not remove and grant and os.environ.get("FAIL_AFTER_AUTHORITY_GRANT") == "1":
            result.update(failed=True, msg="controlled interruption after authority grant")
        return result
