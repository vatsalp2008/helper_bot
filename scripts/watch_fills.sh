#!/bin/bash
# Watch background `helper fill` tasks (the job hunt, CLAUDE.md): exits as soon as any of them prints a new line that
# needs Claude (questions to answer) or Vatsal (a pause), or finishes, so the session wakes up and handles it.
# Usage: scripts/watch_fills.sh <task output folder> <task id>...
D="$1"; shift
SEEN="${TMPDIR:-/tmp}/helper-watch-seen"
mkdir -p "$SEEN"
while true; do
  for t in "$@"; do
    f="$D/$t.output"; [ -f "$f" ] || continue
    n=$(wc -l < "$f"); old=$(cat "$SEEN/$t" 2>/dev/null || echo 0)
    if [ "$n" -gt "$old" ]; then
      new=$(tail -n +$((old+1)) "$f")
      echo "$n" > "$SEEN/$t"
      if echo "$new" | grep -qE "waiting for Claude|Paused|verif|Review page|stops here|EXIT=|unexpected|didn't accept|yourself|CAPTCHA"; then
        echo "=== $t"; echo "$new"; exit 0
      fi
    fi
  done
  sleep 3
done
