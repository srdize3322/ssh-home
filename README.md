# ssh-home

`ssh-home` is a portable SSH cockpit for people who already manage their machines with OpenSSH config aliases. It is a public project by `srdize3322`.

It reads the local `~/.ssh/config`, shows your SSH aliases in a sober terminal UI, lets you inspect resolved endpoint metadata, browse remote folders, and then drops into a normal SSH shell in the selected directory.

No secrets are bundled. No private inventory is read. Passwords and key prompts stay inside the system `ssh` binary.

## Highlights

- Global command: `ssh-home`
- Source of truth: your local OpenSSH config
- TUI with keyboard navigation, favorites, recents, filters and help
- Extra endpoint metadata: `HostName`, `User`, `Port`, `ProxyJump`, `IdentityFile`
- Add new SSH endpoints with `ssh-home --add` or the `n` shortcut in the TUI
- Remote directory browser before opening the final shell
- Clean terminal handoff: the TUI exits before the final SSH session starts
- Terminal mode cleanup before SSH so mouse scrolling behaves normally in the shell
- Local-only state for favorites, recents and last paths
- Python stdlib only, no package install required

## Install

Clone the repo:

```bash
git clone https://github.com/srdize3322/ssh-home.git
cd ssh-home
chmod +x ssh-home ssh-home.py
```

Make the command available from anywhere:

```bash
mkdir -p "$HOME/.local/bin"
ln -sfn "$PWD/ssh-home" "$HOME/.local/bin/ssh-home"
```

Make sure `~/.local/bin` is in your `PATH`, then run:

```bash
ssh-home
```

## One-Line Run

Run directly from GitHub without installing:

```bash
curl -fsSL https://raw.githubusercontent.com/srdize3322/ssh-home/main/ssh-home.py | python3 -
```

Or download to a temporary path:

```bash
curl -fsSL https://raw.githubusercontent.com/srdize3322/ssh-home/main/ssh-home.py -o /tmp/ssh-home.py
python3 /tmp/ssh-home.py
```

## Daily Usage

Open the interactive cockpit:

```bash
ssh-home
```

List detected aliases:

```bash
ssh-home --list
```

List aliases with resolved OpenSSH metadata:

```bash
ssh-home --list --show-resolved
```

Jump directly to a host:

```bash
ssh-home --host app-prod
```

Jump directly to a host and path:

```bash
ssh-home --host app-prod --path /srv/app/current
```

Use a custom config file:

```bash
ssh-home --config ./examples/ssh_config
```

## Add Endpoints

Start the interactive endpoint wizard:

```bash
ssh-home --add
```

Add an endpoint non-interactively:

```bash
ssh-home --add \
  --add-alias app-prod \
  --hostname 203.0.113.10 \
  --ssh-user deploy \
  --port 2222
```

Optional fields:

```bash
ssh-home --add \
  --add-alias app-via-gateway \
  --hostname 198.51.100.7 \
  --ssh-user root \
  --proxyjump gateway \
  --identity-file ~/.ssh/id_example
```

Inside the TUI, press `n` from the host list to add a new endpoint without leaving the app.

`ssh-home` appends a clean `Host` block to the config file selected by `--config` or `~/.ssh/config`. It does not store passwords.

## TUI Shortcuts

The host metadata panel stays beside the host list whenever there is enough horizontal room. Only when the side panel would become too narrow, it moves below the list instead of disappearing.

- `Up` / `Down`: move selection
- `Enter`: open host, enter directory, or use current directory
- type: filter visible hosts or directories
- `Backspace`: edit filter, or go up one remote directory when the filter is empty
- `Left`: go up one remote directory
- `/`: type a manual remote path
- `n`: add a new SSH endpoint from the host list
- `f`: toggle favorite
- `l`: jump to last path for the selected host
- `r`: show recents on host list, or refresh remote directory
- `a`: show all hosts
- `Tab`: cycle host views or return to host list
- `?`: help
- `q`: quit

## Local State

By default, local convenience state lives at:

```bash
~/.config/ssh-home/state.json
```

It can contain SSH aliases, favorites, recent timestamps and last remote paths. It does not store passwords, private keys, resolved IP inventory or environment variables.

Use another state file:

```bash
ssh-home --state-file /tmp/ssh-home-state.json
```

Disable state:

```bash
ssh-home --no-state
```

Clear recents and last paths while keeping favorites:

```bash
ssh-home --clear-history
```

## Security Model

- Public repo contains no real hosts, private paths, credentials or environment files.
- Runtime host data comes only from the machine where the command runs.
- Authentication is handled by OpenSSH, not by `ssh-home`.
- New endpoints are appended to the selected SSH config as plain OpenSSH config blocks.
- The temporary SSH control socket is created under `/tmp` and cleaned up on exit.

## Development

Run tests:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

Compile-check the script:

```bash
python3 -m py_compile ssh-home.py
```

## Requirements

- macOS or Linux
- `python3`
- OpenSSH client with `ssh`
- A valid SSH config, or use `ssh-home --add` to create the first endpoint

## License

MIT
