"""Current runtime prerequisites for disposable runner experiments."""

from pathlib import Path
from contextlib import contextmanager
import json
import re
import shutil

from scripts.forgejo.runtime import Run

ROOT = Path(__file__).resolve().parents[2]


def runtime_info(info: dict) -> dict:
    try:
        host = info["host"]
        if host["security"]["rootless"] is not True:
            raise ValueError("Podman must run rootless")
        if str(host["cgroupVersion"]).removeprefix("v") != "2":
            raise ValueError("Podman must provide cgroup v2")
        architecture = {"aarch64": "arm64", "arm64": "arm64",
                        "x86_64": "amd64", "amd64": "amd64"}[host["arch"]]
        match = re.fullmatch(r"/run/user/([1-9][0-9]*)/containers", info["store"]["runRoot"])
        if match is None:
            raise ValueError("Podman runtime directory is not a rootless user directory")
    except (KeyError, TypeError) as error:
        raise ValueError("Podman runtime observation is incomplete") from error
    return {"architecture": architecture,
            "socket": f"/run/user/{match[1]}/podman/podman.sock"}


class RunnerRun(Run):
    label = "io.homelab.forgejo-runner.test"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.baseline_volumes = None
        self.child_volumes = {}
        self.job_images = []
        self.molecule_scenario = None

    def observe_volumes(self):
        response = self.command([self.podman, "volume", "ls", "--format", "{{.Name}}"])
        self.baseline_volumes = set(response.stdout.splitlines())

    def name(self, suffix: str) -> str:
        # Reuse the tested suffix and run-identifier validation.
        return "runner-" + super().name(suffix)

    def children(self):
        response = self.command([self.podman, "container", "ls", "--all", "--filter",
                                 f"label={self.label}={self.run_id}", "--format", "{{.Names}}"])
        return response.stdout.splitlines()

    def cleanup(self):
        if self.baseline_volumes is None:
            return super().cleanup()
        # Stop the only job creator before discovering its engine-created children.
        resources = self.resources
        self.resources = [r for r in resources if r == ("container", self.name("one-job"))]
        try:
            errors = super().cleanup()
        finally:
            self.resources = resources
        if errors:
            return errors
        try:
            for name in self.children():
                if ("container", name) in self.resources:
                    continue
                document = json.loads(self.command([self.podman, "container", "inspect", name]).stdout)[0]
                if document["Config"]["Labels"].get(self.label) != self.run_id:
                    raise RuntimeError("child ownership changed")
                self.resources.append(("container", name))
                for mount in document.get("Mounts", []):
                    volume = mount.get("Name")
                    if mount.get("Type") != "volume" or not volume or volume in self.baseline_volumes:
                        continue
                    observed = json.loads(self.command([self.podman, "volume", "inspect", volume]).stdout)[0]
                    if not observed.get("CreatedAt"):
                        raise RuntimeError("child volume identity is unavailable")
                    self.child_volumes[volume] = (observed["CreatedAt"], observed.get("Labels"))
        except (RuntimeError, ValueError, KeyError, IndexError, TypeError):
            errors.append("Runner child discovery failed")
        errors.extend(super().cleanup())
        # The host invocation lock is held until cleanup completes. No selected
        # scenario container existed before this job, and job creation is stopped.
        if not errors and self.molecule_scenario is not None:
            from scripts.molecule import ownership_labels
            for platform in self.molecule_scenario.platforms:
                try:
                    name = platform.container
                    exists = self.command([self.podman, "container", "exists", name], check=False)
                    if exists.returncode == 1:
                        continue
                    if exists.returncode:
                        raise RuntimeError("Molecule container inspection failed")
                    document = json.loads(self.command([self.podman, "container", "inspect", name]).stdout)[0]
                    labels = document["Config"]["Labels"]
                    expected = ownership_labels(platform, self.molecule_scenario)
                    if any(labels.get(key) != value for key, value in expected.items()):
                        raise RuntimeError("Molecule child ownership changed")
                    self.command([self.podman, "container", "rm", "--force", document["Id"]])
                    if self.command([self.podman, "container", "exists", name], check=False).returncode != 1:
                        raise RuntimeError("Molecule child survived cleanup")
                except (RuntimeError, ValueError, KeyError, IndexError, TypeError):
                    errors.append("Molecule child cleanup failed")
        for name, identity in self.child_volumes.items():
            try:
                exists = self.command([self.podman, "volume", "exists", name], check=False)
                if exists.returncode == 1:
                    continue
                if exists.returncode:
                    raise RuntimeError("volume inspection failed")
                document = json.loads(self.command([self.podman, "volume", "inspect", name]).stdout)[0]
                if (document.get("CreatedAt"), document.get("Labels")) != identity:
                    raise RuntimeError("volume identity changed")
                # No force: the engine rejects deletion if another container uses it.
                self.command([self.podman, "volume", "rm", name], timeout=30)
            except (RuntimeError, ValueError, KeyError, IndexError, TypeError):
                errors.append("Runner child volume cleanup failed")
        for name in self.job_images:
            try:
                image = json.loads(self.command([self.podman, "image", "inspect", name]).stdout)[0]
                if image["Config"]["Labels"].get(self.label) != self.run_id:
                    raise RuntimeError("Workload image ownership changed")
                self.command([self.podman, "image", "rm", name])
            except (RuntimeError, ValueError, KeyError, IndexError, TypeError):
                errors.append("Workload image cleanup failed")
        return errors


@contextmanager
def cleaned_up(experiment):
    """Retain the probe failure if teardown also fails."""
    primary = None
    try:
        yield
    except BaseException as error:
        primary = error
        raise
    finally:
        if experiment.cleanup():
            message = "Runner experiment cleanup failed"
            if primary is not None:
                message = f"{primary}; {message}"
            raise RuntimeError(message) from primary


def preflight() -> tuple[RunnerRun, dict]:
    podman = shutil.which("podman")
    if podman is None:
        raise RuntimeError("Podman is unavailable; provide a rootless test runtime")
    experiment = RunnerRun(podman=podman)
    response = experiment.command([podman, "info", "--format", "json"], timeout=15)
    return experiment, runtime_info(json.loads(response.stdout))


def load_candidate(path: Path, architecture: str) -> dict:
    try:
        candidate = json.loads(path.read_text())
        image = candidate["probe_images"][architecture]
        runner = candidate.get("runner_images", {}).get(architecture)
        if re.fullmatch(r"quay\.io/podman/stable@sha256:[0-9a-f]{64}", image) is None:
            raise ValueError("invalid probe image")
        if runner is not None and re.fullmatch(
                r"data\.forgejo\.org/forgejo/runner@sha256:[0-9a-f]{64}", runner) is None:
            raise ValueError("invalid runner image")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError("Fixture requires immutable images for the observed architecture") from error
    return {"probe_image": image, "runner_image": runner}
