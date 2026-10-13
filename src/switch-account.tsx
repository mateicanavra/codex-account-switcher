import { Action, ActionPanel, Detail, environment, Icon, List, Toast, showToast } from "@raycast/api";
import { execFile } from "node:child_process";
import path from "node:path";
import { promisify } from "node:util";
import { useCallback, useEffect, useRef, useState } from "react";

const run = promisify(execFile);
const helper = path.join(environment.assetsPath, "switch_account.py");

type Device = { id: string; name: string };
type DeviceConfig = { devices: Device[]; current_id: string | null };
type Account = {
  id: string;
  name: string;
  is_active: boolean;
  identity?: string;
  active_count?: number;
  device_count?: number;
};
type Target = { value: string; title: string };
type RunResult = { targetName: string; accountName: string; succeeded: boolean; stdout: string; stderr: string };

class ResponseError extends Error {}

function output(error: unknown): { stdout: string; stderr: string } {
  const failure = error as { stdout?: unknown; stderr?: unknown } | null;
  return {
    stdout: typeof failure?.stdout === "string" ? failure.stdout.trim() : "",
    stderr: typeof failure?.stderr === "string" ? failure.stderr.trim() : "",
  };
}

function message(error: unknown): string {
  if (error instanceof ResponseError) return error.message;
  const { stderr } = output(error);
  if (stderr && !stderr.includes("Traceback (most recent call last)")) return stderr;
  return "The account helper could not finish. Check the device connection and private configuration, then refresh.";
}

function readDevices(stdout: string): DeviceConfig {
  let config: DeviceConfig;
  try {
    config = JSON.parse(stdout) as DeviceConfig;
  } catch {
    throw new ResponseError("The device configuration could not be read. Check the private configuration, then refresh.");
  }
  if (
    !config ||
    !Array.isArray(config.devices) ||
    !config.devices.every((device) => device && typeof device.id === "string" && typeof device.name === "string")
  ) {
    throw new ResponseError("The device configuration is invalid. Check the private configuration, then refresh.");
  }
  return config;
}

function readAccounts(stdout: string): Account[] {
  let accounts: Account[];
  try {
    accounts = JSON.parse(stdout) as Account[];
  } catch {
    throw new ResponseError("The saved account list could not be read. Refresh to try again.");
  }
  if (
    !Array.isArray(accounts) ||
    !accounts.every(
      (account) => account && typeof account.id === "string" && typeof account.name === "string" && typeof account.is_active === "boolean",
    )
  ) {
    throw new ResponseError("The saved account list is invalid. Refresh to try again.");
  }
  return accounts;
}

async function invoke(args: string[]) {
  return run("/usr/bin/python3", [helper, ...args], { timeout: 150_000, maxBuffer: 512 * 1024 });
}

function resultMarkdown(result: RunResult): string {
  const lines = [
    `# ${result.succeeded ? "Account switch completed" : "Account switch needs attention"}`,
    `Target: ${result.targetName}`,
    `Account: ${result.accountName}`,
  ];
  for (const [label, value] of [["Result", result.stdout], ["Details", result.stderr]]) {
    if (value) lines.push(`## ${label}\n\n${value.split("\n").map((line) => `    ${line}`).join("\n")}`);
  }
  if (!result.stdout && !result.stderr) {
    lines.push(result.succeeded ? "The helper completed successfully." : "The helper could not finish. Check the connection and refresh before trying again.");
  }
  return lines.join("\n\n");
}

