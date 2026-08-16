#!/bin/sh
set -eu

REPOSITORY_URL='https://github.com/dillonlille/dispatch.git'
RELEASES_URL='https://api.github.com/repos/dillonlille/dispatch/releases?per_page=100'
DISPATCH_HOME="${DISPATCH_HOME:-$HOME/.dispatch}"
CHANNEL=''
VERSION=''

fail() {
    printf '%s\n' "Dispatch installation failed: $*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: install.sh [--channel stable|dev] [--version TAG]

Without --channel, choose:
  1. Latest Stable
  2. Dev Branch

--version is an explicit stable GitHub Release tag. The dev channel always
tracks the dev branch.
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
        printf '\nDispatch installation channel:\n1. Latest Stable\n2. Dev Branch\nSelect [1-2]: ' >&3
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
    path.mkdir(mode=0o700)
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
mkdir "$staging"

if [ "$CHANNEL" = stable ]; then
    curl -fsSL --proto '=https' --tlsv1.2 --max-redirs 3 \
        -H 'Accept: application/vnd.github+json' \
        -H 'User-Agent: dispatch-installer' \
        "$RELEASES_URL" -o "$releases" \
        || fail 'could not retrieve GitHub releases'
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
    ref=dev
fi

printf 'Cloning Dispatch %s (%s) ...\n' "$CHANNEL" "$ref"
if [ "$CHANNEL" = stable ]; then
    git clone --quiet --no-checkout --depth 1 "$REPOSITORY_URL" "$clone" \
        || fail 'could not clone the Dispatch repository'
    git -C "$clone" fetch --quiet --depth 1 origin tag "$ref" \
        || fail 'could not fetch the selected published release tag'
    git -C "$clone" checkout --quiet --detach "refs/tags/$ref" \
        || fail 'could not detach at the selected published release tag'
else
    git clone --single-branch --branch dev "$REPOSITORY_URL" "$clone" \
        || fail 'could not clone the complete dev branch'
fi

printf '%s\n' 'Installing Dispatch dependencies and activating the user service ...'
if [ "$CHANNEL" = stable ]; then
    python3 -I -B -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from dispatch_installer.cli import main; raise SystemExit(main())' \
        "$clone/installer/src" \
        --dispatch-home "$DISPATCH_HOME" \
        install --clone "$clone" --channel stable --version "$ref" --yes \
        || fail 'Dispatch installation failed'
else
    python3 -I -B -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from dispatch_installer.cli import main; raise SystemExit(main())' \
        "$clone/installer/src" \
        --dispatch-home "$DISPATCH_HOME" \
        install --clone "$clone" --channel dev --yes \
        || fail 'Dispatch installation failed'
fi

printf '\nDispatch %s is installed in %s.\n' "$CHANNEL" "$DISPATCH_HOME"
if ( : </dev/tty ) 2>/dev/null; then
    exec 3<>/dev/tty
    printf '1. Start Setup\n2. Skip for Now\nSelect [1-2]: ' >&3
    IFS= read -r setup_choice <&3 || setup_choice=2
    case "$setup_choice" in
        1) "$HOME/.local/bin/dispatch" setup <&3 ;;
        2) printf '%s\n' "Setup skipped. Run $HOME/.local/bin/dispatch setup when ready." ;;
        *) exec 3>&-; fail 'invalid setup choice' ;;
    esac
    exec 3>&-
else
    printf '%s\n' "No controlling terminal; setup skipped. Run $HOME/.local/bin/dispatch setup when ready."
fi
