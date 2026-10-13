"use strict";

const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

function openWorkerLog(profile) {
  const directory = path.join(profile, "local-asr-logs");
  fs.mkdirSync(directory, { recursive: true });
  const output = path.join(directory, "worker.log");
  if (fs.existsSync(output) && fs.statSync(output).size > 1024 * 1024) {
    fs.rmSync(output + ".previous", { force: true });
    fs.renameSync(output, output + ".previous");
  }
  return fs.openSync(output, "a");
}

// Only the primary app instance calls this after app.whenReady(). The Python
// worker owns a Windows Job, so model children cannot outlive a crashed app.
class LocalAsrRuntime {
  constructor({ profile, executable = process.execPath, logger, spawnProcess = spawn,
    readConfig = () => JSON.parse(fs.readFileSync(path.join(profile, "local-asr-runtime.json"), "utf8").replace(/^\uFEFF/, "")),
    retryMs = 5000, openLog = openWorkerLog }) {
    Object.assign(this, { profile, executable, logger, spawnProcess, readConfig, retryMs, openLog });
    this.child = null;
    this.timer = null;
    this.stopping = false;
  }

  start() {
    if (this.stopping || this.child || this.timer) return;
    let config;
    try { config = this.readConfig(); }
    catch (err) {
      if (err.code !== "ENOENT") this.logger.warn("Local ASR configuration unavailable", { error: err.message });
      return;
    }
    if (config.enabled !== true) return;
    if (![config.pythonw, config.supervisor].every(value => typeof value === "string" && path.isAbsolute(value))) {
      this.logger.error("Local ASR configuration requires absolute worker paths");
      return;
    }
    let child;
    let output;
    try {
      output = this.openLog(this.profile);
      child = this.spawnProcess(config.pythonw, ["-X", "utf8", config.supervisor,
        "--config", path.join(this.profile, "local-asr-runtime.json"),
        "--parent-pid", String(process.pid), "--parent-exe", this.executable], {
        windowsHide: true, shell: false, stdio: output == null ? "ignore" : ["ignore", output, output],
        cwd: path.dirname(config.supervisor),
      });
    } catch (err) {
      this.logger.error("Local ASR worker failed to launch", { error: err.message });
      this._retry();
      return;
    } finally {
      if (output != null) fs.closeSync(output);
    }
    this.child = child;
    let finished = false;
    const onExit = (code, signal) => {
      if (finished) return;
      finished = true;
      if (this.child === child) this.child = null;
      if (!this.stopping) {
        this.logger.warn("Local ASR worker exited; retrying in background", {
          code: code instanceof Error ? undefined : code,
          error: code instanceof Error ? code.message : undefined, signal,
        });
        this._retry();
      }
    };
    child.once("error", onExit);
    child.once("exit", onExit);
    this.logger.info("Local ASR background worker started", { pid: child.pid });
  }

  _retry() {
    if (this.stopping || this.timer) return;
    this.timer = setTimeout(() => { this.timer = null; this.start(); }, this.retryMs);
    this.timer.unref?.();
  }

  stop() {
    this.stopping = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    const child = this.child;
    if (!child) return Promise.resolve();
    return new Promise(resolve => {
      const deadline = setTimeout(resolve, 3000);
      child.once("exit", () => { clearTimeout(deadline); resolve(); });
      // Windows closes the worker's Job handle on termination, stopping only
      // its own ASR/adapter children. Unrelated Python/model processes are safe.
      child.kill();
    });
  }
}

module.exports = { LocalAsrRuntime };
