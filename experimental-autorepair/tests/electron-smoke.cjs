// Run in Electron, not node. All fixture/profile files live in a supplied temp directory.
const { app } = require("electron");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { installHooks } = require("../autorepair.js");
const directory = process.env.VCC_SMOKE_DIR;
if (!directory || !path.isAbsolute(directory)) throw new Error("VCC_SMOKE_DIR must name a temporary directory");
fs.mkdirSync(path.join(directory, "profile"), { recursive: true });
fs.mkdirSync(path.join(directory, "session"), { recursive: true });
app.setPath("userData", path.join(directory, "profile"));
app.setPath("sessionData", path.join(directory, "session"));
const bundle = path.join(directory, "vencordDesktopMain.js");
const injectionFile = path.resolve(__dirname, "../injection.js");
if (process.argv.includes("--vcc-smoke-child")) {
    assert.equal(fs.readFileSync(bundle, "utf8"), "globalThis.updated = true;\r\n" + fs.readFileSync(injectionFile, "utf8"));
    assert.equal(fs.readFileSync(path.join(directory, "cleaned"), "utf8"), "yes");
    fs.writeFileSync(path.join(directory, "relaunched"), "yes");
    app.exit(0);
}
fs.writeFileSync(bundle, "globalThis.updated = true;\r\n");
installHooks(app, {
    bundle, injectionFile,
    cleanup() { fs.writeFileSync(path.join(directory, "cleaned"), "yes"); }
});
assert.equal(typeof app.exit, "function");
if (process.env.VCC_SMOKE_MODE === "quit") app.whenReady().then(() => app.quit());
else if (process.env.VCC_SMOKE_MODE === "relaunch") {
    app.relaunch({ args: process.argv.slice(1).concat("--vcc-smoke-child") });
    assert.equal(fs.readFileSync(bundle, "utf8"), "globalThis.updated = true;\r\n" + fs.readFileSync(injectionFile, "utf8"));
    app.exit(0);
}
else app.exit(0);
