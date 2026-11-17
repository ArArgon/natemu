"""Where raw results and inputs live: $NATEMU_RAW_ROOT, else natemu-raw beside the main checkout (the same default as
tools/with-env). Inside a git worktree the main checkout is found through `git rev-parse --git-common-dir`, so a worktree
under .claude/worktrees/X uses the same folder (and the same VM lock) as the main checkout."""
import os
import pathlib
import subprocess


def default_root(repo=pathlib.Path(__file__).resolve().parents[1]):
    try:
        common = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                                capture_output=True, text=True, check=True).stdout.strip()
        main = pathlib.Path(common).parent
    except (OSError, subprocess.CalledProcessError):
        main = repo
    return main.parent / 'natemu-raw'


RAW_ROOT = pathlib.Path(os.environ.get('NATEMU_RAW_ROOT') or default_root())

if __name__ == '__main__':   # self-check: from the repo root and from a folder inside it the answer is the same
    here = default_root()
    assert here.name == 'natemu-raw', here
    assert default_root(pathlib.Path(__file__).resolve().parent) == here
    print(here)
