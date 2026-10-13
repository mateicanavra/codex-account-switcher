# Codex Account Switcher

Switch saved Codex accounts from Raycast on **This Mac**, a configured Mac over SSH, or **All** configured Macs. The extension closes Codex, switches and verifies the account, then reopens Codex on each selected Mac.

Light and dark icons follow Raycast’s appearance automatically.

Accounts and their names come from Codex Switcher. Add or rename accounts there, then refresh the list in Raycast.

## Install on this Mac

You need macOS, Raycast, the Codex desktop app, Python 3, Node.js 22.22.2 or later, and npm. Install [Codex Switcher](https://github.com/Lampese/codex-switcher/releases) with its bundled `codex-web` executable and save at least one account. Tested with Codex Switcher 0.2.20.

1. Clone this repository and open its directory:

   ```sh
   git clone https://github.com/mateicanavra/codex-account-switcher.git
   cd codex-account-switcher
   ```

2. Install dependencies and the helper:

   ```sh
   npm ci
   npm run setup
   ```

   The helper is installed at `/Users/Shared/Codex Account Switcher/switch-codex-account.py`.

3. Import the extension into Raycast:

   ```sh
   npm run dev
   ```

   After the build, press `Ctrl+C`. The extension stays installed. Run `npm run dev` again after source changes.

4. Open **Switch Codex Account**, select **This Mac**, and choose an account. Local switching works immediately; device configuration is optional until you add other Macs.

## Add another Mac

Each target needs Codex, Codex Switcher with saved accounts, and Python 3. Configure passwordless SSH aliases such as `laptop` and `desktop` on the Mac running Raycast.

1. Verify that SSH works without a password prompt, then install the helper on the target:

   ```sh
   ssh laptop hostname
   npm run setup -- --host laptop
   ```

2. Get each Mac's identity. Run `npm run identify` on this Mac. For a remote Mac, run:

   ```sh
   ssh laptop 'python3 "/Users/Shared/Codex Account Switcher/switch-codex-account.py" --identify'
   ```

   Copy the `identity` value from the returned JSON; keep this output private.

3. Edit `~/.config/codex-account-switcher/devices.json` using [config/devices.example.json](config/devices.example.json). Add every Mac you want to target. Include this Mac if you want it in **All** or available by name from another Mac:

   ```json
   {
     "devices": [
       {
         "id": "desktop",
         "name": "Desktop",
         "ssh": "desktop",
         "identity": "<identity hash from the desktop>"
       },
       {
         "id": "laptop",
         "name": "Laptop",
         "ssh": "laptop",
         "identity": "<identity hash from the laptop>"
       }
     ]
   }
   ```

   Use unique IDs and the returned 64-character identity hashes. Every entry needs an SSH alias. The IDs `local` and `all` are reserved.

4. Run setup again for each remote alias to copy the completed configuration:

   ```sh
   npm run setup -- --host laptop
   ```

   Setup installs the helper and copies configuration to the SSH user's home directory. Local setup preserves existing configuration.

5. Select the configured Mac in **Switch Codex Account**. To run the Raycast command there too, follow the source installation steps.

Keep device configuration private at the path above; it is ignored by Git. Raycast Cloud Sync can sync settings; install source and helper files separately where needed.

## Switching all Macs

**All** requires at least two configured Macs and affects only that configured set. It lists accounts present everywhere, matched by ChatGPT user and workspace identifiers rather than account names. Credentials stay on each Mac; identifiers are derived locally.

The extension checks every target's reachability, machine identity, and account availability before any switch starts. After these checks pass, Macs switch concurrently. Runtime failures can leave some switched and others unchanged; Raycast reports each result. Fix the affected Mac, refresh, and retry.

For duplicate sessions of the same account and workspace, the extension preserves the active session when possible, otherwise choosing the freshest consistently.

## Troubleshooting

| What you see | What to check |
| --- | --- |
| No saved accounts | Add an account in Codex Switcher on the selected Mac, then refresh Raycast. |
| SSH cannot connect or asks for a password | Test `ssh laptop hostname`; check the alias, SSH key, and whether the Mac is awake. |
| Machine identity does not match | Check the SSH alias and enrolled identity. A failed remote request never switches this Mac instead. |
| Helper is missing | Run `npm run setup` on the affected Mac. |
| Codex Switcher or `codex-web` is missing | Install Codex Switcher with its bundled executable on the affected Mac. |
| An account is absent from All | Save that ChatGPT account and workspace on every configured Mac, then refresh. |
| Codex cannot close gracefully | Finish running sessions or close Codex on that Mac, then retry. Its account stays unchanged if closing fails. |
| Codex cannot reopen | Open Codex manually. Check its installation and any macOS permission prompt. |

## Development and sharing

Run the checks from the repository root:

```sh
npm test
npm run typecheck
npm run build
```

Tests cover account identity matching, preflight checks, and concurrent switching.

GitHub provides source for local installation. A Raycast Store listing requires separate submission and review through [Raycast's publishing process](https://developers.raycast.com/basics/publish-an-extension).
