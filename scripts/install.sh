#!/bin/sh
# oex installer.
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/osgeonepal/oex/main/scripts/install.sh | sh
#
# No flags. Probes your machine and picks whichever installer it finds, in
# this order:
#   1. uv          (preferred: isolated, no daemon, no sudo)
#   2. pipx        (isolated)
#   3. conda       (uses an isolated env named `oex`)
#   4. pip --user
#   5. pip3 --user
#   6. docker      (fallback: writes /usr/local/bin/oex-cli wrapping the image,
#                   may prompt for sudo)
#
# After install:
#   oex-cli --help
set -eu

log()  { printf '\033[1;34m[oex]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[oex]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[oex]\033[0m %s\n' "$*" >&2; }

have() { command -v "$1" >/dev/null 2>&1; }

verify_on_path() {
    if have oex-cli; then
        log "Installed: $(command -v oex-cli)"
        return 0
    fi
    warn "oex-cli installed but not yet on PATH."
    warn "Add the install bin dir to PATH and re-open your shell:"
    warn "  uv:   ensure \$HOME/.local/bin is on PATH"
    warn "  pipx: pipx ensurepath"
    warn "  pip:  add \"\$(python3 -m site --user-base)/bin\" to PATH"
    return 1
}

install_uv() {
    log "Installing oex via uv (isolated venv)"
    uv tool install --upgrade oex
}

install_pipx() {
    log "Installing oex via pipx (isolated venv)"
    pipx install --force oex
}

install_conda() {
    log "Installing oex into a conda env named 'oex' (pip inside conda)"
    conda create -y -n oex "python>=3.11" pip
    conda run -n oex pip install --upgrade oex
    bin="$(conda run -n oex python -c 'import sys; print(sys.prefix)')/bin"
    target="/usr/local/bin/oex-cli"
    if [ -x "$bin/oex-cli" ]; then
        if [ -w "$(dirname "$target")" ]; then
            ln -sf "$bin/oex-cli" "$target"
        else
            log "Symlinking $bin/oex-cli to $target via sudo"
            sudo ln -sf "$bin/oex-cli" "$target"
        fi
    fi
}

install_pip() {
    log "Installing oex via $1 --user"
    "$1" install --user --upgrade oex
}

install_docker_wrapper() {
    log "No Python installer found; installing docker wrapper"
    log "Pulling ghcr.io/osgeonepal/oex:latest"
    docker pull ghcr.io/osgeonepal/oex:latest

    target="/usr/local/bin/oex-cli"
    tmp="$(mktemp)"
    cat >"$tmp" <<'WRAPPER'
#!/bin/sh
exec docker run --rm \
    -v "$PWD/output:/app/output" \
    -v "$PWD/configs:/app/configs:ro" \
    ghcr.io/osgeonepal/oex:latest oex-cli "$@"
WRAPPER
    chmod +x "$tmp"

    if [ -w "$(dirname "$target")" ]; then
        mv "$tmp" "$target"
    else
        log "Writing $target via sudo"
        sudo mv "$tmp" "$target"
    fi
    log "Installed wrapper at $target"
}

main() {
    if have uv;     then install_uv                && verify_on_path && return 0; fi
    if have pipx;   then install_pipx              && verify_on_path && return 0; fi
    if have conda;  then install_conda             && verify_on_path && return 0; fi
    if have pip;    then install_pip pip           && verify_on_path && return 0; fi
    if have pip3;   then install_pip pip3          && verify_on_path && return 0; fi
    if have docker; then install_docker_wrapper    && verify_on_path && return 0; fi

    err "None of uv, pipx, conda, pip, pip3, or docker were found on this machine."
    err "Install one of them, then re-run this script:"
    err "  uv:     curl -LsSf https://astral.sh/uv/install.sh | sh"
    err "  docker: https://docs.docker.com/engine/install/"
    exit 1
}

main "$@"
