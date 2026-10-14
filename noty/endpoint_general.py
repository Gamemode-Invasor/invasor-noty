"""/notify: Noty's own format, for your scripts and any program."""
from . import service
from .endpoints import Endpoint


class General(Endpoint):
    name = "general"
    path = "/notify"
    label = "General"

    def adapt(self, data):
        """Strict: a typo in a field name is an error."""
        unknown = set(data) - {"title", "body", "icon", "sound"}
        if unknown:
            raise service.Rejected(400, f"unknown field(s): {', '.join(sorted(unknown))}")
        return data.get("title"), data.get("body", ""), data.get("icon", ""), data.get("sound", "")
