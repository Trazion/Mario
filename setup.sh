#!/bin/bash
# ═══════════════════════════════════════════════════════════
#  Mario Camera Streamer — Setup Script  (v3.9.10)
#
#  Hardened install:
#   • Detects the REAL invoking user (works under sudo, never
#     produces mario@root.service by accident).
#   • Always installs project code into /home/<user>/mario-stream
#     (rsync from wherever the script was unpacked).
#   • Creates writable data folders + SQLite DB + history JSON.
#   • Writes /home/<user>/.mario_env in systemd-compatible
#     KEY=value format (no `export`), upserts keys idempotently.
#   • Installs mario@.service with ReadWritePaths covering every
#     data dir so SQLite + normalized_cache survive
#     ProtectHome=read-only.
#   • Disables stale mario@root.service if it exists.
# ═══════════════════════════════════════════════════════════
set -e

# ── CLI flags ──────────────────────────────────────────────
ASSUME_YES=0
NO_START=0
for arg in "$@"; do
    case "$arg" in
        --yes|-y)    ASSUME_YES=1 ;;
        --no-start)  NO_START=1 ;;
        -h|--help)
            cat <<HLP
Usage: $0 [--yes] [--no-start]
  --yes        Non-interactive; enable autostart and start the service.
  --no-start   Do not start the service now (autostart still applied if selected/--yes).
HLP
            exit 0 ;;
    esac
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓ $1${NC}"; }
info() { echo -e "${CYAN}  → $1${NC}"; }
warn() { echo -e "${YELLOW}  ⚠ $1${NC}"; }
fail() { echo -e "${RED}  ✗ $1${NC}"; exit 1; }

ask_yes_no() {
    local prompt="$1" default="$2" answer
    [ "$ASSUME_YES" = "1" ] && return 0
    if [ ! -t 0 ]; then [ "$default" = "yes" ]; return $?; fi
    while true; do
        if [ "$default" = "yes" ]; then
            read -r -p "$prompt [Y/n]: " answer; answer="${answer:-Y}"
        else
            read -r -p "$prompt [y/N]: " answer; answer="${answer:-N}"
        fi
        case "$answer" in
            [Yy]|[Yy][Ee][Ss]) return 0 ;;
            [Nn]|[Nn][Oo])     return 1 ;;
            *) echo "Please answer yes or no." ;;
        esac
    done
}

# ── 2A. Detect REAL user (never root by accident) ──────────
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    REAL_USER="$SUDO_USER"
else
    REAL_USER="$(whoami)"
fi
if [ "$REAL_USER" = "root" ]; then
    warn "Detected installation as 'root'. systemd unit would become mario@root.service."
    warn "Re-run WITHOUT sudo as your normal user, e.g.:  ./setup.sh"
    if ! ask_yes_no "Continue installing as root anyway?" "no"; then
        fail "Aborted — re-run as a regular user."
    fi
fi
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
[ -z "$REAL_HOME" ] && REAL_HOME="/home/$REAL_USER"
[ -d "$REAL_HOME" ] || fail "Home directory not found for user $REAL_USER ($REAL_HOME)"

INSTALL_DIR="$REAL_HOME/mario-stream"
DATA_DIR="$REAL_HOME/mario_data"
MEDIA_DIR="$REAL_HOME/mario_media"
ENV_FILE="$REAL_HOME/.mario_env"

# sudo wrapper — only prefix when not already root
SUDO=""
if [ "$(id -u)" != "0" ]; then SUDO="sudo"; fi

# Run a command as REAL_USER (so files end up owned correctly).
run_as_user() {
    if [ "$(whoami)" = "$REAL_USER" ]; then
        bash -c "$*"
    else
        $SUDO -u "$REAL_USER" -H bash -c "$*"
    fi
}

CARD_LABEL="EMEET S600 4K Webcam for Streaming"
MIC_LABEL="EMEET S600 4K Webcam for Streaming"
VIDEO_NR=10

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║     Mario Camera Streamer — Setup        ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo -e "  Installing for user: ${CYAN}${REAL_USER}${NC}  (home: ${REAL_HOME})"
echo -e "  Install path:        ${CYAN}${INSTALL_DIR}${NC}"
echo ""

# ── 1. System packages ─────────────────────────────────────
echo -e "${YELLOW}[1/7] Installing system packages...${NC}"
$SUDO apt-get update -q
$SUDO apt-get install -y ffmpeg v4l2loopback-dkms v4l-utils python3-pip python3-venv \
    pulseaudio pulseaudio-utils rsync
