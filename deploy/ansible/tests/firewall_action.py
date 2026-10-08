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
        backend_path = root / "backend.json"
        backend = json.loads(backend_path.read_text())
        args = self._task.args
        redhat = self._task.action == "ansible.posix.firewalld"
        required = {"firewalld", "python3-firewall"} if redhat else {"ufw"}
        if not required <= set(backend["packages"]):
            return result | {"failed": True, "msg": "firewall backend packages absent"}
        if args.get("offline"):
            assert args["permanent"] and not args["immediate"]
            if os.environ.get("FAIL_BACKEND") == "management":
                return result | {"failed": True, "msg": "controlled offline failure"}
            changed = args["port"] not in backend["management"]
            backend["management"] = sorted(set(backend["management"]) | {args["port"]})
            backend_path.write_text(json.dumps(backend))
            return result | {"changed": changed}
        if redhat and not (backend["running"] and backend["enabled"]):
            return result | {"failed": True, "msg": "firewalld not enabled and running"}
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
