"""Fault and rotation experiments against the disposable Forgejo instance."""

import json
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from scripts.forgejo_runner.registration_probe import (
    retire_uncertain_registration, runner_rows, token_request,
)


def probe_lost_response(experiment, application, repository, token):
    path = f"/repos/{application.user}/{repository['name']}/actions/runners"
    sentinel = token_request(application.url, path, token, method="POST", payload={
        "name": experiment.name("unrelated-registration"), "ephemeral": True,
    })
    payload = {"name": experiment.name("lost-response"),
               "description": experiment.run_id, "ephemeral": True}
    receipt = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if (self.path != "/api/v1" + path or not 0 < size <= 4096
                        or self.headers.get("Authorization") != "token " + token
                        or receipt or json.loads(self.rfile.read(size)) != payload):
                    self.send_error(400)
                    return
                created = token_request(application.url, path, token, method="POST", payload=payload)
                receipt["id"] = created["id"]
            except (RuntimeError, ValueError, KeyError, TypeError):
                self.send_error(502)
                return
            # Forgejo has completed the write, but the caller receives no HTTP response.
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        try:
            token_request(f"http://127.0.0.1:{server.server_port}", path, token,
                          method="POST", payload=payload)
        except RuntimeError:
            if "id" not in receipt:
                raise RuntimeError("Response-loss experiment did not complete registration") from None
        else:
            raise RuntimeError("Response-loss experiment unexpectedly received success")
    finally:
        server.shutdown()
        thread.join(timeout=20)
        server.server_close()
    retired = retire_uncertain_registration(application.url, path, token,
        repo_id=repository["id"], name=payload["name"], marker=experiment.run_id)
    if retired != receipt["id"]:
        raise RuntimeError("Response-loss experiment retired a different registration")

    ambiguous_payload = dict(payload, name=experiment.name("ambiguous-response"))
    identities = [token_request(application.url, path, token, method="POST",
                                payload=ambiguous_payload)["id"] for _ in range(2)]
    try:
        retire_uncertain_registration(application.url, path, token,
            repo_id=repository["id"], name=ambiguous_payload["name"], marker=experiment.run_id)
    except RuntimeError as error:
        if str(error) != "Uncertain registration has no unique owned identity":
            raise
    else:
        raise RuntimeError("Ambiguous registration lookup did not fail closed")
    remaining = {row["id"] for row in runner_rows(application.url, path, token)}
    if remaining != {sentinel["id"], *identities}:
        raise RuntimeError("Uncertain enrollment changed unrelated or ambiguous registrations")
    for identity in [*identities, sentinel["id"]]:
        token_request(application.url, path + "/" + str(identity), token, method="DELETE")
    if runner_rows(application.url, path, token):
        raise RuntimeError("Response-loss experiment left fixture registrations")
    print("Lost registration response reconciled without retry; ambiguous lookup preserved unrelated identities")


def require_denied(url, path, token, *, payload):
    try:
        token_request(url, path, token, method="POST", payload=payload)
    except RuntimeError as error:
        if str(error) not in {f"Synthetic registration API returned HTTP {code}" for code in (401, 403, 404)}:
            raise
    else:
        raise RuntimeError("Enrollment authority was not denied")


def probe_rotation(experiment, application, repository, old_credential):
    replacement = application.request(f"/users/{application.user}/tokens", method="POST", payload={
        "name": "runner-enrollment-replacement", "scopes": ["write:repository"],
        "repositories": [{"owner": application.user, "name": repository["name"]}],
    })
    application.request(f"/users/{application.user}/tokens/{old_credential['id']}", method="DELETE")
    path = f"/repos/{application.user}/{repository['name']}/actions/runners"
    payload = {"name": experiment.name("rotation"), "description": experiment.run_id, "ephemeral": True}
    require_denied(application.url, path, old_credential["sha1"], payload=payload)
    token = replacement["sha1"]
    for forbidden in (f"/repos/{application.user}/runner-second/actions/runners", "/admin/actions/runners"):
        require_denied(application.url, forbidden, token, payload=payload)
    registration = token_request(application.url, path, token, method="POST", payload=payload)
    rows = runner_rows(application.url, path, token)
    if (len(rows) != 1 or rows[0]["id"] != registration["id"]
            or rows[0].get("repo_id") != repository["id"] or rows[0].get("ephemeral") is not True):
        raise RuntimeError("Replacement authority did not preserve one repository-scoped registration")
    token_request(application.url, path + "/" + str(registration["id"]), token, method="DELETE")
    if runner_rows(application.url, path, token):
        raise RuntimeError("Rotation experiment left fixture registrations")
    print("Revoked enrollment authority denied; replacement retained repository scope and registered successfully")
    return token
