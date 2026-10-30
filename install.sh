#!/usr/bin/env bash
# Run from a checked-out repository as the account that will own the client.
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
workspace_dir="$HOME/workspace/test-project"
local_only=false
remote_url=""
yes=false
service=false
python_bin=""
while (($#)); do
    case "$1" in
        --workspace) workspace_dir="${2:?--workspace requires a path}"; shift 2 ;;
        --remote-url) remote_url="${2:?--remote-url requires a URL}"; shift 2 ;;
        --python) python_bin="${2:?--python requires an executable}"; shift 2 ;;
        --local-only) local_only=true; shift ;;
        --yes) yes=true; shift ;;
        --service) service=true; shift ;;
        --help) cat <<'HELP'
Usage: bash install.sh [--workspace PATH] [--remote-url http://VM_IP:8765]
                      [--local-only] [--python EXECUTABLE] [--yes] [--service]
Installs as the current user, uses Python >=3.11 or bootstraps Python 3.12 with uv,
creates .venv and a user launcher. Remote UI configuration prompts for a password.
--yes accepts installation/bootstrap, not a password. --service installs and starts
an optional systemd user UI service. No sudo or OS package changes are performed.
HELP
            exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done
if $local_only && [[ -n "$remote_url" ]]; then
    echo 'Choose --local-only or --remote-url, not both.' >&2; exit 2
fi
printf 'Account: %s\nHome: %s\nInstallation: %s\nWorkspace: %s\n' "$(id -un)" "$HOME" "$repo_dir" "$workspace_dir"
if [[ $(id -u) == 0 ]]; then
    echo 'Run this installer as your dedicated ordinary user (for example aiosagent), not root.' >&2
    exit 1
fi
if ! $yes; then
    read -r -p 'Install for this account? [y/N] ' answer
    [[ "$answer" == [yY] ]] || exit 1
fi
umask 077
if [[ -z "$python_bin" ]]; then
    for candidate in python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys;sys.exit(sys.version_info < (3,11))'; then
            python_bin="$candidate"; break
        fi
    done
fi
if [[ -n "$python_bin" ]]; then
    "$python_bin" -c 'import sys;sys.exit(sys.version_info < (3,11))' || { echo 'Selected Python must be >=3.11.' >&2; exit 1; }
else
    command -v python3 >/dev/null || { echo 'Ask your administrator to install python3 and python3-venv, then rerun.' >&2; exit 1; }
    echo 'Python >=3.11 was not found. Installing user-local uv and Python 3.12.'
    bootstrap_dir="$HOME/.local/share/aios-client/uv-bootstrap"
    python3 -m venv "$bootstrap_dir" || { echo 'Ask your administrator to install python3-venv, then rerun.' >&2; exit 1; }
    "$bootstrap_dir/bin/python" -m pip install uv
    "$bootstrap_dir/bin/uv" python install 3.12
    python_bin="$("$bootstrap_dir/bin/uv" python find 3.12)"
fi
if [[ -e "$repo_dir/.venv" ]]; then
    [[ -x "$repo_dir/.venv/bin/python" ]] && "$repo_dir/.venv/bin/python" -c 'import sys;sys.exit(sys.version_info < (3,11))' || {
        echo 'Existing .venv is incompatible. Rename it and rerun; it has not been deleted.' >&2; exit 1;
    }
else
    "$python_bin" -m venv "$repo_dir/.venv" || { echo 'Could not create .venv; install the matching Python venv package and retry.' >&2; exit 1; }
fi
"$repo_dir/.venv/bin/python" -m pip install -e "$repo_dir"
mkdir -p -- "$workspace_dir" "$HOME/.local/bin"
# No shell interpolation in the launcher; quote paths with bash's %q.
launcher_path="$HOME/.local/bin/aios-client"
launcher_tmp="$(mktemp "$HOME/.local/bin/.aios-client.XXXXXX")"
printf '#!/usr/bin/env bash\nexec %q "$@"\n' "$repo_dir/.venv/bin/aios-client" > "$launcher_tmp"
chmod 700 "$launcher_tmp"
if [[ -e "$launcher_path" ]] && ! cmp -s "$launcher_tmp" "$launcher_path"; then
    rm -f -- "$launcher_tmp"
    echo "Existing launcher differs: $launcher_path. Move it aside explicitly before rerunning." >&2; exit 1
fi
mv -- "$launcher_tmp" "$launcher_path"
if $local_only; then
    "$launcher_path" configure-ui --host 127.0.0.1 --public-url http://127.0.0.1:8765 --workspace "$workspace_dir"
else
    if [[ -z "$remote_url" ]]; then
        echo 'Enter the URL you will open from another computer, e.g. http://192.168.1.161:8765'
        read -r -p 'Remote UI browser URL: ' remote_url
        [[ -n "$remote_url" ]] || { echo 'A reachable browser URL is required, or use --local-only.' >&2; exit 1; }
    fi
    "$launcher_path" configure-ui --host 0.0.0.0 --public-url "$remote_url" --workspace "$workspace_dir"
fi
if $service; then
    "$launcher_path" service install --kind ui --enable
fi
printf '\nInstalled. Start setup with:\n  %s setup\nWorkspace: %s\n' "$launcher_path" "$workspace_dir"
echo 'If ~/.local/bin is on PATH, aios-client setup works without virtual-environment activation.'
echo 'See docs/installation.md for reboot startup, HTTPS, and diagnostics.'
