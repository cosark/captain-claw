/**
 * Captain Claw – Electron main process.
 *
 * Lifecycle:
 *   1. Spawn the PyInstaller-built backend (captain-claw-web) as a child process.
 *   2. Wait for the backend HTTP server to become ready.
 *   3. Open a BrowserWindow pointing at the backend's URL.
 *   4. On quit, gracefully shut down the backend.
 */

const { app, BrowserWindow, Menu, shell, dialog } = require("electron");
const { spawn, execFileSync } = require("child_process");
const path = require("path");
const http = require("http");
const net = require("net");
const fs = require("fs");
const log = require("electron-log/main");

// ── Constants ──────────────────────────────────────────────────

const BACKEND_PORT = 23080;
const BACKEND_HOST = "127.0.0.1";
const BACKEND_URL = `http://${BACKEND_HOST}:${BACKEND_PORT}`;
// The app's own Flight Deck port — not the usual 25080: a deck already
// running there (a developer's) would answer the health check and the window
// would show that deck instead. The next free port up to FD_PORT_LAST if busy.
const FD_PORT = 25090;
const FD_PORT_LAST = 25099;
// Earlier builds ran it on 25080: an install upgraded from one keeps that
// port (and its window's origin, with the page storage on it) while it's free.
const LEGACY_FD_PORT = 25080;
// The port is kept from launch to launch (desktop-fd.json in the user data
// dir): the window's origin, and with it everything a deck without sign-in
// keeps in the page's storage, stays the same.

// One app at a time: a second launch focuses this window instead of starting
// a second Flight Deck on the same data dir.
const GOT_LOCK = app.requestSingleInstanceLock();
if (!GOT_LOCK) app.quit();
const FD_HOST = "127.0.0.1";
const HEALTH_POLL_MS = 300;
const HEALTH_TIMEOUT_MS = 30_000;
const DEV_MODE = process.argv.includes("--dev");

// ── State ──────────────────────────────────────────────────────

let mainWindow = null;
let backendProcess = null;
let fdProcess = null;
let backendReady = false;
let fdReady = false;
let actualPort = BACKEND_PORT;
let actualFdPort = FD_PORT;

// ── Paths ──────────────────────────────────────────────────────

function getBackendBinary() {
  const exeName =
    process.platform === "win32" ? "captain-claw-web.exe" : "captain-claw-web";

  if (DEV_MODE) {
    // In dev mode, look in the project dist/ folder
    const devPath = path.join(__dirname, "..", "dist", "captain-claw", exeName);
    if (fs.existsSync(devPath)) return devPath;

    // Fallback: try running from source via python
    return null;
  }

  // In production: binary is in resources/backend/
  const prodPath = path.join(process.resourcesPath, "backend", exeName);
  if (fs.existsSync(prodPath)) return prodPath;

  return null;
}

function getFdBinary() {
  const exeName =
    process.platform === "win32" ? "captain-claw-fd.exe" : "captain-claw-fd";

  if (DEV_MODE) {
    const devPath = path.join(__dirname, "..", "dist", "captain-claw", exeName);
    if (fs.existsSync(devPath)) return devPath;
    return null;
  }

  const prodPath = path.join(process.resourcesPath, "backend", exeName);
  if (fs.existsSync(prodPath)) return prodPath;

  return null;
}

function getUserDataDir() {
  // Captain Claw stores data in ~/.captain-claw by default.
  // We can override via CAPTAIN_CLAW_HOME for desktop packaging.
  const home =
    process.env.CAPTAIN_CLAW_HOME ||
    path.join(app.getPath("home"), ".captain-claw");
  if (!fs.existsSync(home)) {
    fs.mkdirSync(home, { recursive: true });
  }
  return home;
}

// ── Backend management ─────────────────────────────────────────

