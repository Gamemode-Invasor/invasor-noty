"""What an endpoint is, and the ones Noty has (one file each: endpoint_<name>.py).

An endpoint is a way to send notifications to Noty: a path to POST to, how to turn its payload
into a notification, and what to do when it starts and stops. Subclass Endpoint, put it in
endpoint_<name>.py, and add it to build(). Its on/off switch is the setting endpoint_<name>
(module.json).
"""
from pathlib import Path

from . import service

ASSETS = Path(__file__).resolve().parent / "assets"


class Endpoint:
    name = ""     # the setting is endpoint_<name>; also the "source" of its notifications
    path = ""     # POST here
    label = ""    # how its messages call it
    icon = None   # file in assets/ with its image: used by its own messages

    def adapt(self, data):
        """The payload (a dict) as (title, body, icon), or None to ignore it. Rejected(400, …)
        if it's wrong."""
        raise NotImplementedError

    def start(self, host):
        """The endpoint is on: the service is running and its switch is on."""

    def stop(self):
        """It's off, or the service is stopping."""

    def connected(self, host):
        """The user turned it on. Say so (an endpoint may say it in its own way, or not at all)."""
        host.announce(self, f"{self.label} connected", f"Noty shows what is POSTed to {self.path}.")

    def disconnected(self, host):
        """The user is turning it off (called before stop())."""
        host.announce(self, f"{self.label} disconnected", "Noty no longer shows its notifications.")

    def details(self):
        """Lines for the tab, under the endpoint, while it's on."""
        return []

    def icon_data(self):
        """Its image as a data: URI, or "" if it has none."""
        try:
            return service.icon_from_file(ASSETS / self.icon) if self.icon else ""
        except service.Rejected:
            return ""


def build():
    """Every endpoint, by name, in the order they are listed."""
    from . import endpoint_comet, endpoint_general
    endpoints = [endpoint_general.General(), endpoint_comet.Comet()]
    return {ep.name: ep for ep in endpoints}
