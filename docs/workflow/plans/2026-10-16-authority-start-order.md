# Authority first-start implementation plan

Expected implementation size: 90–120 changed lines (S).
The two-file map contains one task move and roughly 90 lines of harmless boundary coverage;
the S100 denominator remains the issue/source assessment, not this estimate.
Branch feat/authority-start-order-3233; BASE_BRANCH main. Execute as one coupled Cast task.

## Task 1: order and exercise convergence

Files: modify deploy/ansible/roles/provider_authority_host/tasks/install.yml and
 deploy/ansible/tests/check_authority_install.py.
Interfaces: consume existing task/handler YAML, HERE and run() fixture conventions;
add check_start_order(root), called by main() before the existing installation fixture.
No later runtime interface, ownership transition, migration, ADR or shared edit.
Verification: Mode: focused-test. Contract: converged first start, unchanged stopped
recovery, readiness ordering and failure propagation. Case: check_start_order.
Red: unsafe-acl assertion in fresh case before production task move.
Green: uv run --with ansible-core==2.21.1 python3 deploy/ansible/tests/check_authority_install.py
Expected: authority_start_order and existing authority_install success messages; exit 0.

1. Add this complete function before main(), then call check_start_order(root) immediately
   after root = Path(scratch). Run the green command and observe the specified red first.

```python
def check_start_order(root):
    role = HERE.parent / "roles/provider_authority_host"
    source = yaml.safe_load((role / "tasks/install.yml").read_text())
    handlers = yaml.safe_load((role / "handlers/main.yml").read_text())
    start = "Start the configured authority after firewall and protected inputs converge"
    libvirt = "Restart provider authority libvirt after deployed changes"
    authority = "Restart provider authority after deployed changes"
    verify = "Verify the restarted authority session libvirtd limits"
    readiness = "Run the configured authority one-shot readiness probe"
    notified = {task.get("notify") for task in source if isinstance(task.get("notify"), str)}
    assert {libvirt, authority} <= notified
    boundary = root / "service-boundary.py"
    boundary.write_text(
        "import json, pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "state = json.loads(path.read_text())\n"
        "action = sys.argv[2]\n"
        "if action == 'libvirt':\n"
        "    assert not state['fail'], 'libvirt convergence failed'\n"
        "    state['converged'] = True\n"
        "else:\n"
        "    assert state['converged'], 'unsafe-acl: authority before libvirt'\n"
        "    if action in ('started', 'restarted'):\n"
        "        state['running'] = True\n"
        "    else:\n"
        "        assert state['running'], 'readiness before service start'\n"
        "state['events'].append(action)\n"
        "path.write_text(json.dumps(state))\n"
    )
    for case, changed, running, fail in (
        ("fresh", True, False, False),
        ("changed", True, True, False),
        ("unchanged-stopped", False, False, False),
        ("unchanged-running", False, True, False),
        ("failed-libvirt", True, False, True),
    ):
        state = root / (case + ".json")
        state.write_text(
            json.dumps({"converged": not changed, "running": running, "fail": fail, "events": []})
        )

        def command(action, state_path=state):
            return {"argv": [sys.executable, str(boundary), str(state_path), action]}

        tasks = [
            {
                "name": "Deploy changed inputs",
                "ansible.builtin.debug": {"msg": case},
                "changed_when": changed,
                "notify": [libvirt, authority],
            }
        ]
        for task in source:
            name = task.get("name")
            if task.get("ansible.builtin.meta") == "flush_handlers":
                tasks.append(task)
            elif name in (start, verify, readiness):
                action = "started" if name == start else "verify" if name == verify else "ready"
                if name == start:
                    assert task["ansible.builtin.systemd_service"]["state"] == "started"
                tasks.append({"name": name, "ansible.builtin.command": command(action)})
        selected = [
            {
                "name": task["name"],
                "ansible.builtin.command": command(
                    "libvirt" if task["name"] == libvirt else "restarted"
                ),
            }
            for task in handlers
            if task["name"] in (libvirt, authority)
        ]
        play = root / (case + ".yml")
        play.write_text(
            yaml.safe_dump(
                [
                    {
                        "hosts": "localhost",
                        "connection": "local",
                        "gather_facts": False,
                        "vars": {"ansible_python_interpreter": sys.executable},
                        "tasks": tasks,
                        "handlers": selected,
                    }
                ]
            )
        )
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", str(play)],
            text=True,
            capture_output=True,
            check=False,
            timeout=90,
        )
        output = result.stdout + result.stderr
        assert (result.returncode == 0) == (not fail), output
        events = json.loads(state.read_text())["events"]
        expected = ["libvirt", "restarted"] if changed else []
        assert events == ([] if fail else expected + ["started", "verify", "ready"]), output
        if fail:
            assert "libvirt convergence failed" in output, output
    print("authority_start_order: fresh, changed, unchanged/stopped, readiness and failure passed")
```

2. Move the two-line existing flush task immediately before the unchanged start task:

```yaml
- name: Apply deployed-input restarts before proving readiness
  ansible.builtin.meta: flush_handlers

- name: Start the configured authority after firewall and protected inputs converge
  ansible.builtin.systemd_service:
    name: kdive-external-boot-authority.service
    enabled: true
    daemon_reload: true
    state: started
```

3. Rerun the focused command: both success messages and exit 0. Run just lint,
   just type and just lint-ansible (captured blocking streams); expect exit 0.
4. Review the diff, settle formatting with staged prek run, restage only named files,
   then commit. Retain no temp fixtures; TemporaryDirectory owns fixture cleanup.

Acceptance: production flush precedes explicit start; handler declaration order unchanged;
all five cases execute real Ansible dispatch; failed libvirt records no later effects.
Rollback: revert the task ordering/test commit; no installed-host changes are performed.
Deferrals, follow-up candidates and suppressions: none.