function spawnBackend() {
  const binary = getBackendBinary();

  let cmd, args, env;

  env = {
    ...process.env,
    CAPTAIN_CLAW_HOME: getUserDataDir(),
  };

  if (binary) {
    cmd = binary;
    args = [];
    log.info(`Starting backend: ${binary}`);
  } else if (DEV_MODE) {
    // Dev fallback: run from source
    cmd = process.platform === "win32" ? "python" : "python3";
    args = ["-m", "captain_claw.web_server"];
    env.PYTHONPATH = path.join(__dirname, "..");
    log.info("Starting backend from source (dev mode)");
  } else {
    dialog.showErrorBox(
      "Captain Claw",
      "Backend binary not found. The application may not have been packaged correctly."
    );
    app.quit();
    return;
  }

  backendProcess = spawn(cmd, args, {
    env,
    cwd: getUserDataDir(),
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });

  backendProcess.stdout.on("data", (data) => {
    const text = data.toString().trim();
    if (text) log.info(`[backend] ${text}`);

    // Detect actual port from backend output
    const portMatch = text.match(/running at http:\/\/[^:]+:(\d+)/);
    if (portMatch) {
      actualPort = parseInt(portMatch[1], 10);
      log.info(`Backend port detected: ${actualPort}`);
    }
  });

  backendProcess.stderr.on("data", (data) => {
    const text = data.toString().trim();
    if (text) log.warn(`[backend:err] ${text}`);
  });

  backendProcess.on("exit", (code, signal) => {
    log.info(`Backend exited: code=${code} signal=${signal}`);
    backendProcess = null;

    if (backendReady && mainWindow) {
      // Backend crashed while running – show error
      dialog
        .showMessageBox(mainWindow, {
          type: "error",
          title: "Captain Claw",
          message: "The backend process has stopped unexpectedly.",
          buttons: ["Restart", "Quit"],
        })
        .then(({ response }) => {
          if (response === 0) {
            backendReady = false;
            spawnBackend();
            waitForBackend().then(() => mainWindow?.loadURL(getBackendUrl()));
          } else {
            app.quit();
          }
        });
    }
  });
}

function getBackendUrl() {
  return `http://${BACKEND_HOST}:${actualPort}`;
}

function waitForBackend() {
  return new Promise((resolve, reject) => {
    const start = Date.now();

    function poll() {
      const url = `http://${BACKEND_HOST}:${actualPort}/api/commands`;
      http
        .get(url, (res) => {
          if (res.statusCode === 200) {
            backendReady = true;
            log.info("Backend is ready");
            resolve();
          } else {
            retry();
          }
        })
        .on("error", () => retry());
    }

    function retry() {
      if (Date.now() - start > HEALTH_TIMEOUT_MS) {
        reject(new Error("Backend did not start within timeout"));
        return;
      }
      setTimeout(poll, HEALTH_POLL_MS);
    }

    poll();
  });
}

function stopBackend() {
  if (!backendProcess) return;

  log.info("Stopping backend...");

  if (process.platform === "win32") {
    // On Windows, spawn taskkill for the process tree
    spawn("taskkill", ["/pid", backendProcess.pid.toString(), "/f", "/t"]);
  } else {
    backendProcess.kill("SIGTERM");

    // Force kill after 5 seconds
    setTimeout(() => {
      if (backendProcess) {
        log.warn("Backend did not exit gracefully, force killing");
        backendProcess.kill("SIGKILL");
      }
    }, 5000);
  }
}

// ── Flight Deck management ─────────────────────────────────────

function spawnFlightDeck() {
  const binary = getFdBinary();

  let cmd, args, env;

  env = {
    ...process.env,
    FD_DATA_DIR: path.join(getUserDataDir(), "fd-data"),
    FD_AUTH_ENABLED: "false",
  };

  if (binary) {
    cmd = binary;
    args = ["--host", FD_HOST, "--port", String(actualFdPort)];
    log.info(`Starting Flight Deck: ${binary}`);
  } else if (DEV_MODE) {
    cmd = process.platform === "win32" ? "python" : "python3";
    args = ["-m", "captain_claw.flight_deck.server", "--host", FD_HOST, "--port", String(actualFdPort)];
    env.PYTHONPATH = path.join(__dirname, "..");
    log.info("Starting Flight Deck from source (dev mode)");
  } else {
    log.error("Flight Deck binary not found");
    return;
  }

  fdProcess = spawn(cmd, args, {
    env,
    cwd: getUserDataDir(),
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });

  fdProcess.stdout.on("data", (data) => {
    const text = data.toString().trim();
    if (text) log.info(`[flight-deck] ${text}`);

    const portMatch = text.match(/running on http:\/\/[^:]+:(\d+)/i) ||
                      text.match(/Uvicorn running on http:\/\/[^:]+:(\d+)/i);
    if (portMatch) {
      actualFdPort = parseInt(portMatch[1], 10);
      log.info(`Flight Deck port detected: ${actualFdPort}`);
    }
  });

  fdProcess.stderr.on("data", (data) => {
    const text = data.toString().trim();
    if (text) log.warn(`[flight-deck:err] ${text}`);

    // uvicorn logs to stderr
    const portMatch = text.match(/Uvicorn running on http:\/\/[^:]+:(\d+)/i);
    if (portMatch) {
      actualFdPort = parseInt(portMatch[1], 10);
      log.info(`Flight Deck port detected: ${actualFdPort}`);
    }
  });

  writeFdState({ port: actualFdPort, pid: fdProcess.pid });

  fdProcess.on("exit", (code, signal) => {
    log.info(`Flight Deck exited: code=${code} signal=${signal}`);
    fdProcess = null;
    writeFdState({ port: actualFdPort });
  });
}