ok "ffmpeg, v4l2loopback-dkms, v4l-utils, python3-pip, python3-venv, pulseaudio, rsync installed"

# ── 2. FFmpeg capability check ─────────────────────────────
echo ""
echo -e "${YELLOW}[2/7] Checking FFmpeg capabilities...${NC}"
if ffmpeg -filters 2>/dev/null | grep -q drawtext; then
    ok "FFmpeg drawtext filter available"
else
    warn "FFmpeg drawtext not available — text overlay won't work"
fi
ok "FFmpeg version: $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"

# ── 3. v4l2loopback ────────────────────────────────────────
echo ""
echo -e "${YELLOW}[3/7] Virtual camera (v4l2loopback)...${NC}"
$SUDO modprobe v4l2loopback devices=1 video_nr=${VIDEO_NR} \
       card_label="${CARD_LABEL}" exclusive_caps=1 2>/dev/null \
    && ok "v4l2loopback loaded → /dev/video${VIDEO_NR}" \
    || warn "modprobe failed — module may already be loaded"
echo "v4l2loopback" | $SUDO tee /etc/modules-load.d/v4l2loopback.conf > /dev/null
printf 'options v4l2loopback devices=1 video_nr=%d card_label="%s" exclusive_caps=1\n' \
    "${VIDEO_NR}" "${CARD_LABEL}" \
    | $SUDO tee /etc/modprobe.d/v4l2loopback.conf > /dev/null
ok "Virtual camera persistent config written"

# ── 4. PulseAudio virtual mic ──────────────────────────────
echo ""
echo -e "${YELLOW}[4/7] Virtual microphone (PulseAudio)...${NC}"
run_as_user "pulseaudio --check 2>/dev/null || (pulseaudio --start --log-target=syslog 2>/dev/null || true)"
run_as_user "pactl load-module module-null-sink sink_name=VirtualMic sink_properties=device.description=VirtualMic_Sink 2>/dev/null || true"
run_as_user "pactl load-module module-virtual-source source_name=VirtualMicSource master=VirtualMic.monitor source_properties=device.description='${MIC_LABEL}' 2>/dev/null || true"
ok "Virtual microphone created: \"${MIC_LABEL}\""

PULSE_DEFAULT="$REAL_HOME/.config/pulse/default.pa"
run_as_user "mkdir -p '$(dirname "$PULSE_DEFAULT")'"
if ! run_as_user "grep -q VirtualMic '$PULSE_DEFAULT' 2>/dev/null"; then
    run_as_user "cat >> '$PULSE_DEFAULT' << 'PULSE'
### Mario Camera Streamer — Virtual Microphone
load-module module-null-sink sink_name=VirtualMic sink_properties=device.description=\"VirtualMic_Sink\"
load-module module-virtual-source source_name=VirtualMicSource master=VirtualMic.monitor source_properties=device.description=\"${MIC_LABEL}\"
PULSE"
    ok "PulseAudio persistent config written"
else
    ok "Virtual microphone already in PulseAudio config"
fi

# ── 5. Install project code to INSTALL_DIR (rsync) ─────────
echo ""
echo -e "${YELLOW}[5/7] Installing project code → ${INSTALL_DIR}${NC}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
    run_as_user "mkdir -p '$INSTALL_DIR'"
    # Sync code but never touch venv / cache / user data.
    # v3.9.22: .git/ is now INCLUDED (was excluded) — /api/version/apply
    # runs `git -C <app's own directory>` to self-update, and that only
    # works if the directory ffmpeg + gunicorn actually run from
    # ($INSTALL_DIR, not $SCRIPT_DIR) is itself a git checkout with a
    # working "origin" remote. Excluding .git/ made the in-app "Update
    # Now" button fail with "Project is not a git checkout" on every
    # install that used this rsync path (i.e. every install where
    # setup.sh wasn't run from inside $INSTALL_DIR itself).
    $SUDO rsync -a --delete \
        --exclude 'venv/' \
        --exclude '__pycache__/' \
        --exclude '*.pyc' \
        --exclude 'normalized_cache/' \
        --exclude 'mario_data/' \
        --exclude 'mario_media/' \
        --exclude 'mario_playlists/' \
        --exclude 'mario_recordings/' \
        --exclude 'mario_thumbs/' \
        --exclude 'mario_watermarks/' \
        --exclude 'mario_profiles/' \
        "$SCRIPT_DIR"/ "$INSTALL_DIR"/
    $SUDO chown -R "$REAL_USER:$REAL_USER" "$INSTALL_DIR"
    ok "Project code synced from $SCRIPT_DIR"
