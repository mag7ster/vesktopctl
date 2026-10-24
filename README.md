# vesktopctl

Control Vesktop mute and deafen from global shortcuts on Linux, including niri and DMS.
Supports Vesktop Flatpak and native packages.

A small JavaScript bridge inside Vesktop receives commands over a Unix socket and clicks
Discord's controls. Commands toggle the current state. No background service or polling.

## Install

Requires Python 3 and an initialized Vesktop installation. Start Vesktop at least once
so it downloads Vencord. The curl installer also requires curl.

```sh
curl -fsSL https://raw.githubusercontent.com/mag7ster/vesktopctl/main/install.sh | sh
```

Or install from a local checkout:

```sh
python3 install.py
```

The installer finds Vencord automatically. If both Flatpak and native Vesktop are installed,
select one with `--target flatpak` or `--target native`. For a custom location, add
`--bundle /path/to/vencordDesktopMain.js`. For curl installation, pass options with
`| sh -s -- --target flatpak`.

The CLI is installed in `~/.local/bin`; add that directory to `PATH` if needed.
Tools live in `~/.local/share/vesktopctl`, and backups in `~/.vesktopctl`.
Flatpak gets access only to the `xdg-run/vesktopctl` runtime directory.

Installation, patching, and removal restart the selected Vesktop only if it was running.
To manage restarts yourself, close Vesktop first and pass `--no-restart`.

## Commands

```sh
vesktopctl mute                    # Toggle microphone mute
vesktopctl deafen                  # Toggle deafen
vesktopctl patch                   # Apply or restore the patch
vesktopctl uninstall --patch-only  # Remove integration, keep the CLI
vesktopctl uninstall               # Remove integration and the CLI
vesktopctl --help
```

Vencord updates can replace the patch. Run `vesktopctl patch` to restore it using the
saved installation selection. It also restores the bridge and Flatpak permission after
`uninstall --patch-only`. No download is needed. Override the selection with
`--target flatpak|native` and, optionally, `--bundle PATH`.

Removal preserves an updated Vencord bundle and restores backed-up files and permissions.
If a managed file has been edited, removal reports the conflict and keeps the tools and
backups so you can resolve it and retry.

## Shortcuts

In `~/.config/niri/config.kdl`:

```kdl
binds {
    Mod+Shift+M repeat=false { spawn "vesktopctl" "mute"; }
    Mod+Shift+D repeat=false { spawn "vesktopctl" "deafen"; }
}
```

In DMS, assign `vesktopctl mute` and `vesktopctl deafen` to your preferred shortcuts.
Use the full path `~/.local/bin/vesktopctl` expanded to your home directory if the
shortcut environment does not include `~/.local/bin` in `PATH`.

## Troubleshooting

- **Cannot contact Vesktop:** start Vesktop; if it is already running, run `vesktopctl patch`.
- **Button not found:** open the Discord window. Discord interface changes may require a selector update.
- **Multiple installations:** use `vesktopctl patch --target flatpak` or `--target native`.

The host socket is `$XDG_RUNTIME_DIR/vesktopctl/control.sock`. This utility uses Discord's
UI, so it requires its controls to be available.

## Credits

This project was developed with the assistance of OpenAI Codex, an AI coding agent.

[vesktopCustomCommands](https://github.com/NitramO-YT/vesktopCustomCommands) was used as a
reference for the JavaScript injection and button-click approach.

[MIT License](LICENSE).