// ── The app's own Flight Deck between launches ─────────────────

function fdStateFile() {
  return path.join(getUserDataDir(), "desktop-fd.json");
}

function readFdState() {
  try {
    return JSON.parse(fs.readFileSync(fdStateFile(), "utf8")) || {};
  } catch {
    return {};
  }
}

function writeFdState(state) {
  try {
    fs.mkdirSync(getUserDataDir(), { recursive: true });
    fs.writeFileSync(fdStateFile(), JSON.stringify(state));
  } catch (err) {
    log.warn(`Could not save ${fdStateFile()}: ${err.message}`);
  }
}

function alive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

// Whether a pid is still the Flight Deck an earlier launch started (and not
// some other process that got the number since). Windows can't be told
// apart this way, so there it is left alone.
function isOurFd(pid, port) {
  if (process.platform === "win32" || !port) return false;
  try {
    const cmd = execFileSync("ps", ["-p", String(pid), "-o", "command="], { encoding: "utf8" });
    return /captain-claw-fd|captain_claw\.flight_deck\.server/.test(cmd)
      && new RegExp(`--port\\s+${Number(port)}(\\s|$)`).test(cmd);
  } catch {
    return false;
  }
}

// A crash or force-quit skips before-quit, leaving the earlier Flight Deck
// running on its port: stop it, or this launch would start a second one on
// the same data dir.
async function stopLeftoverFd() {
  const { pid, port } = readFdState();
  if (!pid || !alive(pid) || !isOurFd(pid, port)) return;
  log.warn(`Stopping the Flight Deck left over from an earlier run (pid ${pid})`);
  try {
    process.kill(pid, "SIGTERM");
  } catch {
    return;
  }
  for (let i = 0; i < 50 && alive(pid); i++) await new Promise((r) => setTimeout(r, 100));
  if (alive(pid)) {
    try { process.kill(pid, "SIGKILL"); } catch { /* already gone */ }
  }
}

function portFree(port) {
  return new Promise((resolve) => {
    const probe = net.createServer();
    probe.once("error", () => resolve(false));
    probe.once("listening", () => probe.close(() => resolve(true)));
    probe.listen(port, FD_HOST);
  });
}

async function pickFdPort(preferred) {
  const ports = [];
  if (preferred === LEGACY_FD_PORT || (preferred >= FD_PORT && preferred <= FD_PORT_LAST)) ports.push(preferred);
  for (let port = FD_PORT; port <= FD_PORT_LAST; port++) if (port !== preferred) ports.push(port);
  for (const port of ports) {
    if (await portFree(port)) return port;
    log.warn(`Port ${port} is busy — trying the next one`);
  }
  throw new Error(`No free port for Flight Deck between ${FD_PORT} and ${FD_PORT_LAST}`);
}

function getFdUrl() {
  return `http://${FD_HOST}:${actualFdPort}`;
}

function waitForFlightDeck() {
  return new Promise((resolve, reject) => {
    const start = Date.now();

    function poll() {
      const url = `http://${FD_HOST}:${actualFdPort}/api/health`;
      http
        .get(url, (res) => {
          if (res.statusCode === 200 || res.statusCode === 404) {
            // 404 is OK — means server is up, just no /api/health route
            fdReady = true;
            log.info("Flight Deck is ready");
            resolve();
          } else {
            retry();
          }
        })
        .on("error", () => retry());
    }

    function retry() {
      if (Date.now() - start > HEALTH_TIMEOUT_MS) {
        reject(new Error("Flight Deck did not start within timeout"));
        return;
      }
      setTimeout(poll, HEALTH_POLL_MS);
    }

    poll();
  });
}

function stopFlightDeck() {
  if (!fdProcess) return;

  log.info("Stopping Flight Deck...");

  if (process.platform === "win32") {
    spawn("taskkill", ["/pid", fdProcess.pid.toString(), "/f", "/t"]);
  } else {
    fdProcess.kill("SIGTERM");

    setTimeout(() => {
      if (fdProcess) {
        log.warn("Flight Deck did not exit gracefully, force killing");
        fdProcess.kill("SIGKILL");
      }
    }, 5000);
  }
}

