#!/bin/sh
set -eu

REPOSITORY_URL='https://github.com/dillonlille/dispatch.git'
RELEASES_URL='https://api.github.com/repos/dillonlille/dispatch/releases?per_page=100'
DISPATCH_HOME="${DISPATCH_HOME:-$HOME/.dispatch}"
CHANNEL=''
VERSION=''
SETUP_MODE=''   # '', yes, no

fail() {
    printf '%s\n' "Dispatch installation failed: $*" >&2
    exit 1
}

# --- Presentation helpers (degrade to plain text without a TTY / under NO_COLOR)
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != "dumb" ]; then
    C_ACCENT=$(printf '\033[36m'); C_OK=$(printf '\033[32m'); C_WARN=$(printf '\033[33m'); C_DIM=$(printf '\033[2m'); C_BOLD=$(printf '\033[1m'); C_RESET=$(printf '\033[0m')
else
    C_ACCENT=''; C_OK=''; C_WARN=''; C_DIM=''; C_BOLD=''; C_RESET=''
fi

commit12() {
    # first 12 characters of the resolved commit (portable, no string slicing)
    printf '%s' "$1" | cut -c1-12
}

say_ok()   { printf '  %s✓%s %s\n' "$C_OK" "$C_RESET" "$*"; }
say_run()  { printf '  %s●%s %s\n' "$C_ACCENT" "$C_RESET" "$*"; }
say_warn() { printf '  %s⚠%s %s\n' "$C_WARN" "$C_RESET" "$*"; }
say_dim()  { printf '  %s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }

show_banner() {
    printf '\n'
    printf '  %s╭────────────────────────────────╮%s\n' "$C_ACCENT" "$C_RESET"
    printf '  %s│%s                                %s│%s\n' "$C_ACCENT" "$C_RESET" "" "$C_RESET"
    printf '  %s│%s      %s◆  D I S P A T C H%s        %s│%s\n' "$C_ACCENT" "$C_RESET" "$C_BOLD" "$C_RESET" "" "$C_RESET"
    printf '  %s│%s   Your operations platform     %s│%s\n' "$C_ACCENT" "$C_RESET" "" "$C_RESET"
    printf '  %s│%s                                %s│%s\n' "$C_ACCENT" "$C_RESET" "" "$C_RESET"
    printf '  %s╰────────────────────────────────╯%s\n' "$C_ACCENT" "$C_RESET"
    printf '\n'
}

show_channel_menu() {
    printf '\n  %sSelect installation channel%s\n\n' "$C_BOLD" "$C_RESET"
    printf '    %s1.%s Latest Stable          %s\n' "$C_ACCENT" "$C_RESET" "${C_WARN}Recommended${C_RESET}"
    say_dim "       Newest published release. Most reliable."
    printf '    %s2.%s Development (main)\n' "$C_ACCENT" "$C_RESET"
    say_dim "       Current main branch. Freshest, less tested."
    printf '\n'
}

retry() {
    # retry <description> <command...>: run command with bounded retries
    description="$1"; shift
    attempt=1
    max_attempts=3
    while :; do
        if "$@"; then
            return 0
        fi
        if [ "$attempt" -ge "$max_attempts" ]; then
            fail "$description failed after $max_attempts attempts"
        fi
        printf '%s\n' "$description failed (attempt $attempt/$max_attempts); retrying..." >&2
        attempt=$((attempt + 1))
        sleep 2
    done
}

usage() {
    cat <<'EOF'
Usage: install.sh [--channel stable|dev] [--version TAG] [--setup|--no-setup]

Without --channel, choose:
  1. Latest Stable
  2. Development (main)

--version is an explicit stable GitHub Release tag. The dev channel always
tracks the main branch. --setup or --no-setup pre-selects the post-install
setup prompt for non-interactive use.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --channel)
            [ "$#" -ge 2 ] || fail '--channel requires stable or dev'
            CHANNEL="$2"
            shift 2
            ;;
        --version)
            [ "$#" -ge 2 ] || fail '--version requires a release tag'
            VERSION="$2"
            shift 2
            ;;
        --setup)
            SETUP_MODE=yes
            shift
            ;;
        --no-setup)
            SETUP_MODE=no
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            usage >&2
            fail "unknown argument: $1"
            ;;
    esac
done

case "$CHANNEL" in
    ''|stable|dev) ;;
    *) fail 'channel must be stable or dev' ;;
esac
[ "$CHANNEL" != dev ] || [ -z "$VERSION" ] || fail '--version is only valid with --channel stable'

