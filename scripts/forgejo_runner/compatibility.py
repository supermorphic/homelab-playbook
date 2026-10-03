"""Exercise the real worker socket from a disposable job container."""

import json
from pathlib import Path
import shlex
import tempfile

from scripts.forgejo_runner.fixture import ROOT, cleaned_up, load_candidate, preflight


def run(descriptor: Path | None, *, registration: bool = False, controller_failure=False) -> int:
    experiment, runtime = preflight()
    if descriptor is None:
        raise ValueError("A reviewed synthetic image fixture descriptor is required")
    candidate = load_candidate(descriptor, runtime["architecture"])
    image = candidate["probe_image"]
    experiment.observe_volumes()
    scratch_root = ROOT / ".tmp/forgejo-runner"
    scratch_root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="probe-", dir=scratch_root) as directory, cleaned_up(experiment):
            experiment.command([experiment.podman, "pull", image], timeout=300)
            probe = experiment.foreground("client", [
                "--userns=keep-id:uid=1000,gid=1000", "--user", "1000:1000",
                "--security-opt", "label=disable",
                "--volume", f"{runtime['socket']}:/run/worker-podman.sock",
                "--env", "CONTAINER_HOST=unix:///run/worker-podman.sock",
                "--tmpfs", "/var/lib/containers",
                "--tmpfs", "/home/podman/.local/share/containers",
                image, "podman", "--remote", "info", "--format", "json",
            ])
            observed = json.loads(probe.stdout)
            if observed["host"]["security"]["rootless"] is not True:
                raise RuntimeError("Job client did not reach a rootless worker")
            print("Worker socket is usable from an unprivileged job container")
            sibling = experiment.reserve("container", "sibling")
            scratch = Path(directory) / "paths"
            scratch.mkdir()
            (scratch / "sentinel.txt").write_text("independent fixture input\n")
            command = shlex.join([
                "podman", "--remote", "run", "--detach", "--name", sibling,
                "--label", f"{experiment.label}={experiment.run_id}",
                "--userns=keep-id:uid=1000,gid=1000", "--user", "1000:1000",
                "--volume", f"{scratch}:/fixture:U",
                "--tmpfs", "/var/lib/containers",
                "--tmpfs", "/home/podman/.local/share/containers", image, "sleep", "120",
            ])
            script = command + "\n" + shlex.join([
                "podman", "--remote", "exec", sibling, "cat", "/fixture/sentinel.txt",
            ]) + "\n"
            result = experiment.foreground("job", [
                "--userns=keep-id:uid=1000,gid=1000", "--user", "1000:1000",
                "--security-opt", "label=disable",
                "--volume", f"{runtime['socket']}:/run/worker-podman.sock",
                "--volume", f"{scratch}:{scratch}",
                "--env", "CONTAINER_HOST=unix:///run/worker-podman.sock",
                "--tmpfs", "/var/lib/containers",
                "--tmpfs", "/home/podman/.local/share/containers",
                image, "/bin/sh", "-ec", script,
            ])
            if not result.stdout.endswith("independent fixture input\n"):
                raise RuntimeError("Sibling bind path or exec did not preserve the fixture input")
            print("Job created a sibling container with matching bind paths and :U ownership")
            if registration:
                from scripts.forgejo_runner.registration_probe import run_one_job, run_registration
                inputs = Path(directory) / "registration"
                inputs.mkdir(mode=0o700)
                application, repository, token = run_registration(experiment, inputs)
                run_one_job(experiment, inputs, application, repository, token, runtime, candidate,
                            controller_failure=controller_failure)
    except RuntimeError as error:
        from scripts.forgejo_runner.registration_probe import ControllerFailureInjected
        if not controller_failure or type(error) is not ControllerFailureInjected:
            raise
        for kind, name in [*experiment.resources, *[("volume", n) for n in experiment.child_volumes]]:
            if experiment.command([experiment.podman, kind, "exists", name], check=False).returncode != 1:
                raise RuntimeError("Fault probe left an owned fixture resource") from error
        if experiment.children():
            raise RuntimeError("Fault probe left a labelled job container") from error
        print("Forced controller failure removed its job containers and new volumes")
    return 0