// ── Window management ──────────────────────────────────────────

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 800,
    minHeight: 600,
    title: "Captain Claw",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      nodeIntegration: false,
      contextIsolation: true,
    },
    show: false,
  });

  // Show when ready to prevent visual flash
  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
  });

  // Open external links in the default browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http")) shell.openExternal(url);
    return { action: "deny" };
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

function buildMenu() {
  const isMac = process.platform === "darwin";

  const template = [
    ...(isMac
      ? [
          {
            label: "Captain Claw",
            submenu: [
              { role: "about" },
              { type: "separator" },
              { role: "services" },
              { type: "separator" },
              { role: "hide" },
              { role: "hideOthers" },
              { role: "unhide" },
              { type: "separator" },
              { role: "quit" },
            ],
          },
        ]
      : []),
    {
      label: "Edit",
      submenu: [
        { role: "undo" },
        { role: "redo" },
        { type: "separator" },
        { role: "cut" },
        { role: "copy" },
        { role: "paste" },
        { role: "selectAll" },
      ],
    },
    {
      label: "View",
      submenu: [
        { role: "reload" },
        { role: "forceReload" },
        { role: "toggleDevTools" },
        { type: "separator" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
      ],
    },
    {
      label: "Window",
      submenu: [
        { role: "minimize" },
        { role: "zoom" },
        ...(isMac
          ? [{ type: "separator" }, { role: "front" }]
          : [{ role: "close" }]),
      ],
    },
  ];

  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ── App lifecycle ──────────────────────────────────────────────

app.on("second-instance", () => {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.focus();
});

app.whenReady().then(async () => {
  if (!GOT_LOCK) return;
  log.info("Captain Claw Desktop starting...");
  log.info(`Platform: ${process.platform} ${process.arch}`);
  log.info(`Dev mode: ${DEV_MODE}`);
  log.info(`User data: ${getUserDataDir()}`);

  buildMenu();
  createWindow();

  // Show a loading state
  mainWindow.loadURL(
    `data:text/html;charset=utf-8,${encodeURIComponent(LOADING_HTML)}`
  );

  try {
    // Backend is optional — start it but don't block on it
    spawnBackend();
    waitForBackend()
      .then(() => log.info("Backend ready (background)"))
      .catch((err) => log.warn("Backend failed to start (non-fatal):", err.message));

    // Flight Deck is the primary UI — must start
    await stopLeftoverFd();
    // No port saved yet but the window has page storage: an upgrade from a
    // build that ran on 25080 — keep its origin while 25080 is free.
    const saved = readFdState().port;
    const upgraded = saved === undefined && fs.existsSync(path.join(app.getPath("userData"), "Local Storage"));
    actualFdPort = await pickFdPort(saved ?? (upgraded ? LEGACY_FD_PORT : undefined));
    spawnFlightDeck();
    await waitForFlightDeck();
    mainWindow.loadURL(getFdUrl());
  } catch (err) {
    log.error("Failed to start Flight Deck:", err);
    dialog.showErrorBox(
      "Captain Claw",
      `Failed to start Flight Deck:\n\n${err.message}\n\nCheck the logs at: ${log.transports.file.getFile().path}`
    );
    app.quit();
  }
});

app.on("window-all-closed", () => {
  stopFlightDeck();
  stopBackend();
  app.quit();
});

app.on("before-quit", () => {
  stopFlightDeck();
  stopBackend();
});

app.on("activate", () => {
  // macOS: re-create window when dock icon is clicked
  if (BrowserWindow.getAllWindows().length === 0 && fdReady) {
    createWindow();
    mainWindow.loadURL(getFdUrl());
  }
});

// ── Loading screen HTML ────────────────────────────────────────

const LOADING_HTML = `<!DOCTYPE html>
<html>
<head>
<style>
  body {
    margin: 0;
    height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    background: #1a1a2e;
    color: #e0e0e0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
  }
  h1 { font-size: 2rem; margin-bottom: 0.5rem; color: #00d4ff; }
  p  { font-size: 1rem; opacity: 0.7; }
  .spinner {
    width: 40px; height: 40px;
    border: 3px solid rgba(0,212,255,0.2);
    border-top-color: #00d4ff;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin-bottom: 1.5rem;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
  <div class="spinner"></div>
  <h1>Captain Claw</h1>
  <p>Starting backend server...</p>
</body>
</html>`;
