"""Repository-limited enrollment exercised against a synthetic Forgejo."""

import configparser
import io
import json
import urllib.error
import urllib.request
import base64
import time

import yaml

from scripts.forgejo.compatibility import Application


class ActionsApplication(Application):
    def configuration(self) -> str:
        parser = configparser.ConfigParser(interpolation=None, allow_unnamed_section=True)
        parser.optionxform = str
        parser.read_string(super().configuration())
        parser["actions"]["ENABLED"] = "true"
        output = io.StringIO()
        parser.write(output)
        return output.getvalue()


def token_request(url: str, path: str, token: str, *, method: str = "GET",
                  payload: dict | None = None):
    request = urllib.request.Request(url + "/api/v1" + path, method=method,
        headers={"Authorization": "token " + token, "Content-Type": "application/json"},
        data=json.dumps(payload).encode() if payload is not None else None)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=15) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Synthetic registration API returned HTTP {error.code}") from None
    except (OSError, ValueError) as error:
        raise RuntimeError("Synthetic registration API did not return usable evidence") from error


def run_registration(experiment, directory, *, empty=False):
    application = ActionsApplication(experiment, directory)
    application.initialize_admin()
    first = application.request("/user/repos", method="POST", payload={
        "name": "runner-first", "auto_init": not empty, "default_branch": "main"})
    application.request("/user/repos", method="POST", payload={
        "name": "runner-second", "auto_init": True, "default_branch": "main"})
    credential = application.request(f"/users/{application.user}/tokens", method="POST", payload={
        "name": "runner-enrollment-fixture", "scopes": ["write:repository"],
        "repositories": [{"owner": application.user, "name": first["name"]}]})
    token = credential["sha1"]
    path = f"/repos/{application.user}/{first['name']}/actions/runners"
    registration = token_request(application.url, path, token, method="POST", payload={
        "name": experiment.name("controller"), "description": experiment.run_id,
        "ephemeral": True})
    rows = token_request(application.url, path + "?visible=false", token)
    if isinstance(rows, dict):
        rows = rows.get("runners", [])
    matching = [row for row in rows if row["id"] == registration["id"]]
    if len(matching) != 1 or matching[0].get("repo_id") != first["id"] or matching[0].get("ephemeral") is not True:
        raise RuntimeError("Registration did not preserve repository scope and ephemeral mode")
    for forbidden in [f"/repos/{application.user}/runner-second/actions/runners", "/admin/actions/runners"]:
        try:
            token_request(application.url, forbidden, token, method="POST", payload={
                "name": "forbidden", "ephemeral": True})
        except RuntimeError as error:
            if not any(str(status) in str(error) for status in (401, 403, 404)):
                raise
        else:
            raise RuntimeError("Enrollment credential exceeded its repository boundary")
    token_request(application.url, path + "/" + str(registration["id"]), token, method="DELETE")
    print("Repository-limited token registered and retired an ephemeral runner; broader scope was denied")
    return application, first, token


class ControllerFailureInjected(RuntimeError):
    pass


