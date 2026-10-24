// Actual Electron renderer + Unix socket + Python CLI. No Discord account/network.
const { app, BrowserWindow } = require("electron");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");
const exec = promisify(execFile);
const directory = process.env.VCC_SMOKE_DIR;
if (!directory || !path.isAbsolute(directory)) throw new Error("VCC_SMOKE_DIR must name a temporary directory");
const runtime = path.join(directory, "run");
for (const name of ["profile", "session", "run", "vesktopctl-experimental"]) fs.mkdirSync(path.join(directory, name), { recursive: true, mode: 0o700 });
app.setPath("userData", path.join(directory, "profile"));
app.setPath("sessionData", path.join(directory, "session"));
// This fixture runs the temporary Electron copy directly, outside Flatpak.
delete process.env.FLATPAK_ID;
process.env.XDG_RUNTIME_DIR = runtime;
for (const name of ["bridge.js", "autorepair.js", "injection.js"]) {
    fs.copyFileSync(path.resolve(__dirname, "..", name), path.join(directory, "vesktopctl-experimental", name));
}
const bundle = path.join(directory, "vencordDesktopMain.js");
const original = "globalThis.smoke = true;\r\n";
fs.writeFileSync(bundle, original);
require(path.join(directory, "vesktopctl-experimental", "bridge.js"));
const server = globalThis.__vesktopctlExperimentalServer;
const listening = server.listening ? Promise.resolve() : new Promise((resolve, reject) => {
    server.once("listening", resolve);
    server.once("error", reject);
});

async function main() {
    await Promise.all([app.whenReady(), listening]);
    const window = new BrowserWindow({ show: false, webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true } });
    await window.loadURL("data:text/html," + encodeURIComponent(`<!doctype html>
        <button aria-label="Mute" onclick="document.body.dataset.mute=String(Number(document.body.dataset.mute||0)+1)">Mute</button>
        <button aria-label="Deafen" onclick="document.body.dataset.deafen=String(Number(document.body.dataset.deafen||0)+1)">Deafen</button>`));
    const cli = path.resolve(__dirname, "../vesktopctl");
    const env = { ...process.env };
    delete env.ELECTRON_RUN_AS_NODE;
    for (const command of ["mute", "deafen", "mute"]) {
        const result = await exec("python3", [cli, command], { env, timeout: 10000 });
        assert.equal(result.stderr, "");
    }
    const state = await window.webContents.executeJavaScript("({ mute: document.body.dataset.mute, deafen: document.body.dataset.deafen })");
    assert.deepEqual(state, { mute: "2", deafen: "1" });
    const address = path.join(runtime, "vesktopctl/control.sock");
    assert.equal(fs.statSync(address).mode & 0o777, 0o600);
    fs.writeFileSync(path.join(directory, "ipc-result.json"), JSON.stringify(state));
    // app.exit is real: the parent must verify the repaired bundle and removed socket.
    app.exit(0);
}
main().catch(error => {
    fs.writeFileSync(path.join(directory, "ipc-error.txt"), String(error.stack));
    console.error(error);
    app.exit(1);
});
