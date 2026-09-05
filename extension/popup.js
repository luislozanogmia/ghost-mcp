const dot = document.getElementById("dot");
const statusLabel = document.getElementById("statusLabel");
const statusDetail = document.getElementById("statusDetail");
const portInput = document.getElementById("port");
const connectBtn = document.getElementById("connectBtn");
const disconnectBtn = document.getElementById("disconnectBtn");
const versionEl = document.getElementById("version");

function updateUI(status) {
  const on = status.connected;
  dot.className = `dot ${on ? "on" : "off"}`;
  statusLabel.textContent = on ? "Connected" : "Disconnected";
  statusDetail.textContent = on ? `Daemon on port ${status.port}` : "Not connected to daemon";
  portInput.value = status.port;
  versionEl.textContent = `v${status.version}`;
}

function refresh() {
  chrome.runtime.sendMessage({ type: "get-status" }, (status) => {
    if (status) updateUI(status);
  });
}

connectBtn.addEventListener("click", () => {
  const port = parseInt(portInput.value) || 9377;
  chrome.runtime.sendMessage({ type: "connect", port }, () => {
    setTimeout(refresh, 500);
  });
});

disconnectBtn.addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "disconnect" }, () => {
    setTimeout(refresh, 200);
  });
});

refresh();
