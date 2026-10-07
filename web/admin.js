"use strict";

const form = document.querySelector("#configuration");
const pages = document.querySelector("#pages");
const status = document.querySelector("#status");
const save = document.querySelector("#save");
const reload = document.querySelector("#reload");
const navigation = document.querySelector("#navigation");
let current;
let busy = false;
let dirty = false;
let csrfToken = "";
let enabled = new Set();
let initialEnabled = new Set();
const controls = new Map();
const pageInfo = new Map();
const generalFields = [
  {key: "PLEX_URL", label: "Plex server URL", kind: "url", description: "Address of your upstream Plex Media Server."},
  {key: "PLEX_TOKEN", label: "Plex token", description: "Used for library scans and finding downloaded tracks."},
  {key: "MUSIC_SECTION_ID", label: "Music library section", kind: "select", choices: [], description: "Must be a Plex Music library, not Movies or TV Shows. Save the Plex URL/token, then load music libraries to select the correct destination."},
  {key: "PROXY_PORT", label: "Listening port", kind: "number", minimum: 1, maximum: 65535, step: "1", description: "Requires a process restart. Plex clients connect to this port."},
  {key: "ADMIN_PORT", label: "Administration port", kind: "number", minimum: 1, maximum: 65535, step: "1", description: "Separate from Plex traffic. Default 32300. Requires restart; reconnect to the new port afterward."},
  {key: "ADMIN_ALLOWED_NETWORKS", label: "Allowed admin networks", description: "Comma-separated IPv4/IPv6 CIDRs or individual IPs. Applies immediately to login, pages and APIs. Must include your current peer address. Forwarded headers are not trusted."},
];
const downloadFields = [
  {key: "DOWNLOAD_DIR", label: "Music download folder", description: "Absolute path where music plugins save audio files. Preserves your existing download location."},
  {key: "PLEX_DOWNLOAD_DIR", label: "Music folder as seen by Plex", description: "Leave blank to use the music download folder. Set this when Plex uses a different path."},
  {key: "AUDIO_FORMAT", label: "Audio format", kind: "select", choices: ["mp3", "flac", "m4a", "opus", "wav"], description: "Common formats supported by both built-in providers. An existing custom format is preserved."},
  {key: "RETENTION_DAYS", label: "Keep downloads for", kind: "number", minimum: 1, step: "1", unit: "days", description: "Files older than this are deleted when automatic cleanup is enabled."},
  {key: "CLEANUP_INTERVAL_HOURS", label: "Cleanup frequency", kind: "number", minimum: 0, step: "any", unit: "hours", description: "How often to check for expired downloads. Must be greater than zero."},
];
const categoryLocationFields = [
  ["SERIES", "Series"], ["MOVIES", "Movies"], ["VIDEOS", "Other videos"],
].flatMap(([key, label]) => [
  {key: `${key}_DOWNLOAD_DIR`, label: `${label} download folder`, description: "Reserved for future plugins. Configuring this path does not enable downloading or cleanup for this category."},
  {key: `PLEX_${key}_DOWNLOAD_DIR`, label: `${label} folder as seen by Plex`, description: "Leave blank to use this category's download folder."},
]);
const advancedFields = [
  {key: "SEARCH_LIMIT", label: "Results per provider", kind: "number", minimum: 1, step: "1"},
  {key: "PROVIDER_TIMEOUT", label: "Search timeout", kind: "number", minimum: 0, step: "any", unit: "sec", description: "Time allowed for each provider search. Must be greater than zero."},
  {key: "DOWNLOAD_TIMEOUT", label: "Download timeout", kind: "number", minimum: 0, step: "any", unit: "sec", description: "Maximum time allowed for an audio download. Must be greater than zero."},
  {key: "SCAN_TIMEOUT", label: "Library scan timeout", kind: "number", minimum: 0, step: "any", unit: "sec", description: "How long to wait for Plex to index a downloaded track. Must be greater than zero."},
  {key: "SCAN_POLL_INTERVAL", label: "Library polling interval", kind: "number", minimum: 0, step: "any", unit: "sec", description: "Delay between checks for the indexed track. Must be greater than zero."},
];

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function secret(key) { return /TOKEN|SECRET|PASSWORD|KEY/.test(key); }
function isTrue(value) { return ["true", "1", "yes"].includes(String(value).toLowerCase()); }
function message(text, error = false) {
  status.textContent = text;
  status.classList.toggle("error", error);
}
function setDirty(value) {
  dirty = value;
  document.querySelector(".save-bar").hidden = location.hash === "#logs" && !value;
  document.querySelector("#dirty-badge").hidden = !value;
  document.querySelector("#save-hint").textContent = value ? "You have unsaved changes" : "Configuration is up to date";
  save.disabled = busy || !current || !value;
}
function updateDirty() {
  const providerChanged = enabled.size !== initialEnabled.size || [...enabled].some(name => !initialEnabled.has(name));
  const fieldChanged = [...controls.values()].some(control =>
    control.value() !== control.initial || (control.clear && control.clear.checked));
  const extras = [...pages.querySelectorAll("[data-extra-key]")].some(input => input.value.trim());
  setDirty(providerChanged || fieldChanged || Boolean(extras));
}
function setBusy(value) {
  busy = value;
  save.disabled = value || !current || !dirty;
  reload.disabled = value;
  for (const page of pages.children) page.inert = value;
}
async function api(path, method = "GET", body) {
  const response = await fetch(`${location.origin}/admin/api/${path}`, {
    method, credentials: "same-origin",
    headers: body ? {"Content-Type": "application/json", "X-CSRF-Token": csrfToken} : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (response.status === 401) {
    dirty = false;
    location.assign("/admin/login");
    throw new Error("Your session expired. Sign in again.");
  }
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
  return data;
}
function request(method, body) { return api("config", method, body); }
function link(url, label) {
  const anchor = element("a", "configure-link", label);
  anchor.href = url;
  if (url.startsWith("https://")) { anchor.target = "_blank"; anchor.rel = "noopener noreferrer"; }
  return anchor;
}
function tokenGuide(parent, title, description, links) {
  const guide = element("div", "token-guide");
  guide.append(element("h3", "", title), element("p", "", description));
  for (const [url, label] of links) guide.append(link(url, label));
  parent.append(guide);
  return guide;
}
function panel(title, description) {
  const container = element("section", "panel");
  const heading = element("div", "panel-heading");
  heading.append(element("h2", "", title));
  if (description) heading.append(element("p", "", description));
  const body = element("div", "panel-body");
  container.append(heading, body);
  return {container, body};
}
function toggle(id, label, checked, onChange) {
  const wrapper = element("div", "switch-control");
  const input = element("input", "toggle");
  input.type = "checkbox";
  input.id = id;
  input.setAttribute("role", "switch");
  input.setAttribute("aria-label", label);
  input.checked = checked;
  const caption = element("label", "switch-caption", checked ? "On" : "Off");
  caption.htmlFor = id;
  input.addEventListener("change", () => {
    caption.textContent = input.checked ? "On" : "Off";
    onChange(input.checked);
  });
  wrapper.append(input, caption);
  return {wrapper, input, caption};
}
function addField(parent, field) {
  if (controls.has(field.key)) return;
  const row = element("div", "field");
  const heading = element("div");
  const label = element("label", "field-label", field.label);
  label.htmlFor = field.key;
  heading.append(label, element("small", "field-key", field.key));
  const area = element("div", "control");
  const value = current.values[field.key] ?? field.default ?? "";
  let input;
  let read;
  if (field.kind === "switch") {
    const switchControl = toggle(field.key, field.label, isTrue(value), updateDirty);
    input = switchControl.input;
    read = () => input.checked ? "true" : "false";
    area.append(switchControl.wrapper);
  } else if (field.kind === "select") {
    input = element("select");
    const choices = [...(field.choices || [])];
    if (!choices.includes(value)) choices.push(value);
    for (const choice of choices) {
      const option = element("option", "", choice === value && !(field.choices || []).includes(choice) ? `${choice || "Not set"} (current)` : choice.toUpperCase());
      option.value = choice;
      input.append(option);
    }
    input.value = value;
    read = () => input.value;
    area.append(input);
  } else {
    input = element("input");
    input.type = secret(field.key) ? "password" : field.kind || "text";
    input.autocomplete = secret(field.key) ? "new-password" : "off";
    input.value = value;
    if (field.kind === "number") {
      if (field.minimum != null) input.min = String(field.minimum);
      if (field.maximum != null) input.max = String(field.maximum);
      input.step = field.step || "any";
    }
    if (secret(field.key)) input.placeholder = current.secrets_set.includes(field.key) ? "Saved - leave blank to keep" : "Not set";
    if (field.unit) {
      const unit = element("div", "input-with-unit");
      unit.append(input, element("span", "unit", field.unit));
      area.append(unit);
    } else area.append(input);
    read = () => input.value;
  }
  input.id = field.key;
  input.dataset.key = field.key;
  input.addEventListener("input", () => {
    if (input.checkValidity()) input.removeAttribute("aria-invalid");
    updateDirty();
  });
  input.addEventListener("change", updateDirty);
  if (field.description) {
    const hint = element("p", "help", field.description);
    hint.id = `${field.key}-help`;
    input.setAttribute("aria-describedby", hint.id);
    area.append(hint);
  }
  let clear;
  if (secret(field.key) && current.secrets_set.includes(field.key)) {
    const clearLabel = element("label", "clear");
    clear = element("input");
    clear.type = "checkbox";
    clear.dataset.clear = field.key;
    clearLabel.append(clear, document.createTextNode("Clear saved secret"));
    area.append(clearLabel);
    clear.addEventListener("change", () => { input.disabled = clear.checked; updateDirty(); });
  }
  controls.set(field.key, {input, value: read, initial: read(), clear});
  row.append(heading, area);
  parent.append(row);
}
function addPage(id, title, description, icon, provider = false) {
  const page = element("section", "settings-page");
  page.dataset.page = id;
  page.hidden = true;
  pageInfo.set(id, {title, description});
  pages.append(page);
  const link = element("a", provider ? "nav-link nav-provider" : "nav-link");
  link.href = `#${id}`;
  link.append(element("span", "nav-icon", icon), document.createTextNode(title));
  link.dataset.pageLink = id;
  navigation.append(link);
  return page;
}
function showPage(focus = false) {
  if (!current) return;
  const selected = location.hash.slice(1);
  const id = pageInfo.has(selected) ? selected : "general";
  document.querySelector(".save-bar").hidden = id === "logs" && !dirty;
  for (const page of pages.children) page.hidden = page.dataset.page !== id;
  for (const link of navigation.querySelectorAll("[data-page-link]")) {
    if (link.dataset.pageLink === id) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const info = pageInfo.get(id);
  document.querySelector("#page-title").textContent = info.title;
  document.querySelector("#page-description").textContent = info.description;
  document.title = `${info.title} | Plex Source Injection`;
  if (focus) document.querySelector("#main").focus({preventScroll: true});
}
function providerState(provider) {
  if (enabled.has(provider.name) !== initialEnabled.has(provider.name)) {
    return enabled.has(provider.name) ? ["Will enable on save", "warning"] : ["Will disable on save", "warning"];
  }
  if (!enabled.has(provider.name)) return ["Disabled", ""];
  if (!pages.querySelector(`[data-category-provider="${provider.name}"]:checked`)) return ["No categories selected", "warning"];
  return current.active_providers.includes(provider.name) ? ["Active", "active"] : ["Enabled, not configured", "warning"];
}
function syncProviders() {
  for (const input of pages.querySelectorAll("[data-provider-toggle]")) {
    input.checked = enabled.has(input.dataset.providerToggle);
    input.nextElementSibling.textContent = input.checked ? "On" : "Off";
  }
  for (const badge of pages.querySelectorAll("[data-provider-state]")) {
    const provider = current.providers.find(item => item.name === badge.dataset.providerState);
    const [text, className] = providerState(provider);
    badge.textContent = text;
    badge.className = `provider-state ${className}`;
  }
}
function providerToggle(provider, suffix) {
  const control = toggle(`enable-${provider.name}-${suffix}`, `Enable ${provider.display_name}`, enabled.has(provider.name), checked => {
    if (checked) enabled.add(provider.name);
    else enabled.delete(provider.name);
    syncProviders();
    updateDirty();
  });
  control.input.dataset.providerToggle = provider.name;
  return control.wrapper;
}
function providerIcon(provider) {
  return element("span", `provider-icon ${provider.name === "spotify" || provider.name === "youtube" ? provider.name : ""}`, provider.display_name.slice(0, 1));
}
function stateBadge(provider) {
  const badge = element("span", "provider-state");
  badge.dataset.providerState = provider.name;
  return badge;
}
function addExtraSettings(page, id, prefix = "") {
  const details = element("details", "panel extra-settings");
  details.append(element("summary", "", "Add a custom setting"));
  details.append(element("p", "", prefix
    ? `Use ${prefix}_ names to keep additional settings on this plugin's page. Declared plugin settings can use any name.`
    : "For settings not assigned to a plugin. Names use uppercase letters, numbers and underscores."));
  const row = element("div", "field");
  const keyLabel = element("label", "field-label", "Setting name");
  keyLabel.htmlFor = `${id}-extra-key`;
  const key = element("input");
  key.id = keyLabel.htmlFor;
  key.dataset.extraKey = id;
  key.placeholder = prefix ? `${prefix}_TOKEN` : "CUSTOM_SETTING";
  key.pattern = "[A-Z][A-Z0-9_]{0,79}";
  row.append(keyLabel, key);
  const valueRow = element("div", "field");
  const valueLabel = element("label", "field-label", "Value");
  valueLabel.htmlFor = `${id}-extra-value`;
  const value = element("input");
  value.id = valueLabel.htmlFor;
  value.dataset.extraValue = id;
  value.type = "password";
  value.autocomplete = "new-password";
  valueRow.append(valueLabel, value);
  details.append(row, valueRow);
  page.append(details);
}
function addCategories(page, provider) {
  const categories = panel("Media categories", "Only supported categories are available. With none selected, this plugin will not contribute search results or ingest music.");
  const row = element("div", "category-options");
  const selected = new Set(provider.selected_categories.split(",").filter(Boolean));
  for (const name of provider.supported_categories) {
    const label = element("label", "category-choice");
    const input = element("input");
    input.type = "checkbox";
    input.checked = selected.has(name);
    input.dataset.categoryProvider = provider.name;
    input.value = name;
    label.append(input, document.createTextNode(name.slice(0, 1).toUpperCase() + name.slice(1)));
    row.append(label);
  }
  categories.body.append(row);
  const key = `${provider.name.toUpperCase()}_CATEGORIES`;
  const read = () => [...row.querySelectorAll("input:checked")].map(input => input.value).join(",");
  controls.set(key, {input: row.querySelector("input") || row, value: read, initial: read()});
  row.addEventListener("change", () => { syncProviders(); updateDirty(); });
  page.append(categories.container);
}
function addDependencyPage(tool) {
  const page = addPage(`tool/${tool.name}`, tool.name, "Choose an existing executable or install a checksum-verified upstream release.", "T");
  const settings = panel("Dependency source", "Managed installs are explicit. Switching source requires saving. FFmpeg settings are shared by both built-in plugins.");
  addField(settings.body, {key: tool.mode_key, label: "Installation source", kind: "select", choices: tool.modes, default: tool.modes[0], description: "External uses your binary. Managed downloads a standalone binary. Managed-python installs spotdl from PyPI into a private virtual environment (recommended on EL9). Bundled uses the installed Python package."});
  addField(settings.body, {key: tool.binary_key, label: "External executable", default: tool.name, description: "Executable name on PATH or absolute file path. Used only in external mode."});
  page.append(settings.container);
  const sourceDescription = tool.name === "spotdl" ? `${tool.source} (standalone), PyPI (managed-python)` : tool.source;
  const downloads = panel("Managed versions", `Source: ${sourceDescription}. Executables run with the proxy's permissions. Updates never run automatically.`);
  const body = element("div", "tool-actions");
  const info = element("p", "help", "Checking installed version...");
  const version = element("select");
  version.setAttribute("aria-label", `${tool.name} release version`);
  const latest = element("option", "", "Latest stable release");
  latest.value = "latest";
  version.append(latest);
  const check = element("button", "button-secondary", "Check for updates");
  check.type = "button";
  const install = element("button", "button-primary", "Install / update");
  install.type = "button";
  const result = element("p", "help");
  result.setAttribute("role", "status");
  async function refresh() {
    try {
      const data = await api(`dependencies/${tool.name}`);
      info.textContent = data.installed ? `Installed: ${data.installed.version} (${data.installed.sha256.slice(0, 12)}). Version check: ${data.installed.reported_version || "not recorded"}.` : "No managed version installed.";
    } catch (error) { info.textContent = error.message; info.classList.add("error"); }
  }
  check.addEventListener("click", async () => {
    check.disabled = true;
    result.textContent = "Checking upstream releases...";
    try {
      const data = await api(`dependencies/${tool.name}/check`, "POST", {});
      version.replaceChildren(latest);
      for (const release of data.releases) {
        const option = element("option", "", `${release.version} (${release.published.slice(0, 10)})`);
        option.value = release.version;
        version.append(option);
      }
      result.textContent = "Choose a version to install or update. Older verified versions can also be selected.";
    } catch (error) { result.textContent = error.message; }
    finally { check.disabled = false; }
  });
  install.addEventListener("click", async () => {
    if (dirty) { message("Save or reload your changes before installing a dependency.", true); return; }
    const source = current.values[tool.mode_key] === "managed-python" ? "PyPI with a private Python virtual environment" : tool.source;
    if (!window.confirm(`Download and install ${tool.name} (${version.value}) from ${source}? The proxy will run this executable when managed mode is selected.`)) return;
    setBusy(true);
    result.textContent = "Downloading and verifying. This may take several minutes...";
    try {
      const data = await api(`dependencies/${tool.name}/install`, "POST", {version: version.value});
      result.textContent = `Installed ${data.installed.version}. ${["managed", "managed-python"].includes(current.values[tool.mode_key]) ? "New requests now use this version." : "Select Managed and save to use it."} Previous version files remain available to active downloads.`;
      await refresh();
    } catch (error) { result.textContent = error.message; }
    finally { setBusy(false); }
  });
  body.append(info, version, check, install, result);
  downloads.body.append(body);
  page.append(downloads.container);
  if (tool.name === "ffmpeg") tokenGuide(page, "FFmpeg builds", "Managed FFmpeg currently supports Linux x86-64 and ARM64 using yt-dlp's GPL builds, not binaries published by ffmpeg.org. Other systems must provide ffmpeg and ffprobe themselves.", [["https://ffmpeg.org/download.html", "FFmpeg download guidance"], ["https://github.com/yt-dlp/FFmpeg-Builds", "Build source & licenses"]]);
  refresh();
}
function addPlexLogin(parent) {
  const guide = tokenGuide(parent, "Get your Plex token", "Recommended: sign in on Plex's website and authorize this proxy. Your password and MFA stay with Plex. The resulting token is stored in SQLite. Use the server owner's account for library scans. Or copy X-Plex-Token manually from a Plex request.", [["https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/", "Manual token instructions"]]);
  const start = element("button", "button-secondary", "Sign in with Plex");
  start.type = "button";
  const authLink = element("a", "configure-link", "Open Plex sign-in");
  authLink.target = "_blank";
  authLink.rel = "noopener noreferrer";
  authLink.hidden = true;
  const poll = element("button", "button-secondary", "Check authorization");
  poll.type = "button";
  poll.hidden = true;
  let loginId;
  start.addEventListener("click", async () => {
    if (dirty) { message("Save or reload your changes before signing in with Plex.", true); return; }
    setBusy(true);
    try {
      const data = await api("plex/login", "POST", {});
      loginId = data.login_id;
      authLink.href = data.url;
      authLink.hidden = false;
      poll.hidden = false;
      message("Open Plex sign-in, authorize the proxy, then click Check authorization. Login expires after a few minutes.");
    } catch (error) { message(error.message, true); }
    finally { setBusy(false); }
  });
  poll.addEventListener("click", async () => {
    if (dirty) { message("Configuration has unsaved changes. Save/reload, then restart Plex sign-in.", true); return; }
    setBusy(true);
    try {
      const data = await api("plex/login/poll", "POST", {login_id: loginId});
      if (data.complete) {
        render(data.config);
        message("Plex token saved and applied. Configure your Plex URL and music library section if needed.");
      } else message("Authorization is still pending. Complete Plex sign-in, then check again.");
    } catch (error) { message(error.message, true); }
    finally { setBusy(false); }
  });
  guide.append(start, authLink, poll);
}
function addMusicLibrarySelector(parent) {
  const button = element("button", "button-secondary", "Load Plex music libraries");
  button.type = "button";
  const result = element("p", "help");
  result.setAttribute("role", "status");
  button.addEventListener("click", async () => {
    if (["PLEX_URL", "PLEX_TOKEN"].some(key => {
      const control = controls.get(key);
      return control.value() !== control.initial || control.clear?.checked;
    })) {
      result.textContent = "Save your Plex URL/token changes before loading libraries.";
      return;
    }
    button.disabled = true;
    result.textContent = "Loading music libraries from the saved Plex server...";
    const control = controls.get("MUSIC_SECTION_ID");
    try {
      const data = await api("plex/music-libraries");
      if (!button.isConnected) return;
      const selected = control.input.value;
      control.input.replaceChildren();
      const matches = data.libraries.some(library => library.id === selected);
      if (!matches) {
        const currentOption = element("option", "", `ID ${selected} - unavailable or not a Music library`);
        currentOption.value = selected;
        currentOption.disabled = true;
        control.input.append(currentOption);
      }
      for (const library of data.libraries) {
        const option = element("option", "", `${library.title} (ID ${library.id})`);
        option.value = library.id;
        control.input.append(option);
      }
      control.input.value = selected;
      result.textContent = !data.libraries.length
        ? "No Music libraries found. Create a Music library in Plex, add your music download folder, then load again."
        : matches ? "Music library verified. Select a different library if needed, then save."
        : "The current ID is not an available Music library. Select a Music library from the list and save.";
    } catch (error) { result.textContent = error.message; }
    finally { button.disabled = false; }
  });
  parent.append(button, result);
}
function addLogsPage() {
  const page = addPage("logs", "Logs", "Search results, downloads and application errors. Recent activity is retained across restarts.", "L");
  const logPanel = panel("Activity", "Latest 2,000 application entries are retained in SQLite. Credentials are redacted; titles, paths and search terms remain visible to administrators.");
  const toolbar = element("div", "log-toolbar");
  const category = element("select");
  category.setAttribute("aria-label", "Log category");
  for (const [value, label] of [["", "All activity"], ["search", "Searches"], ["download", "Downloads"], ["system", "System"]]) {
    const option = element("option", "", label); option.value = value; category.append(option);
  }
  const level = element("select");
  level.setAttribute("aria-label", "Log level");
  for (const value of ["", "INFO", "WARNING", "ERROR", "CRITICAL"]) {
    const option = element("option", "", value || "All levels"); option.value = value; level.append(option);
  }
  const refresh = element("button", "button-secondary", "Refresh logs");
  refresh.type = "button";
  const older = element("button", "button-secondary", "Load older");
  older.type = "button";
  older.hidden = true;
  const summary = element("p", "help");
  summary.setAttribute("role", "status");
  const entries = element("div", "log-entries");
  let before;
  let requestId = 0;
  async function fetchLogs(append = false) {
    const id = ++requestId;
    refresh.disabled = true;
    older.disabled = true;
    summary.textContent = "Loading activity...";
    const params = new URLSearchParams({limit: "100"});
    if (category.value) params.set("category", category.value);
    if (level.value) params.set("level", level.value);
    if (append && before) params.set("before", before);
    try {
      const data = await api(`logs?${params}`);
      if (id !== requestId) return;
      if (!append) entries.replaceChildren();
      for (const entry of data.entries) {
        const row = element("article", `log-entry log-${entry.level.toLowerCase()}`);
        const heading = element("div", "log-meta");
        heading.append(element("time", "", new Date(entry.timestamp).toLocaleString()),
          element("strong", "", entry.level), element("span", "", `${entry.category} / ${entry.source}`));
        row.append(heading, element("pre", "log-message", entry.message));
        entries.append(row);
      }
      before = data.entries.at(-1)?.id;
      older.hidden = data.entries.length < 100;
      summary.textContent = entries.children.length ? `Showing ${entries.children.length} entries, newest first.` : "No matching activity yet.";
    } catch (error) {
      if (id === requestId) summary.textContent = error.message;
    } finally {
      if (id === requestId) { refresh.disabled = false; older.disabled = false; }
    }
  }
  refresh.addEventListener("click", () => fetchLogs());
  category.addEventListener("change", () => fetchLogs());
  level.addEventListener("change", () => fetchLogs());
  older.addEventListener("click", () => fetchLogs(true));
  toolbar.append(category, level, refresh);
  logPanel.body.append(toolbar, summary, entries, older);
  page.append(logPanel.container);
  fetchLogs();
}
function render(data) {
  current = data;
  enabled = new Set(data.values.ENABLED_PROVIDERS.split(",").map(name => name.trim()).filter(Boolean));
  initialEnabled = new Set(enabled);
  controls.clear();
  pageInfo.clear();
  pages.replaceChildren();
  navigation.replaceChildren();
  const general = addPage("general", "General", "Connect your Plex server and manage the proxy.", "G");
  const connection = panel("Plex connection", "Your server, library and authentication.");
  generalFields.forEach(field => addField(connection.body, field));
  addMusicLibrarySelector(connection.body);
  general.append(connection.container);
  addPlexLogin(general);
  const downloads = addPage("downloads", "Downloads & cleanup", "Separate download locations for each media category.", "D");
  const audio = panel("Audio downloads", "Paths must be absolute. Downloads remain available in your Plex library.");
  downloadFields.slice(0, 3).forEach(field => addField(audio.body, field));
  downloads.append(audio.container);
  const locations = panel("Series, movies & videos", "Prepared for future plugins. These folders are not downloaded to or automatically cleaned up by the current music-only pipeline.");
  categoryLocationFields.forEach(field => addField(locations.body, field));
  downloads.append(locations.container);
  const cleanup = panel("Automatic cleanup", "Remove expired downloads and rescan the library.");
  const cleanupRow = element("div", "field");
  const cleanupLabel = element("label", "field-label", "Enable automatic cleanup");
  cleanupLabel.htmlFor = "cleanup-enabled";
  const cleanupControl = toggle("cleanup-enabled", "Enable automatic cleanup", Number(data.values.RETENTION_DAYS) > 0, checked => {
    controls.get("RETENTION_DAYS").input.disabled = !checked;
    controls.get("CLEANUP_INTERVAL_HOURS").input.disabled = !checked;
    updateDirty();
  });
  cleanupRow.append(cleanupLabel, cleanupControl.wrapper);
  cleanup.body.append(cleanupRow);
  downloadFields.slice(3).forEach(field => addField(cleanup.body, field));
  const retention = controls.get("RETENTION_DAYS");
  if (!cleanupControl.input.checked) retention.input.value = "30";
  retention.value = () => cleanupControl.input.checked ? retention.input.value : "0";
  retention.initial = data.values.RETENTION_DAYS;
  retention.input.disabled = !cleanupControl.input.checked;
  controls.get("CLEANUP_INTERVAL_HOURS").input.disabled = !cleanupControl.input.checked;
  downloads.append(cleanup.container);
  const providers = addPage("providers", "Plugins", "Select your music sources, then configure each plugin.", "P");
  const grid = element("div", "provider-grid");
  for (const provider of data.providers) {
    const card = element("section", "provider-card");
    const top = element("div", "provider-card-top");
    const title = element("div");
    title.append(element("h2", "", provider.display_name), stateBadge(provider));
    top.append(providerIcon(provider), title);
    const bottom = element("div", "provider-card-bottom");
    const link = element("a", "configure-link", "Configure plugin");
    link.href = `#plugin/${provider.name}`;
    bottom.append(providerToggle(provider, "card"), link);
    card.append(top, element("p", "", provider.description || "External music source."), bottom);
    grid.append(card);
  }
  if (!data.providers.length) grid.append(element("p", "empty-state", "No plugins discovered. Install a provider module to get started."));
  providers.append(grid);
  const missing = [...enabled].filter(name => !data.available_providers.includes(name));
  if (missing.length) {
    const warning = element("div", "notice");
    warning.append(element("p", "", `Unavailable configured plugins: ${missing.join(", ")}. Install them again or remove them before saving.`));
    const remove = element("button", "button-secondary", "Remove unavailable plugins");
    remove.type = "button";
    remove.addEventListener("click", () => { missing.forEach(name => enabled.delete(name)); warning.hidden = true; updateDirty(); });
    warning.append(remove);
    providers.append(warning);
  }
  const advanced = addPage("advanced", "Advanced", "Fine-tune searches, downloads and Plex library scans.", "A");
  const tuning = panel("Search & timeouts", "Timing values must be greater than zero.");
  advancedFields.forEach(field => addField(tuning.body, field));
  advanced.append(tuning.container);
  addLogsPage();
  navigation.append(element("p", "nav-heading nav-group", "PLUGINS"));
  const declared = new Set(data.providers.flatMap(provider => provider.fields.map(field => field.key)));
  const generalKeys = new Set([...generalFields, ...downloadFields, ...categoryLocationFields, ...advancedFields].map(field => field.key));
  generalKeys.add("ENABLED_PROVIDERS");
  for (const tool of data.dependency_tools) { generalKeys.add(tool.mode_key); generalKeys.add(tool.binary_key); }
  for (const provider of data.providers) generalKeys.add(`${provider.name.toUpperCase()}_CATEGORIES`);
  const customKeys = Object.keys(data.values).filter(key => !generalKeys.has(key) && !declared.has(key));
  const assigned = new Set();
  for (const provider of data.providers) {
    const page = addPage(`plugin/${provider.name}`, provider.display_name, provider.description || "Configure this external music source.", provider.display_name.slice(0, 1), true);
    const summary = element("section", "panel provider-summary");
    const title = element("div");
    title.append(element("h2", "", provider.display_name), stateBadge(provider), element("p", "", "Enable this plugin to include its results in Plex searches."));
    summary.append(providerIcon(provider), title, providerToggle(provider, "page"));
    page.append(summary);
    addCategories(page, provider);
    const settingsPanel = panel("Plugin configuration", "Credentials are never displayed. Blank secret fields keep the saved value.");
    provider.fields.filter(field => !data.dependency_tools.some(tool => tool.binary_key === field.key)).forEach(field => addField(settingsPanel.body, field));
    const prefix = provider.name.toUpperCase();
    for (const key of customKeys.filter(key => key.startsWith(`${prefix}_`))) {
      if (!assigned.has(key)) {
        addField(settingsPanel.body, {key, label: key, kind: "text"});
        assigned.add(key);
      }
    }
    if (!settingsPanel.body.children.length) settingsPanel.body.append(element("p", "empty-state", "This plugin has no declared settings. Add custom options below if needed."));
    page.append(settingsPanel.container);
    if (provider.name === "spotify") tokenGuide(page, "Get Spotify app credentials", "Spotify search uses the Client Credentials flow, not your account password. Sign in to the developer dashboard, create an application, then copy its Client ID and Client Secret here. Access tokens are obtained automatically by Spotipy. Account login cannot create developer app credentials. Developer-account access restrictions and quotas still apply.", [["https://developer.spotify.com/dashboard", "Spotify developer dashboard"], ["https://developer.spotify.com/documentation/web-api/tutorials/client-credentials-flow", "Authentication guide"]]);
    if (provider.name === "youtube") tokenGuide(page, "Get a YouTube API key (optional)", "No token is needed for yt-dlp searches. To use the Data API, create a Google Cloud project, enable YouTube Data API v3, then create an API key under APIs & Services > Credentials. Restrict the key to the YouTube API and your server's IP where practical. A Google username/password cannot create this key automatically.", [["https://console.cloud.google.com/apis/credentials", "Google Cloud credentials"], ["https://developers.google.com/youtube/v3/getting-started", "YouTube API setup"]]);
    if (provider.dependencies.length) {
      const tools = panel("External dependencies", "Choose your own executable or a managed version. Shared dependency choices apply to every plugin using that tool.");
      const links = element("div", "tool-links");
      for (const dependency of provider.dependencies) links.append(link(`#tool/${dependency}`, `Configure ${dependency}`));
      tools.body.append(links);
      page.append(tools.container);
    }
    addExtraSettings(page, provider.name, prefix);
  }
  navigation.append(element("p", "nav-heading nav-group", "DEPENDENCIES"));
  data.dependency_tools.forEach(addDependencyPage);
  navigation.append(element("p", "nav-heading nav-group", "MORE"));
  const custom = addPage("custom", "Custom settings", "Manage additional settings not assigned to a plugin.", "+");
  const other = panel("Additional configuration", "Plugin-prefixed settings appear on their plugin's own page.");
  customKeys.filter(key => !assigned.has(key)).forEach(key => addField(other.body, {key, label: key}));
  if (!other.body.children.length) other.body.append(element("p", "empty-state", "No unassigned custom settings."));
  custom.append(other.container);
  addExtraSettings(custom, "custom");
  syncProviders();
  showPage();
  document.querySelector("#connection").textContent = `Plex :${data.listening_port} / Admin :${data.admin_listening_port}`;
  const notice = document.querySelector("#restart-notice");
  notice.hidden = !data.restart_required;
  notice.textContent = `Restart required: Plex :${data.listening_port}, admin :${data.admin_listening_port}. After restarting, Plex uses :${data.values.PROXY_PORT} and administration uses :${data.values.ADMIN_PORT}. Reconnect to the new admin port if changed.`;
  setDirty(false);
}
function collectUpdate() {
  const values = {ENABLED_PROVIDERS: [...enabled].join(",")};
  const clear = [];
  for (const [key, control] of controls) {
    const invalid = !control.input.disabled && control.input.checkValidity && !control.input.checkValidity();
    control.input.setAttribute("aria-invalid", String(invalid));
    if (invalid) {
      location.hash = control.input.closest("[data-page]").dataset.page;
      showPage();
      control.input.reportValidity();
      control.input.focus();
      throw new Error(`Check ${key}: ${control.input.validationMessage}`);
    }
    if (control.clear && control.clear.checked) clear.push(key);
    else if (control.value() !== control.initial) values[key] = control.value();
  }
  for (const keyInput of pages.querySelectorAll("[data-extra-key]")) {
    const key = keyInput.value.trim();
    if (!key) continue;
    if (!/^[A-Z][A-Z0-9_]{0,79}$/.test(key)) {
      location.hash = keyInput.closest("[data-page]").dataset.page;
      showPage();
      keyInput.focus();
      throw new Error("Custom setting names must use uppercase letters, numbers and underscores.");
    }
    if (key in current.values || controls.has(key) || key in values) throw new Error(`${key} already exists. Edit its existing control instead.`);
    values[key] = pages.querySelector(`[data-extra-value="${keyInput.dataset.extraKey}"]`).value;
  }
  return {values, clear, revision: current.revision};
}
async function load() {
  setBusy(true);
  try {
    csrfToken = (await api("session")).csrf;
    render(await request("GET"));
    message("Configuration loaded.");
  } catch (error) { message(error.message, true); }
  finally { setBusy(false); }
}
form.addEventListener("input", updateDirty);
form.addEventListener("change", updateDirty);
form.addEventListener("submit", async event => {
  event.preventDefault();
  if (busy || !current) return;
  let update;
  try { update = collectUpdate(); }
  catch (error) { message(error.message, true); return; }
  setBusy(true);
  try {
    render(await request("PUT", update));
    message("Saved to SQLite and applied to new requests.");
  } catch (error) { message(error.message, true); }
  finally { setBusy(false); }
});
reload.addEventListener("click", () => {
  if (!dirty || window.confirm("Discard unsaved changes and reload the saved configuration?")) load();
});
window.addEventListener("hashchange", () => showPage(true));
document.querySelector(".skip-link").addEventListener("click", event => {
  event.preventDefault();
  document.querySelector("#main").focus();
});
window.addEventListener("beforeunload", event => {
  if (dirty) { event.preventDefault(); event.returnValue = ""; }
});
document.querySelector("#logout").addEventListener("click", async () => {
  if (dirty && !window.confirm("Discard unsaved changes and sign out?")) return;
  try {
    await api("logout", "POST", {});
    dirty = false;
    location.assign("/admin/login");
  } catch (error) { message(error.message, true); }
});
load();
