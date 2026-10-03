# Noty

A module for [Invasor](../invasor) that shows **Steam notifications from your own scripts**: they look like Steam's
achievement notifications, in Game Mode and Big Picture.

Noty runs a small web service on **127.0.0.1** (port 33811 by default; turn the service off in its tab to change it). Only programs on this
machine can use it; nothing else on the network can.

```sh
curl -X POST http://127.0.0.1:33811/notify -H 'Content-Type: application/json' \
     -d '{"title": "Backup done", "body": "42 GB copied", "icon": "/home/deck/Pictures/ok.png"}'
```

| Field | Required | |
|---|---|---|
| `title` | yes | Plain text, up to 64 characters. |
| `body` | no | Plain text, up to 256 characters. |
| `icon` | no | An `https://` image URL, `data:image/…;base64,…`, or an absolute path to a `.png`, `.jpg`, `.gif` or `.webp` file on this machine (up to 384 KB). |

Answers: `200 {"ok": true, "shown": true}` when shown; `400` bad request; `413` too large; `415` not JSON;
`429` more than 5 in 10 s; `503` Steam can't show it right now. The tab shows whether the service is listening, a test
button and the last notifications.

### Endpoints
Each program can send its own format to its own endpoint; Noty turns it into a notification.

| Endpoint | For | Payload |
|---|---|---|
| `/notify` | general: your scripts and any program | `{"title", "body", "icon"}` as above. Unknown fields are an error. |
| `/notify/comet` | [Comet](https://github.com/imLinguin/comet) (GOG) | Comet's events. `achievement_unlocked` is shown; other events are ignored. |

For Comet, the notification takes `achievement.name` as the title, `achievement.description` (and the rarity) as the
body, and `achievement.image_path` (a cached local image) or else `achievement.image_url` as the icon. Other fields
are ignored, so new ones in Comet don't break anything:

```json
{"event": "achievement_unlocked", "version": 1, "game": {"id": "1234567890", "name": "…"},
 "achievement": {"name": "…", "description": "…", "rarity": 22.6,
                 "image_url": "https://images.gog.com/….jpg", "image_path": "/path/to/cached.jpg"}}
```

### Do not disturb and turning endpoints off
In the tab you can turn on **Do not disturb**, or turn off a single endpoint (e.g. Comet's, while scripts still
notify). Requests are still received and checked, and answered `200 {"ok": true, "shown": false, "reason": …}` with
`"do_not_disturb"` or `"endpoint_off"`, so senders don't log errors; nothing is shown and nothing is kept for later.
They're listed in the tab's last notifications with 🔕. An event Noty doesn't show (e.g. a Comet event other than an
achievement) gets `"reason": "ignored"`. The test button always shows its notification.

Notifications follow Steam's own setting for achievement notifications. They're shown through Invasor's core
(`ctx.notify`); if a Steam update ever changes how Steam shows them, Noty answers 503 and nothing else is affected.

## Build, test and install
With the core checked out next to this repository (`../invasor`):

```sh
python3 ../invasor/tools/pack_module.py noty       # build, check, tests -> noty-<version>.zip
python3 ../invasor/tools/install_module.py noty    # or install that zip from ⚙ Settings › Install module
```

Needs an Invasor core with `author` support in module.json.

## License
[GNU General Public License v3.0 or later](LICENSE) (GPL-3.0-or-later).
