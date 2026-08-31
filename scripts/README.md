# Wear Term scripts

Small POSIX `sh` scripts built on the `wt-*` device commands the app puts on `$PATH`
(`wt-battery`, `wt-clipboard`, `wt-fetch`, `wt-net`, `wt-notify`, `wt-open`, `wt-sensor`,
`wt-tts`, `wt-vibrate`).

The watch installs them from the app: **Menu → Scripts**, tap a row, tap again to confirm.
Each file is downloaded from this directory on `main`, checked against the `sha256` in
[index.json](index.json), and written into `$PREFIX/bin`, so it is on `$PATH` immediately.

| Script | What it does |
| --- | --- |
| `wt-status` | battery, network and system card in one screen |
| `wt-battwatch` | polls charge, buzzes and notifies once when it drops below a threshold |
| `wt-timer` | countdown; vibrates, notifies and speaks when it runs out |
| `wt-pomodoro` | work/break rounds, each boundary marked on the wrist |
| `wt-remind` | one notification after a delay |
| `wt-clipopen` | opens the clipboard's URL through the system |
| `wt-sensorlog` | samples a sensor on an interval into a CSV |

Every script prints its own usage in the comment header, and most take `-h`.

## Adding one

Drop the script in this directory with a header in the same shape:

```sh
#!/bin/sh
# wt-thing - what it does, one line.
# Usage: wt-thing [-x]
```

Line 2 becomes the catalogue description and the `Usage:` line the second row, so run
`./build-index.sh` after any edit — the app verifies the hash and refuses a stale index entry.

The shebang is rewritten to the installed shell at install time; keep `#!/bin/sh` here.