def run_one_job(experiment, directory, application, repository, token, runtime, candidate,
                *, controller_failure=False, workload=None):
    image = candidate["runner_image"]
    if image is None:
        raise ValueError("Compatibility requires an immutable runner image")
    experiment.command([experiment.podman, "pull", image], timeout=300)
    label = experiment.name("label")
    path = f"/repos/{application.user}/{repository['name']}"
    registration = token_request(application.url, path + "/actions/runners", token,
        method="POST", payload={"name": experiment.name("one-job"), "ephemeral": True})
    job_image = workload["image"] if workload else candidate["probe_image"]
    config = {"log": {"level": "debug"}, "runner": {"capacity": 1, "labels": [f"{label}:docker://{job_image}"],
                         "timeout": "1m"},
              "container": {"docker_host": "unix://" + runtime["socket"], "network": application.network,
                            "options": ("--tmpfs /var/lib/containers --tmpfs /home/podman/.local/share/containers "
                                        f"--label {experiment.label}={experiment.run_id}")},
              "cache": {"enabled": False},
              "server": {"connections": {"fixture": {
                  "url": f"http://{application.app}:3000", "uuid": registration["uuid"],
                  "token": registration["token"]}}}}
    if workload:
        config["runner"]["timeout"] = "120m"
        config["container"]["force_pull"] = False
        config["container"]["options"] += " --security-opt label=disable"
        config["container"]["options"] += " --volume " + str(workload["workspace"]) + ":" + str(workload["workspace"]) + ":U"
    experiment.private_file(directory, "runner.yml", yaml.safe_dump(config))
    workflow = ("name: single-job admission\non: [push]\njobs:\n"
                f"  first:\n    runs-on: {label}\n    steps:\n      - run: echo independent-first-job\n"
                f"  second:\n    runs-on: {label}\n    steps:\n      - run: echo unexpected-second-job\n")
    if workload:
        from scripts.forgejo_runner.workload import workflow as workload_workflow
        workflow = workload_workflow(label, application, repository, workload["workspace"], workload["selector"])
    if controller_failure:
        workflow = workflow.replace("echo independent-first-job", "sleep 90")
    created = application.request(path + "/contents/.forgejo/workflows/probe.yml", method="POST",
        payload={"content": base64.b64encode(workflow.encode()).decode(), "message": "synthetic workflow"})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        queued = token_request(application.url, path + f"/actions/runners/jobs?labels={label}", token)
        if queued and {row.get("name") for row in queued} == {"first", "second"}:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Synthetic workflow did not queue a job for the runner")
    handle = next(row["handle"] for row in queued if row.get("name") == "first")
    data = directory / "runner-data"
    data.mkdir(mode=0o700)
    runner = experiment.create("container", "one-job", [
        "--network", application.network,
        "--userns=keep-id:uid=1000,gid=1000", "--user", "1000:1000",
        "--security-opt", "label=disable",
        "--volume", f"{runtime['socket']}:{runtime['socket']}",
        "--volume", f"{directory}:/fixture:ro",
        "--volume", f"{data}:/data",
        "--entrypoint", "forgejo-runner", image, "--config", "/fixture/runner.yml", "one-job", "--handle", handle,
    ])
    if controller_failure:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            tracked = {name for kind, name in experiment.resources if kind == "container"}
            if any(name not in tracked for name in experiment.children()):
                experiment.command([experiment.podman, "kill", runner])
                raise ControllerFailureInjected("Synthetic controller failure was injected")
            time.sleep(1)
        raise RuntimeError("Fault probe did not observe the job container")
    waited = experiment.command([experiment.podman, "wait", runner], timeout=7500 if workload else 120)
    from scripts.forgejo_runner.fixture import ROOT
    evidence = ROOT / ".tmp/forgejo-runner"
    evidence.mkdir(parents=True, exist_ok=True)
    logs = experiment.command([experiment.podman, "logs", runner], check=False)
    experiment.private_file(evidence, experiment.run_id + ".runner.log",
                            (logs.stdout + logs.stderr).replace(registration["token"], "[redacted]"))
    if waited.stdout.strip() != "0":
        raise RuntimeError("One-job runner did not finish successfully")
    commit = created["commit"]["sha"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        rows = token_request(application.url, path + "/actions/tasks", token)
        if isinstance(rows, dict):
            rows = rows.get("workflow_runs", rows.get("tasks", []))
        success = [r for r in rows if r.get("status") == "success" and r.get("head_sha") == commit]
        if success:
            break
        time.sleep(1)
    else:
        experiment.private_file(evidence, experiment.run_id + ".tasks.json", json.dumps(rows))
        found = experiment.command([experiment.podman, "exec", application.app, "find",
                                    "/var/lib/gitea", "-path", "*/actions_log/*", "-type", "f"])
        for index, logfile in enumerate(found.stdout.splitlines()):
            content = experiment.command([experiment.podman, "exec", application.app, "base64", logfile])
            experiment.private_file(evidence, experiment.run_id + f".task-{index}.base64",
                                    content.stdout.replace(registration["token"], "[redacted]"))
        raise RuntimeError("One-job execution did not produce a successful task")
    if workload:
        from scripts.forgejo_runner.workload import require_success
        require_success(rows, commit)
    if len(success) != 1:
        raise RuntimeError("Ephemeral runner executed more than its single assigned job")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        waiting = token_request(application.url, path + f"/actions/runners/jobs?labels={label}", token)
        if any(row.get("name") == "second" for row in (waiting or [])):
            break
        time.sleep(1)
    else:
        raise RuntimeError("The second eligible job did not remain queued")
    remaining = token_request(application.url, path + "/actions/runners?visible=false", token)
    if any(r["id"] == registration["id"] for r in remaining):
        raise RuntimeError("Forgejo did not retire the ephemeral runner")
    if workload:
        evidence_record = {"source_tree": workload["source_tree"], "workflow_commit": commit,
                           "selector": workload["selector"], "status": "success"}
        experiment.private_file(evidence, experiment.run_id + ".workload.json",
                                json.dumps(evidence_record, indent=2) + "\n")
        print(f"Forgejo workload {workload['selector']} passed at {commit}")
    print("One-job runner completed one job and its registration was retired by Forgejo")
