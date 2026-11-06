#!/bin/sh
# yield.sh: before taking the VM lock, let another agent's queued VM checks go first (flock has no fairness):
# wait while a process of theirs runs, at most 30 minutes (main session's scheduling request, November 5).
for i in $(seq 1 90); do pgrep -f 'agent-a0d9674f2cc21db45/tools/bench' >/dev/null || exit 0; sleep 20; done