else
    ok "Already running from $INSTALL_DIR — no copy needed"
fi
cd "$INSTALL_DIR"

# ── 6. Python venv ─────────────────────────────────────────
echo ""
echo -e "${YELLOW}[6/7] Python virtualenv → ${INSTALL_DIR}/venv${NC}"
run_as_user "cd '$INSTALL_DIR' && [ -d venv ] || python3 -m venv venv"
run_as_user "cd '$INSTALL_DIR' && ./venv/bin/pip install --upgrade pip --quiet"
run_as_user "cd '$INSTALL_DIR' && ./venv/bin/pip install -r requirements.txt --quiet"
ok "Python packages installed in venv"

# ── 7. Data folders + DB + env file ────────────────────────
echo ""
echo -e "${YELLOW}[7/7] Data folders, DB, env file${NC}"
for dir in "$REAL_HOME/mario_playlists" "$REAL_HOME/mario_profiles" \
           "$REAL_HOME/mario_thumbs" "$REAL_HOME/mario_recordings" \
           "$REAL_HOME/mario_watermarks" "$MEDIA_DIR" \
           "$DATA_DIR" "$DATA_DIR/normalized_cache"; do
    run_as_user "mkdir -p '$dir' && chmod 755 '$dir'"
    ok "$dir"
done
$SUDO chown -R "$REAL_USER:$REAL_USER" \
    "$REAL_HOME/mario_playlists" "$REAL_HOME/mario_profiles" \
    "$REAL_HOME/mario_thumbs" "$REAL_HOME/mario_recordings" \
    "$REAL_HOME/mario_watermarks" "$MEDIA_DIR" "$DATA_DIR"

MARIO_DB_PATH="$DATA_DIR/mario_state.db"
MARIO_HISTORY_FILE="$DATA_DIR/mario_history.json"
for f in "$MARIO_DB_PATH" "$MARIO_HISTORY_FILE"; do
    run_as_user "[ -e '$f' ] || : > '$f'"
    run_as_user "chmod 664 '$f'"
done
$SUDO chown "$REAL_USER:$REAL_USER" "$MARIO_DB_PATH" "$MARIO_HISTORY_FILE"
ok "SQLite DB: $MARIO_DB_PATH"
ok "History:   $MARIO_HISTORY_FILE"

# ── .mario_env (idempotent upsert, no `export` prefix) ─────
# Preserve existing MARIO_PASSWORD if present; else generate.
EXISTING_PASS=""
if [ -f "$ENV_FILE" ]; then
    EXISTING_PASS="$($SUDO grep -E '^(export[[:space:]]+)?MARIO_PASSWORD=' "$ENV_FILE" 2>/dev/null \
        | tail -1 | sed -E 's/^(export[[:space:]]+)?MARIO_PASSWORD=//; s/^"//; s/"$//')"
fi
if [ -z "$EXISTING_PASS" ]; then
    EXISTING_PASS="$(head -c 12 /dev/urandom | base64 | tr -d '/+=' | head -c 16)"
    NEW_PASS=1
else
    NEW_PASS=0
fi

# upsert KEY=VALUE in $ENV_FILE (removes any old `export` prefix and dup lines)
upsert_env() {
    local key="$1" val="$2"
    $SUDO touch "$ENV_FILE"
    $SUDO sed -i -E "/^(export[[:space:]]+)?${key}=.*/d" "$ENV_FILE"
    echo "${key}=${val}" | $SUDO tee -a "$ENV_FILE" > /dev/null
}

# Wipe any stray `export ` prefixes that the file may carry from older setups.
if [ -f "$ENV_FILE" ]; then
    $SUDO sed -i -E 's/^export[[:space:]]+//' "$ENV_FILE"
fi

upsert_env MARIO_HOST                    "127.0.0.1"
upsert_env MARIO_PORT                    "5000"
upsert_env MARIO_DEBUG                   "0"
upsert_env MARIO_USER                    "mario"
upsert_env MARIO_PASSWORD                "\"${EXISTING_PASS}\""
upsert_env MARIO_DB_PATH                 "$MARIO_DB_PATH"
upsert_env MARIO_HISTORY_FILE            "$MARIO_HISTORY_FILE"
upsert_env MARIO_DATA_DIR                "$DATA_DIR"
upsert_env MARIO_NORMALIZED_CACHE_DIR    "$DATA_DIR/normalized_cache"
upsert_env MARIO_SCAN_ROOTS \
    "$MEDIA_DIR:$REAL_HOME/Videos:$REAL_HOME/Downloads:$REAL_HOME/mario_playlists:$REAL_HOME/mario_recordings:$REAL_HOME/mario_watermarks:/mnt:/media"

