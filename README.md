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
| `/notify/comet` | [Comet](https://github.com/imLinguin/comet) (GOG) | Comet's unlocked achievement, sent as a flat JSON object. |

For Comet, the notification takes `name` as the title, `description` (and the `rarity`) as the body, and
`image_url_unlocked` as the icon. Other fields are ignored, so new ones in Comet don't break anything. Only an
achievement unlocked in the last 5 minutes (`unlock_time`) is shown: anything else (a rarity update, or a new database
being filled with what you already had) is answered `ignored`:

```json
{"id": 54837606302766106, "key": "PET_DOG_MECK", "name": "…", "description": "…",
 "visible_while_locked": 1, "unlock_time": "2026-07-31T01:48:51+0000",
 "image_url_locked": "https://images.gog.com/….jpg", "image_url_unlocked": "https://images.gog.com/….jpg",
 "changed": 0, "rarity": 70.6, "rarity_level_description": "Common", "rarity_level_slug": "common"}
```

### Comety
Noty ships Comety (`bin/comety`, x86-64 only): a small program that watches a Comet SQLite database and POSTs each new
achievement to `/notify/comet`. Noty runs it for you:

1. When a game starts, Noty waits 5 seconds (for Comet to create its files) and then looks for every `gameplay.db` in
   `~/.local/share/Steam/steamapps/compatdata/<AppId>/pfx/drive_c/users/steamuser/AppData/Local/comet/gameplay/` and
   its subfolders. If there is none, it tries again after 10 s and once more after 15 s, then gives up.
2. For each one it starts `comety --url http://127.0.0.1:<port>/notify/comet --quiet <database>`. The tab lists them,
   one per line.
3. It shows a notification, "Comet - <game>" / "Noty is connecting to <game>", with the game's logo (the one you set in
   Steam, or else Steam's own, or else Comet's own image, `assets/comety.png`). It follows Do not disturb like any other.
4. When the game ends, the comety programs stop and a notification says "Noty disconnected from <game>". If the service
   stops they stop too, silently. A game with no database does nothing.
5. With the **Comet endpoint turned off**, none of this happens: no databases are searched and no comety runs (the other
   endpoints keep working). Turn it on while a game is running and it starts as if the game had just started.

If Comety can't run, Noty logs it and carries on.

### How the endpoints are built
Each endpoint is a file of its own, `endpoint_<name>.py` (`endpoint_general.py`, `endpoint_comet.py`), with a class that
extends `Endpoint` (`endpoints.py`) and is listed in `build()`:

| | |
|---|---|
| `name`, `path`, `label`, `icon` | Its setting (`endpoint_<name>` in `module.json`), where to POST, how its messages call it, and its image (a file in `assets/`). |
| `adapt(data)` | The payload as `(title, body, icon)`, or `None` to ignore it. |
| `start(host)` / `stop()` | It's on (the service runs and its switch is on) / it's off or the service stops. |
| `connected(host)` / `disconnected(host)` | The user turned it on / off. By default: a notification "<label> connected" / "<label> disconnected" with its image. Comet says nothing here: it speaks when it connects to a game. |
| `details()` | Extra lines the tab shows under it. |

Starting the service doesn't announce anything: only the user turning an endpoint on or off does.

### Do not disturb and turning endpoints off
In the tab you can turn on **Do not disturb**, or turn off a single endpoint (e.g. Comet's, while scripts still
notify). Requests are still received and checked, and answered `200 {"ok": true, "shown": false, "reason": …}` with
`"do_not_disturb"` or `"endpoint_off"`, so senders don't log errors; nothing is shown and nothing is kept for later.
They're listed in the tab's last notifications with 🔕. An event an adapter doesn't show gets `"reason": "ignored"`. The test button always shows its notification.

Notifications follow Steam's own setting for achievement notifications. They're shown through Invasor's core
(`ctx.notify`); if a Steam update ever changes how Steam shows them, Noty answers 503 and nothing else is affected.

## Build, test and install
With the core checked out next to this repository (`../invasor`):

```sh
python3 ../invasor/tools/pack_module.py noty       # build, check, tests -> noty-<version>.zip
python3 ../invasor/tools/install_module.py noty    # or install that zip from ⚙ Settings › Install module
```

Needs an Invasor core with `author` support in module.json.

## Changes

### 0.3.0
- `/notify/comet` takes Comet's new payload: the unlocked achievement as a flat JSON object (`name`, `description`,
  `rarity`, `image_url_unlocked`…), no longer wrapped in an `event`. The cached local image (`image_path`) is gone.
- Only an achievement unlocked in the last 5 minutes is shown (`unlock_time`); anything else is answered `ignored`.
- New: Noty ships and runs **Comety** (`bin/comety`), which reads Comet's database and sends each new achievement to
  `/notify/comet`. It starts for the running game (5 s after it starts, then again after 10 s and after 15 s), one per
  `gameplay.db` found, and stops with the game. With the Comet endpoint off it doesn't start at all. The tab lists them, and a notification tells you it's connecting.
- Comety: only a real unlock is audited (the trigger is replaced in existing databases), sent rows are always marked
  as done, invalid rows (HTTP 400/422) are dropped, a failed send stops the batch and retries in order, it backs off
  when the database is missing, and it exits if Noty dies or another Comety already watches the database.
- Each endpoint is a file of its own with a small interface (see *How the endpoints are built*): what to do when it starts
  and stops, its image, and what it says when the user turns it on or off ("General connected"). Comet says it when it
  connects to a game and when the game ends. Its notifications carry Comet's image (`assets/comety.png`) when the game
  has no logo.
- The module zip now includes the files under `bin/` and `assets/` (needs the core's updated `pack_module.py`).

### 0.2.0
- First release.

## License
[GNU General Public License v3.0 or later](LICENSE) (GPL-3.0-or-later).