export default function Command() {
  const [machine, setMachine] = useState("local");
  const [devices, setDevices] = useState<Device[]>([]);
  const [data, setData] = useState<{ machine: string; accounts: Account[] }>();
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [revision, setRevision] = useState(0);
  const [lastResult, setLastResult] = useState<RunResult>();
  const running = useRef(false);
  const machines: Target[] = [
    { value: "local", title: "This Mac" },
    ...(devices.length > 1 ? [{ value: "all", title: "All devices" }] : []),
    ...devices.map((device) => ({ value: device.id, title: device.name })),
  ];
  const targetName = machines.find((item) => item.value === machine)?.title || "selected device";
  const refresh = useCallback(() => setRevision((value) => value + 1), []);

  useEffect(() => {
    let disposed = false;
    setLoading(true);
    setError(undefined);
    setData(undefined);
    async function load() {
      try {
        const deviceResponse = await invoke(["--devices"]);
        const config = readDevices(deviceResponse.stdout);
        if (disposed) return;
        setDevices(config.devices);
        if (
          (machine === "all" && config.devices.length <= 1) ||
          (machine !== "local" && machine !== "all" && !config.devices.some((device) => device.id === machine))
        ) {
          setMachine("local");
          return;
        }
        const accountResponse = await invoke([machine, "--list"]);
        const accounts = readAccounts(accountResponse.stdout);
        if (!disposed) setData({ machine, accounts });
      } catch (failure) {
        if (!disposed) setError(message(failure));
      } finally {
        if (!disposed) setLoading(false);
      }
    }
    void load();
    return () => {
      disposed = true;
    };
  }, [machine, revision]);

  async function switchAccount(account: Account) {
    if (running.current || loading || data?.machine !== machine) return;
    const runTarget = machine;
    const runTargetName = targetName;
    const accountName = account.name || "Unnamed saved account";
    running.current = true;
    setBusy(true);
    let toast: Toast | undefined;
    try {
      toast = await showToast({ style: Toast.Style.Animated, title: `Switching on ${runTargetName}`, message: accountName }).catch(() => undefined);
      let result: RunResult;
      try {
        const response = await invoke([runTarget, runTarget === "all" ? "--identity" : "--account-id", account.id]);
        result = { targetName: runTargetName, accountName, succeeded: true, stdout: response.stdout.trim(), stderr: response.stderr.trim() };
      } catch (failure) {
        result = { targetName: runTargetName, accountName, succeeded: false, ...output(failure) };
      }
      setLastResult(result);
      // Toasts are feedback only; a toast failure must never strand the command in its busy state.
      try {
        if (toast) {
          toast.style = result.succeeded ? Toast.Style.Success : Toast.Style.Failure;
          toast.title = result.succeeded ? `Codex is running on ${runTargetName}` : "Account switch needs attention";
          toast.message = result.succeeded ? accountName : "Open View Last Result for device outcomes.";
        }
      } catch {
        // The result remains available even if Raycast has dismissed the toast.
      }
    } finally {
      running.current = false;
      setBusy(false);
      refresh();
    }
  }

  const accounts = data?.machine === machine ? data.accounts : [];
  const refreshAction = <Action title="Refresh Accounts" icon={Icon.ArrowClockwise} onAction={refresh} shortcut={{ modifiers: ["cmd"], key: "r" }} />;
  const resultAction = lastResult ? (
    <Action.Push title="View Last Result" icon={Icon.Document} target={<Detail navigationTitle="Last Account Switch" markdown={resultMarkdown(lastResult)} />} />
  ) : undefined;
  return (
    <List
      isLoading={loading || busy}
      navigationTitle={busy ? `Switching on ${targetName}…` : "Switch Codex Account"}
      searchBarPlaceholder={machine === "all" ? "Search accounts shared by all devices…" : "Search saved accounts…"}
      searchBarAccessory={busy ? undefined : (
        <List.Dropdown tooltip="Choose the target device" value={machine} onChange={setMachine}>
          {machines.map((item) => <List.Dropdown.Item key={item.value} value={item.value} title={item.title} />)}
        </List.Dropdown>
      )}
    >
      {error ? (
        <List.EmptyView title={`Cannot load accounts on ${targetName}`} description={error} icon={Icon.ExclamationMark} actions={<ActionPanel>{refreshAction}{resultAction}</ActionPanel>} />
      ) : !loading && accounts.length === 0 ? (
        <List.EmptyView
          title={machine === "all" ? "No accounts shared by every device" : "No saved Codex accounts"}
          description={machine === "all" ? "Save the same account on every configured device, then refresh. Each device must be reachable." : `Add an account in Codex Switcher on ${targetName}, then refresh.`}
          actions={<ActionPanel>{refreshAction}{resultAction}</ActionPanel>}
        />
      ) : accounts.map((account) => (
        <List.Item
          key={account.id}
          title={account.name || "Unnamed saved account"}
          subtitle={machine === "all" ? `Available on all ${account.device_count ?? devices.length} devices` : undefined}
          icon={account.is_active ? Icon.CheckCircle : Icon.Person}
          accessories={machine === "all"
            ? [{ tag: `Active on ${account.active_count ?? (account.is_active ? account.device_count ?? devices.length : 0)} of ${account.device_count ?? devices.length}` }]
            : account.is_active ? [{ tag: "Active" }] : []}
          actions={<ActionPanel>
            <Action title={machine === "all" ? "Switch and Reopen Codex on All Devices" : "Switch and Reopen Codex"} icon={Icon.ArrowClockwise} onAction={() => switchAccount(account)} />
            {resultAction}
            {refreshAction}
          </ActionPanel>}
        />
      ))}
    </List>
  );
}