# Optional knobs — only insert if absent.
preserve_default() {
    local key="$1" val="$2"
    if ! $SUDO grep -qE "^${key}=" "$ENV_FILE"; then
        echo "${key}=${val}" | $SUDO tee -a "$ENV_FILE" > /dev/null
    fi
}
preserve_default MARIO_STRICT_ORIGIN     "0"
preserve_default MARIO_ALLOWED_ORIGINS   '""'
preserve_default MARIO_HW_ENCODER        "libx264"
preserve_default MARIO_CSRF              "1"
preserve_default MARIO_MAX_AUTO_RESTARTS "10"
preserve_default MARIO_SSE_MAX           "20"
preserve_default MARIO_MJPEG_MAX         "10"
preserve_default MARIO_MAX_UPLOAD_MB     "16"
preserve_default MARIO_BACKUP_MAX_MB     "50"
preserve_default MARIO_BACKUP_MAX_FILES  "500"
preserve_default MARIO_LOG_LEVEL         "INFO"
preserve_default MARIO_GITHUB_REPO       "Trazion/Mario"
preserve_default MARIO_ALLOW_AUTO_UPDATE "0"
preserve_default MARIO_TELEGRAM_TOKEN    '""'
preserve_default MARIO_TELEGRAM_CHAT_ID  '""'
preserve_default MARIO_DISCORD_WEBHOOK   '""'
preserve_default MARIO_WEBHOOK_EVENTS    '"start,stop,error"'

$SUDO chown "$REAL_USER:$REAL_USER" "$ENV_FILE"
$SUDO chmod 600 "$ENV_FILE"
ok "Environment file: $ENV_FILE"
if [ "$NEW_PASS" = "1" ]; then
    info "Generated MARIO_PASSWORD: ${EXISTING_PASS}"
else
    ok "Preserved existing MARIO_PASSWORD"
fi

# ── systemd install ────────────────────────────────────────
CURRENT_UNIT="mario@${REAL_USER}.service"
SYSTEMD_INSTALLED=0; SYSTEMD_ENABLED=0; SYSTEMD_STARTED=0

if [ -f "$INSTALL_DIR/mario.service" ] && command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    echo ""
    echo -e "${YELLOW}[systemd] Installing template → /etc/systemd/system/mario@.service${NC}"
    $SUDO cp "$INSTALL_DIR/mario.service" /etc/systemd/system/mario@.service
    $SUDO systemctl daemon-reload
    SYSTEMD_INSTALLED=1
    ok "Installed mario@.service"

    # 2G — disable stale mario@root.service when REAL_USER is not root.
    if [ "$REAL_USER" != "root" ] \
        && $SUDO systemctl list-unit-files 'mario@*.service' 2>/dev/null | grep -q 'mario@root.service'; then
        $SUDO systemctl disable --now mario@root.service 2>/dev/null || true
        warn "Disabled stale mario@root.service"
    fi
    # Also stop any other stale mario@<user> we are not installing for.
    for u in $($SUDO systemctl list-units --type=service --all --no-legend 'mario@*.service' 2>/dev/null | awk '{print $1}'); do
        if [ "$u" != "$CURRENT_UNIT" ]; then
            $SUDO systemctl stop "$u" 2>/dev/null || true
        fi
    done

    if ask_yes_no "Enable Mario Stream to start automatically after reboot?" "yes"; then
        $SUDO systemctl enable "$CURRENT_UNIT" && { SYSTEMD_ENABLED=1; ok "Autostart enabled for $CURRENT_UNIT"; } \
            || warn "Failed to enable $CURRENT_UNIT"
    else
        info "Enable later with: sudo systemctl enable $CURRENT_UNIT"
    fi

    if [ "$NO_START" = "1" ]; then
        info "--no-start: skipping immediate start."
    elif ask_yes_no "Start Mario Stream now?" "yes"; then
        $SUDO systemctl restart "$CURRENT_UNIT" && { SYSTEMD_STARTED=1; ok "Service started: $CURRENT_UNIT"; } \
            || warn "Failed to start $CURRENT_UNIT — check: sudo journalctl -u $CURRENT_UNIT -n 50"
    else
        info "Start later with: sudo systemctl start $CURRENT_UNIT"
    fi
else
    info "systemd not detected. Skipping service install."
fi

