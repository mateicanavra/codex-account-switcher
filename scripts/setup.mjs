import { execFileSync } from "node:child_process";
import { chmodSync, copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const source = join(root, "assets", "switch_account.py");
const installed = "/Users/Shared/Codex Account Switcher/switch-codex-account.py";
const config = join(homedir(), ".config/codex-account-switcher/devices.json");
const args = process.argv.slice(2);
if (args.length && (args.length !== 2 || args[0] !== "--host" || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(args[1]))) {
  throw new Error("Usage: npm run setup [-- --host SSH_ALIAS]");
}

mkdirSync(dirname(config), { recursive: true });
if (!existsSync(config)) writeFileSync(config, JSON.stringify({ devices: [] }, null, 2) + "\n", { mode: 0o600 });

if (!args.length) {
  mkdirSync(dirname(installed), { recursive: true });
  copyFileSync(source, installed);
  chmodSync(installed, 0o755);
  console.log("Installed the local Codex account-switch helper.");
} else {
  const script = `import json,pathlib,sys
data=json.load(sys.stdin)
helper=pathlib.Path('/Users/Shared/Codex Account Switcher/switch-codex-account.py')
helper.parent.mkdir(parents=True,exist_ok=True)
helper.write_text(data['helper']);helper.chmod(0o755)
config=pathlib.Path.home()/'.config/codex-account-switcher/devices.json'
config.parent.mkdir(parents=True,exist_ok=True)
if data['config'].get('devices') or not config.exists():
    config.write_text(json.dumps(data['config'],indent=2)+'\\n');config.chmod(0o600)
print('Installed the remote helper and private device configuration.')
`;
  const quote = (text) => "'" + text.replaceAll("'", "'\\''") + "'";
  execFileSync("/usr/bin/ssh", ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", args[1], "/usr/bin/python3 -c " + quote(script)], {
    input: JSON.stringify({ helper: readFileSync(source, "utf8"), config: JSON.parse(readFileSync(config, "utf8")) }),
    stdio: ["pipe", "inherit", "inherit"],
  });
}
