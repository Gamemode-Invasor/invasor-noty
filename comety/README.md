# Comety

The small program Noty runs for [Comet](https://github.com/imLinguin/comet) (GOG). It watches Comet's SQLite database
(`gameplay.db`) and POSTs each new unlocked achievement, as JSON, to a URL: Noty's `/cometa/db`. The binary Noty
ships, `noty/bin/comety`, is built from this source. It's covered by this repository's license.

```
comety --url <http://…> [--quiet] <database> [ms]
```

| | |
|---|---|
| `<database>` | Comet's SQLite database (read/write). |
| `[ms]` | Polling interval in milliseconds (1000 by default). |
| `--url` | `http://` endpoint to POST each new row to (required). |
| `--quiet` | Don't print the rows it processes. |

## How it works
- It adds an `achievement_audit` table and a trigger to the database: every time an achievement gets an `unlock_time`
  (new, or changed), the trigger copies the row there with `processed = 0`. Existing databases get the current trigger.
- Each interval it POSTs the unprocessed rows, in order, as JSON (without `audit_id` and `processed`). A `200` marks the
  row done; a `400`/`422` (the row isn't valid) drops it, marking it too; anything else (429, 5xx, no connection…) stops
  the batch and it tries again at the next interval. Rows already sent are always marked, even if it's stopped.
- If the database is missing or broken it waits longer between tries (up to 30 s).
- One instance per database (an advisory `flock` on it), and it exits when the process that started it dies.
- `SIGTERM`/`SIGINT` finish the batch in course and exit.

`example.json` is the payload it sends.

## Build
Needs a C compiler and `curl`/`unzip` (it downloads the SQLite amalgamation into `deps/`, which isn't kept in git):

```sh
make                                  # static `comety` (and `comety.no-static`)
cp comety ../noty/bin/comety          # the one Noty packs
```

The same source and toolchain give the same binary. x86-64 only, as built here.
