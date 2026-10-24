"use strict";

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const vm = require("node:vm");

const BEGIN = "/* vesktopctl-experimental begin */";
const END = "/* vesktopctl-experimental end */";
const START = `\n;${BEGIN}\n`;
const FINISH = `${END}\n`;
const HOOKS = Symbol.for("vesktopctl.experimental.lifecycle");

function stripPatch(content) {
    const count = marker => content.split(marker).length - 1;
    if (count(BEGIN) !== 1 || count(END) !== 1) {
        throw new Error("incomplete or duplicated experimental patch");
    }
    const start = content.indexOf(START);
    const end = content.indexOf(FINISH);
    if (start < 0 || end < start) throw new Error("invalid experimental patch boundaries");
    return content.slice(0, start) + content.slice(end + FINISH.length);
}

function patchContent(content, injection) {
    if (!injection.startsWith(START) || !injection.endsWith(FINISH) || stripPatch(injection) !== "") {
        throw new Error("invalid injection.js");
    }
    if (content.includes("/* vesktopctl begin */") || content.includes("VesktopCustomCommands Injection")) {
        throw new Error("another VCC patch is installed; remove it before using the experiment");
    }
    const clean = content.includes(BEGIN) || content.includes(END) ? stripPatch(content) : content;
    const patched = clean + injection;
    // Compile without executing either the upstream code or our injection.
    new vm.Script(patched, { filename: "vencordDesktopMain.js" });
    return patched;
}

function sameFile(a, b) {
    return ["dev", "ino", "size", "mtimeNs", "ctimeNs"].every(key => a[key] === b[key]);
}

function repair(bundle, injectionFile) {
    const before = fs.lstatSync(bundle, { bigint: true });
    if (!before.isFile()) throw new Error("bundle must be a regular file, not a symlink");
    const bytes = fs.readFileSync(bundle);
    const content = bytes.toString("utf8");
    if (!Buffer.from(content, "utf8").equals(bytes)) throw new Error("bundle is not valid UTF-8");
    const injection = fs.readFileSync(injectionFile, "utf8");
    const patched = patchContent(content, injection);
    if (patched === content) return false;

    const temporary = path.join(path.dirname(bundle), `.vesktopctl-experimental-${crypto.randomBytes(8).toString("hex")}.tmp`);
    let fd;
    try {
        fd = fs.openSync(temporary, "wx", Number(before.mode & 0o777n));
        fs.writeFileSync(fd, patched, "utf8");
        fs.fchmodSync(fd, Number(before.mode & 0o777n));
        fs.fsyncSync(fd);
        fs.closeSync(fd);
        fd = undefined;
        if (!sameFile(before, fs.lstatSync(bundle, { bigint: true }))) {
            throw new Error("bundle changed during repair; leaving it untouched");
        }
        fs.renameSync(temporary, bundle);
        return true;
    } finally {
        if (fd !== undefined) fs.closeSync(fd);
        try { fs.unlinkSync(temporary); } catch (error) { if (error.code !== "ENOENT") throw error; }
    }
}

function installHooks(app, { bundle, injectionFile, cleanup = () => {}, logger = console }) {
    if (app[HOOKS]) return app[HOOKS];
    const originalRelaunch = app.relaunch;
    const originalExit = app.exit;
    let repairing = false;
    function check() {
        if (repairing) return;
        repairing = true;
        try {
            if (repair(bundle, injectionFile)) logger.info("[vesktopctl-experimental] patch restored before shutdown");
        } catch (error) {
            logger.error("[vesktopctl-experimental] autorepair failed; use the launcher to retry:", error);
        } finally {
            repairing = false;
        }
    }
    function close() {
        try { cleanup(); } catch (error) { logger.error("[vesktopctl-experimental] socket cleanup failed:", error); }
    }
    function relaunch(...args) {
        check();
        return Reflect.apply(originalRelaunch, this, args);
    }
    function exit(...args) {
        check();
        close();
        return Reflect.apply(originalExit, this, args);
    }
    const beforeQuit = () => { check(); close(); };
    try {
        app.relaunch = relaunch;
        app.exit = exit;
        app.on("before-quit", beforeQuit);
        app[HOOKS] = { check };
    } catch (error) {
        if (app.relaunch === relaunch) app.relaunch = originalRelaunch;
        if (app.exit === exit) app.exit = originalExit;
        app.removeListener("before-quit", beforeQuit);
        throw error;
    }
    return app[HOOKS];
}

module.exports = { BEGIN, END, stripPatch, patchContent, repair, installHooks };