# ── Passwordless sudo for the dashboard's "Restart App" button ─────
# The backend's /api/system/restart-app already exists and is opt-in-safe
# (rate-limited, fixed command, no user input concatenated) but needs
# `sudo systemctl restart <this one unit>` to work without a password
# prompt — a web request obviously can't answer one. Scope the rule to
# EXACTLY this unit, nothing broader.
RESTART_SUDO_ENABLED=0
if [ "$SYSTEMD_INSTALLED" = "1" ]; then
    if ask_yes_no "Allow the Mario dashboard's 'Restart App' button to restart this one service (adds a narrowly-scoped passwordless sudo rule)?" "no"; then
        SYSTEMCTL_BIN="$(command -v systemctl)"
        if [ -z "$SYSTEMCTL_BIN" ]; then
            warn "systemctl not found on PATH — skipping."
        else
            SUDOERS_FILE="/etc/sudoers.d/mario-restart-${REAL_USER}"
            SUDOERS_LINE="${REAL_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} restart ${CURRENT_UNIT}, ${SYSTEMCTL_BIN} is-active ${CURRENT_UNIT}"
            TMP_SUDOERS="$(mktemp)"
            echo "$SUDOERS_LINE" > "$TMP_SUDOERS"
            # visudo -c validates syntax BEFORE it ever touches /etc/sudoers.d —
            # a malformed file installed straight could break sudo system-wide.
            if $SUDO visudo -c -f "$TMP_SUDOERS" >/dev/null 2>&1; then
                $SUDO install -o root -g root -m 0440 "$TMP_SUDOERS" "$SUDOERS_FILE"
                RESTART_SUDO_ENABLED=1
                ok "Passwordless restart enabled → $SUDOERS_FILE"
            else
                warn "Generated sudoers rule failed validation — skipped. Restart App button will stay disabled."
            fi
            rm -f "$TMP_SUDOERS"
        fi
    else
        info "Skipped. Enable later with:"
        info "  echo '${REAL_USER} ALL=(root) NOPASSWD: $(command -v systemctl 2>/dev/null || echo /usr/bin/systemctl) restart ${CURRENT_UNIT}' | sudo tee /etc/sudoers.d/mario-restart-${REAL_USER} && sudo chmod 0440 /etc/sudoers.d/mario-restart-${REAL_USER}"
    fi
fi

# ── Final summary ──────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║       Installation completed             ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo -e "  Installed path:    ${CYAN}${INSTALL_DIR}${NC}"
echo -e "  Media folder:      ${CYAN}${MEDIA_DIR}${NC}"
echo -e "  Data folder:       ${CYAN}${DATA_DIR}${NC}"
echo -e "  Normalized cache:  ${CYAN}${DATA_DIR}/normalized_cache${NC}"
echo -e "  Service:           ${CYAN}${CURRENT_UNIT}${NC}"
if [ "$SYSTEMD_INSTALLED" = "1" ]; then
    echo -e "  Autostart:         $([ "$SYSTEMD_ENABLED" = "1" ] && echo -e "${GREEN}enabled${NC}" || echo -e "${YELLOW}disabled${NC}")"
    echo -e "  Service state:     $([ "$SYSTEMD_STARTED" = "1" ] && echo -e "${GREEN}started${NC}" || echo -e "${YELLOW}not started${NC}")"
    echo -e "  Restart App btn:   $([ "$RESTART_SUDO_ENABLED" = "1" ] && echo -e "${GREEN}enabled${NC}" || echo -e "${YELLOW}disabled${NC}")"
fi
echo -e "  URL:               ${CYAN}http://127.0.0.1:5000${NC}"
echo ""
echo -e "  ${YELLOW}Useful commands:${NC}"
echo -e "    ${CYAN}sudo systemctl status   ${CURRENT_UNIT}${NC}"
echo -e "    ${CYAN}sudo journalctl -u      ${CURRENT_UNIT} -f${NC}"
echo -e "    ${CYAN}sudo systemctl restart  ${CURRENT_UNIT}${NC}"
echo -e "    ${CYAN}sudo systemctl stop     ${CURRENT_UNIT}${NC}"
echo -e "    ${CYAN}sudo systemctl disable  ${CURRENT_UNIT}${NC}"
echo ""

# Sanity checks (best-effort)
lsmod | grep -q v4l2loopback && ok "v4l2loopback active" || warn "v4l2loopback not detected — reboot may be required"
run_as_user "pactl list sources short 2>/dev/null | grep -q VirtualMicSource" \
    && ok "Virtual microphone active" || warn "Virtual microphone not detected — try: pulseaudio --start"
