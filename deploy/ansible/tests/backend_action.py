"""Isolated package/service effects for the real firewall task harness."""

import json
import os
from pathlib import Path

from ansible.plugins.action import ActionBase  # ty: ignore[unresolved-import]


class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        result = super().run(tmp, task_vars)
        root = Path(os.environ["FAKE_AUTHORITY_FIREWALL_ROOT"])
        path = root / "backend.json"
        state = json.loads(path.read_text())
        before = state.copy()
        args = self._task.args.copy()
        operation = args.pop("operation")
        with (root / "backend-calls.jsonl").open("a") as output:
            output.write(json.dumps({"operation": operation, "args": args}) + "\n")
        if os.environ.get("FAIL_BACKEND") == operation and (
            operation != "systemd_service" or args["name"] == "firewalld"
        ):
            return result | {"failed": True, "msg": "controlled backend failure"}
        if operation == "package":
            names = args["name"]
            names = set(names if isinstance(names, list) else [names])
            installed = set(state["packages"])
            state["packages"] = sorted(
                installed | names if args["state"] == "present" else installed - names
            )
        elif operation == "service_facts":
            result["ansible_facts"] = {
                "services": {
                    "firewalld.service": {"state": "running" if state["running"] else "stopped"}
                }
            }
        elif operation == "stat":
            result["stat"] = {"exists": True}
        elif args["name"] == "firewalld":
            port = str((task_vars or {}).get("ansible_port", 22)) + "/tcp"
            if not state["running"] and port not in state["management"]:
                return result | {"failed": True, "msg": "startup would lose management"}
            state.update(running=args["state"] == "started", enabled=args.get("enabled", False))
        else:
            assert args["state"] == "stopped"
            state["authority_stopped"] = True
        path.write_text(json.dumps(state))
        return result | {"changed": state != before}