command -v curl >/dev/null 2>&1 || fail 'curl is required'
command -v git >/dev/null 2>&1 || fail 'git is required'
command -v python3 >/dev/null 2>&1 || fail 'Python 3.11 through 3.13 is required'
python3 -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)' \
    || fail 'Python 3.11 through 3.13 is required'

if [ -z "$CHANNEL" ]; then
    if ( : </dev/tty ) 2>/dev/null; then
        exec 3<>/dev/tty
        show_channel_menu >&3
        printf '  Select [1-2]: ' >&3
        IFS= read -r choice <&3 || choice=1
        exec 3>&-
        case "$choice" in
            1) CHANNEL=stable ;;
            2) CHANNEL=dev ;;
            *) fail 'invalid channel selection' ;;
        esac
    else
        fail 'no controlling terminal; pass --channel stable or --channel dev'
    fi
fi

umask 077
case "$DISPATCH_HOME" in
    /*) ;;
    *) fail 'DISPATCH_HOME must be an absolute path' ;;
esac
python3 - "$DISPATCH_HOME" <<'PY'
import os
import pathlib
import shutil
import stat
import sys
raw_path = pathlib.Path(sys.argv[1])
raw_home = pathlib.Path(os.environ["HOME"])
if not raw_home.is_absolute() or ".." in raw_home.parts:
    raise SystemExit("unsafe HOME: it must be an absolute path without traversal")
for candidate in (raw_home, *raw_home.parents):
    if candidate.is_symlink():
        raise SystemExit(f"unsafe HOME symlink ancestor: {candidate}")
    if candidate.exists() and not candidate.is_dir():
        raise SystemExit(f"unsafe HOME non-directory ancestor: {candidate}")
if not raw_home.is_dir():
    raise SystemExit("unsafe HOME: it must be an existing directory")
home_details = raw_home.stat(follow_symlinks=False)
if home_details.st_uid != os.geteuid():
    raise SystemExit("unsafe HOME: it must be user-owned")
if stat.S_IMODE(home_details.st_mode) & 0o022:
    raise SystemExit("unsafe HOME: it must not be writable by group or other")
if ".." in raw_path.parts:
    raise SystemExit("unsafe DISPATCH_HOME: traversal components are not allowed")
path = pathlib.Path(os.path.abspath(raw_path))
home = pathlib.Path(os.path.abspath(raw_home))
if path == home or path in home.parents:
    raise SystemExit("unsafe DISPATCH_HOME: it cannot equal HOME or contain HOME")
for candidate in (path, *path.parents):
    if candidate.is_symlink():
        raise SystemExit(f"unsafe DISPATCH_HOME symlink ancestor: {candidate}")
    if candidate.exists() and not candidate.is_dir():
        raise SystemExit(f"unsafe DISPATCH_HOME non-directory ancestor: {candidate}")
    if candidate.exists() and candidate != path:
        details = candidate.stat(follow_symlinks=False)
        writable = stat.S_IMODE(details.st_mode) & 0o022
        sticky_boundary = bool(details.st_mode & stat.S_ISVTX)
        if details.st_uid not in {0, os.geteuid()} or (writable and not sticky_boundary):
            raise SystemExit(f"unsafe DISPATCH_HOME ownership or mode ancestor: {candidate}")
if path.exists():
    details = path.stat(follow_symlinks=False)
    if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077:
        raise SystemExit("unsafe DISPATCH_HOME: the existing installation root must be a private user-owned directory")
    temporary = path / ".install-tmp"
    if temporary.is_symlink() or (temporary.exists() and not temporary.is_dir()):
        raise SystemExit("unsafe temporary installation path")
    if temporary.exists():
        temporary_details = temporary.stat(follow_symlinks=False)
        if temporary_details.st_uid != os.geteuid() or stat.S_IMODE(temporary_details.st_mode) != 0o700:
            raise SystemExit("unsafe temporary installation directory: it must be private and user-owned")
else:
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise SystemExit("unsafe DISPATCH_HOME: the installation root parent must be an existing directory")
    details = parent.stat(follow_symlinks=False)
    if details.st_uid != os.geteuid() or details.st_mode & 0o022:
        raise SystemExit("unsafe DISPATCH_HOME: the installation root parent is not safe")
private_roots = []
for variable, suffix in (
    ("DISPATCH_CONFIG_ROOT", "config"),
    ("DISPATCH_SECRETS_ROOT", "secrets"),
    ("DISPATCH_DATA_ROOT", "data"),
    ("DISPATCH_STATE_ROOT", "state"),
    ("DISPATCH_CACHE_ROOT", "cache"),
    ("DISPATCH_LOGS_ROOT", "logs"),
    ("DISPATCH_RUNTIME_ROOT", "run"),
):
    configured = os.environ.get(variable)
    if configured:
        if any(ord(character) < 32 or ord(character) == 127 for character in configured):
            raise SystemExit(f"unsafe {variable}: control characters are not allowed")
        raw_private = pathlib.Path(configured)
        if not raw_private.is_absolute() or ".." in raw_private.parts:
            raise SystemExit(f"unsafe {variable}: it must be an absolute path without traversal")
    else:
        raw_private = path / suffix
    private = pathlib.Path(os.path.abspath(raw_private))
    if private == path or private in path.parents:
        raise SystemExit(f"unsafe {variable}: it cannot equal or contain DISPATCH_HOME")
    if private == home or private in home.parents:
        raise SystemExit(f"unsafe {variable}: it cannot equal or contain HOME")
    for managed in (path / "dispatch", path / "venv", path / ".install-tmp"):
        if private == managed or managed in private.parents or private in managed.parents:
            raise SystemExit(f"unsafe {variable}: it cannot overlap managed installation code")
    for candidate in (private, *private.parents):
        if candidate.is_symlink():
            raise SystemExit(f"unsafe {variable} symlink ancestor: {candidate}")
        if candidate.exists() and not candidate.is_dir():
            raise SystemExit(f"unsafe {variable} non-directory ancestor: {candidate}")
        if candidate.exists():
            details = candidate.stat(follow_symlinks=False)
            writable = stat.S_IMODE(details.st_mode) & 0o022
            sticky_boundary = bool(details.st_mode & stat.S_ISVTX)
            if details.st_uid not in {0, os.geteuid()} or (writable and not sticky_boundary):
                raise SystemExit(f"unsafe {variable} ownership or mode ancestor: {candidate}")
    if private.exists():
        details = private.stat(follow_symlinks=False)
        if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o700:
            raise SystemExit(f"unsafe {variable}: the existing root must be private and user-owned")
    private_roots.append((variable, private))
for index, (left_name, left) in enumerate(private_roots):
    for right_name, right in private_roots[index + 1:]:
        if left == right or left in right.parents or right in left.parents:
            raise SystemExit(f"unsafe private roots: {left_name} and {right_name} overlap")
if not path.exists():
    path.mkdir(mode=0o700)
required_bytes = int(os.environ.get("DISPATCH_MIN_FREE_BYTES", "2147483648"))
for label, target in (("DISPATCH_HOME", path), ("temporary staging", path / ".install-tmp")):
    probe = target if target.exists() else target.parent
    free = shutil.disk_usage(probe).free
    if free < required_bytes:
        raise SystemExit(
            f"insufficient disk space on {label} filesystem: "
            f"{free // (1024 * 1024)} MB free, at least "
            f"{required_bytes // (1024 * 1024)} MB required"
        )
PY
temporary_root="$DISPATCH_HOME/.install-tmp"
[ ! -L "$temporary_root" ] || fail 'temporary installation directory must not be a symlink'
if [ -e "$temporary_root" ] && [ ! -d "$temporary_root" ]; then
    fail 'temporary installation path must be a directory'
fi
if [ ! -e "$temporary_root" ]; then
    mkdir -m 700 "$temporary_root"
fi
python3 -I -B -c '
import os, pathlib, stat, sys
path = pathlib.Path(sys.argv[1])
if path.is_symlink() or not path.is_dir():
    raise SystemExit("unsafe temporary installation directory")
details = path.stat(follow_symlinks=False)
if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o700:
    raise SystemExit("unsafe temporary installation directory: it must be private and user-owned")
' "$temporary_root"
staging="$temporary_root/bootstrap.$$"
clone="$staging/dispatch"
releases="$staging/releases.json"
cleanup() {
    rm -rf "$staging"
}
trap cleanup EXIT HUP INT TERM
# Sweep stale staging dirs from previously crashed installs (older than 1 day).
find "$temporary_root" -maxdepth 1 -name 'bootstrap.*' -type d -mtime +1 -exec rm -rf {} + 2>/dev/null || true
mkdir "$staging"

if [ "$CHANNEL" = stable ]; then
    retry 'could not retrieve GitHub releases' \
        curl -fsSL --proto '=https' --tlsv1.2 --max-redirs 3 \
        -H 'Accept: application/vnd.github+json' \
        -H 'User-Agent: dispatch-installer' \
        "$RELEASES_URL" -o "$releases"
    VERSION="$(RELEASES="$releases" REQUESTED="$VERSION" python3 -c '
import json, os, re
items = json.load(open(os.environ["RELEASES"], encoding="utf-8"))
tag = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
valid = [item for item in items if isinstance(item, dict) and item.get("draft") is False and item.get("prerelease") is False and isinstance(item.get("tag_name"), str) and tag.fullmatch(item["tag_name"])]
valid.sort(key=lambda item: str(item.get("published_at") or item.get("created_at") or ""), reverse=True)
if not valid:
    raise SystemExit("no published stable release")
requested = os.environ["REQUESTED"]
if requested and not any(item["tag_name"] == requested for item in valid):
    raise SystemExit("requested tag is not a published stable release")
print(requested or valid[0]["tag_name"])
')" || fail 'could not resolve the latest stable release'
    ref="$VERSION"
else
    ref=main
fi

show_banner
say_ok "System requirements met"
say_run "Verifying installation paths ..."
printf 'Cloning Dispatch %s (%s) ...\n' "$CHANNEL" "$ref"
if [ "$CHANNEL" = stable ]; then
    retry 'could not clone the Dispatch repository' \
        git clone --quiet --no-checkout --depth 1 "$REPOSITORY_URL" "$clone"
    retry 'could not fetch the selected published release tag' \
        git -C "$clone" fetch --quiet --depth 1 origin tag "$ref"
    git -C "$clone" checkout --quiet --detach "refs/tags/$ref" \
        || fail 'could not detach at the selected published release tag'
else
    retry 'could not clone the complete main branch' \
        git clone --single-branch --branch main "$REPOSITORY_URL" "$clone"
fi
resolved_commit="$(git -C "$clone" rev-parse HEAD)"
say_ok "Fetched ${CHANNEL} at commit $(commit12 "$resolved_commit")"

printf '%s\n' 'Installing Dispatch dependencies and activating the user service ...'
say_run "Installing dependencies and browser (this can take a few minutes) ..."
if [ "$CHANNEL" = stable ]; then
    python3 -I -B -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from dispatch_installer.cli import main; raise SystemExit(main())' \
        "$clone/installer/src" \
        --dispatch-home "$DISPATCH_HOME" \
        install --clone "$clone" --channel stable --version "$ref" --yes >/dev/null \
        || fail 'Dispatch installation failed'
else
    python3 -I -B -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from dispatch_installer.cli import main; raise SystemExit(main())' \
        "$clone/installer/src" \
        --dispatch-home "$DISPATCH_HOME" \
        install --clone "$clone" --channel dev --yes >/dev/null \
        || fail 'Dispatch installation failed'
fi
say_ok "Installed ${CHANNEL} ($(commit12 "$resolved_commit"))"
say_ok "Launcher ready at ~/.local/bin/dispatch"
say_ok "User service active"

printf '\n'
say_dim "──────────────────────────────────────────────"
say_ok "${C_BOLD}Dispatch is ready${C_RESET} → $DISPATCH_HOME"
printf '\n'

case "$SETUP_MODE" in
    yes)
        "$HOME/.local/bin/dispatch" setup
        ;;
    no)
        say_dim "Setup skipped. Run: $HOME/.local/bin/dispatch setup"
        ;;
    *)
        if ( : </dev/tty ) 2>/dev/null; then
            exec 3<>/dev/tty
            printf '  %sNext step:%s configure plugins and your agent\n' "$C_BOLD" "$C_RESET" >&3
            printf '\n    %s1.%s Start Setup          %s\n' "$C_ACCENT" "$C_RESET" "${C_WARN}Recommended${C_RESET}" >&3
            printf '    %s2.%s Skip for Now\n' "$C_ACCENT" "$C_RESET" >&3
            printf '\n  Select [1-2]: ' >&3
            IFS= read -r setup_choice <&3 || setup_choice=2
            case "$setup_choice" in
                1) "$HOME/.local/bin/dispatch" setup <&3 ;;
                2) say_dim "Setup skipped. Run: $HOME/.local/bin/dispatch setup" ;;
                *) exec 3>&-; fail 'invalid setup choice' ;;
            esac
            exec 3>&-
        else
            say_dim "No controlling terminal; setup skipped."
            say_dim "Run later: $HOME/.local/bin/dispatch setup"
        fi
        ;;
esac
