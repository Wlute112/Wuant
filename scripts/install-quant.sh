#!/bin/bash
set -e

# Default values
BIN_DIR="${HOME}/.local/bin"
RC_FILE="${ZDOTDIR:-$HOME}/.zshrc"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --bin-dir)
            if [[ $# -lt 2 ]]; then
                echo "Error: --bin-dir requires an argument" >&2
                exit 1
            fi
            BIN_DIR="$2"
            shift 2
            ;;
        --rc-file)
            if [[ $# -lt 2 ]]; then
                echo "Error: --rc-file requires an argument" >&2
                exit 1
            fi
            RC_FILE="$2"
            shift 2
            ;;
        --help)
            echo "Usage: $0 [--bin-dir DIR] [--rc-file FILE]"
            echo "  --bin-dir DIR   Directory to install quant binary (default: $HOME/.local/bin)"
            echo "  --rc-file FILE  Shell rc file to update PATH (default: ${ZDOTDIR:-$HOME}/.zshrc)"
            exit 0
            ;;
        *)
            echo "Error: Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# Resolve checkout root from installer location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Require executable root/quant
QUANT_PATH="$CHECKOUT_ROOT/quant"
if [[ ! -x "$QUANT_PATH" ]]; then
    echo "Error: quant executable not found at $QUANT_PATH" >&2
    exit 1
fi

# Validate paths are absolute
case "$BIN_DIR" in
    /*) ;;
    *) echo "Error: --bin-dir must be an absolute path: $BIN_DIR" >&2; exit 1 ;;
esac

case "$RC_FILE" in
    /*) ;;
    *) echo "Error: --rc-file must be an absolute path: $RC_FILE" >&2; exit 1 ;;
esac

# Create bin directory if it doesn't exist
if [[ ! -d "$BIN_DIR" ]]; then
    mkdir -p "$BIN_DIR" || { echo "Error: Cannot create directory $BIN_DIR" >&2; exit 1; }
fi

# Create symlink bin/quant -> checkout/quant
SYMLINK_PATH="$BIN_DIR/quant"

# Check for conflicting files/directories/symlinks
if [[ -e "$SYMLINK_PATH" ]] || [[ -L "$SYMLINK_PATH" ]]; then
    # Allow already matching symlink without changing
    if [[ -L "$SYMLINK_PATH" ]] && [[ "$(readlink "$SYMLINK_PATH")" == "$QUANT_PATH" ]]; then
        :
    else
        echo "Error: $SYMLINK_PATH already exists and is not the expected symlink to $QUANT_PATH" >&2
        exit 1
    fi
else
    # Create absolute symlink
    ln -s "$QUANT_PATH" "$SYMLINK_PATH" || { echo "Error: Cannot create symlink $SYMLINK_PATH" >&2; exit 1; }
fi

# Create rc parent directory if needed
RC_DIR="$(dirname "$RC_FILE")"
if [[ ! -d "$RC_DIR" ]]; then
    mkdir -p "$RC_DIR" || { echo "Error: Cannot create directory $RC_DIR" >&2; exit 1; }
fi

# Prepare PATH export line
# Use printf %q for shell-safe quoting of BIN_DIR
BIN_DIR_QUOTED="$(printf '%q' "$BIN_DIR")"
PATH_EXPORT_LINE="export PATH=$BIN_DIR_QUOTED:\$PATH"

# Check if exact line already exists in rc file
if [[ -f "$RC_FILE" ]]; then
    if grep -qxF "$PATH_EXPORT_LINE" "$RC_FILE" 2>/dev/null; then
        # Line already exists, skip appending
        :
    else
        # Append line with newline
        printf '\n%s\n' "$PATH_EXPORT_LINE" >> "$RC_FILE"
    fi
else
    # Create rc file with line
    printf '%s\n' "$PATH_EXPORT_LINE" > "$RC_FILE"
fi

# Print next steps for current shell
echo "Installation complete."
echo ""
echo "To use quant immediately, run:"
printf '  source %q\n' "$RC_FILE"
echo "  quant --help"
echo ""
echo "Or restart your shell."
