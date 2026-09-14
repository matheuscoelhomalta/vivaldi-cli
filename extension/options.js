async function showCode() {
  const stored = await chrome.storage.local.get("pairingCode");
  let code = stored.pairingCode;
  if (!code) {
    code = crypto.randomUUID().replaceAll("-", "");
    await chrome.storage.local.set({pairingCode: code});
  }
  document.getElementById("code").textContent = code;
}

showCode().catch(error => {
  document.getElementById("code").textContent = `Could not load pairing code: ${error.message}`;
});
