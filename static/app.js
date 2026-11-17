const $ = id => document.getElementById(id);
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const bytes = value => {
  if (!value) return "0 B";
  const unit = Math.min(3, Math.floor(Math.log(value) / Math.log(1024)));
  return `${(value / 1024 ** unit).toFixed(unit ? 1 : 0)} ${["B", "KiB", "MiB", "GiB"][unit]}`;
};
function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $("toast").classList.remove("show"), 4000);
}
async function api(path, body, method = "POST", signal) {
  const options = body === undefined ? {method: "GET"} : {method, headers: {"Content-Type":"application/json"}, body: JSON.stringify(body)};
  if (signal) options.signal = signal;
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "The request failed. Check your settings.");
  return data;
}
function action(id, handler) {
  $(id).onclick = async () => {
    const button = $(id);
    button.disabled = true;
    try { await handler(); }
    catch (error) { toast(error.message); }
    finally {
      button.disabled = false;
      if (["loadMoreEpisodes", "loadAllEpisodes", "queueSelected"].includes(id)) renderEpisodes();
    }
  };
}

const settingFields = {
  output_dir: "outputDir", staging_dir: "stagingDir", min_free_gb: "minFree", max_retries: "maxRetries",
  retry_delay_seconds: "retryDelay", bandwidth_limit: "bandwidth", ntfy_url: "ntfyUrl",
  pool_status_file: "poolFile", pool_max_percent: "poolThreshold"
};
async function loadConfig() {
  const config = await api("/api/config");
  for (const [key, id] of Object.entries(settingFields)) $(id).value = config[key] ?? "";
  $("workerCount").value = config.max_workers;
  $("ntfyToken").placeholder = config.ntfy_token_set ? "Saved token — leave blank to keep" : "Optional access token";
  $("pills").innerHTML = ["yt_dlp", "ffmpeg"].map(key => `<span class="pill ${config[key + "_ok"] ? "good" : "bad"}">${key.replace("_", "-")} ${config[key + "_ok"] ? "ready" : "missing"}</span>`).join(" ");
  const smb = config.smb || {};
  $("smbStatus").textContent = config.smb_connected ? `Connected — //${smb.host}/${smb.share}` : smb.host ? "Saved share — disconnected" : "Local storage";
  $("smbStatus").className = `pill ${config.smb_connected ? "good" : ""}`;
  for (const [key, id] of Object.entries({host: "smbHost", share: "smbShare", username: "smbUser", subdir: "smbSubdir"})) $(id).value = smb[key] || "";
}
action("saveSettings", async () => {
  const settings = {};
  for (const [key, id] of Object.entries(settingFields)) settings[key] = $(id).type === "number" ? Number($(id).value) : $(id).value.trim();
  if ($("ntfyToken").value) settings.ntfy_token = $("ntfyToken").value;
  await api("/api/config", settings);
  $("ntfyToken").value = "";
  await loadConfig();
  toast("Settings saved. Existing jobs keep their destination and staging location.");
});
action("clearNtfyToken", async () => { await api("/api/config", {ntfy_token: ""}); await loadConfig(); toast("Saved token cleared."); });
action("testNtfy", async () => { await api("/api/notifications/test", {}); toast("Test notification sent."); });
action("checkStorage", async () => {
  const result = await api("/api/storage");
  $("storageStatus").textContent = result.error || result.locations.map(location => `${location.path} — ${bytes(location.free_bytes)} free, writable`).join("\n");
});
action("saveWorkers", async () => {
  await api("/api/workers", {max_workers: Number($("workerCount").value)});
  toast("Worker count saved.");
});
action("smbConnect", async () => {
  await api("/api/smb/connect", {host: $("smbHost").value, share: $("smbShare").value, username: $("smbUser").value, password: $("smbPass").value, subdir: $("smbSubdir").value});
  $("smbPass").value = "";
  await loadConfig(); toast("SMB destination saved.");
});
action("smbDisconnect", async () => { await api("/api/smb/disconnect", {}); await loadConfig(); toast("New jobs will use local storage."); });
action("start", async () => {
  const urls = $("urls").value.split("\n").map(url => url.trim()).filter(Boolean);
  if (!urls.length) throw new Error("Add at least one URL.");
  const result = await api("/api/download", {urls});
  $("urls").value = "";
  queuedToast(result);
  await refresh();
});
function queuedToast(result) {
  toast(`Queued ${result.created.length.toLocaleString()} items; ${result.skipped.toLocaleString()} already queued or awaiting retry.`);
}

let currentPref = "roosterteeth";
let currentEpisodes = [];
let episodesByKey = new Map();
let catalog = {showId: "", title: "", query: "", cursor: null};
let catalogGeneration = 0;
let episodePage = 0;
let catalogLoading = false;
const episodePageSize = 100;
function defaultPlatform(episode) {
  if (currentPref === "youtube" && episode.youtube_url) return "youtube";
  return episode.rt_url ? "roosterteeth" : episode.youtube_url ? "youtube" : null;
}
function setPref(preference) {
  currentPref = preference;
  $("prefRt").classList.toggle("active", preference === "roosterteeth");
  $("prefYt").classList.toggle("active", preference === "youtube");
  currentEpisodes.forEach(episode => episode.selected = defaultPlatform(episode));
  renderEpisodes();
}
$("prefRt").onclick = () => setPref("roosterteeth");
$("prefYt").onclick = () => setPref("youtube");

let showTimer, episodeTimer;
$("showSearch").addEventListener("input", () => { clearTimeout(showTimer); showTimer = setTimeout(searchShows, 300); });
async function searchShows() {
  const query = $("showSearch").value.trim();
  const results = $("showResults");
  results.style.display = "";
  results.textContent = "Searching…";
  try {
    const {shows} = await api(`/api/browse/shows?q=${encodeURIComponent(query)}`);
    if ($("showSearch").value.trim() !== query) return;
    results.innerHTML = shows.map(show => `<div class="browse-item" data-show-id="${escapeHtml(show.id)}" data-show-title="${escapeHtml(show.title)}">
      ${show.thumbnail ? `<img src="${escapeHtml(show.thumbnail)}" alt="" loading="lazy">` : ""}<div>${escapeHtml(show.title)}<div class="browse-meta">${show.episode_count.toLocaleString()} catalog episodes</div></div></div>`).join("") || "No shows found.";
  } catch (error) { results.textContent = error.message; }
}
$("showResults").addEventListener("click", event => {
  const show = event.target.closest("[data-show-id]");
  if (show) loadEpisodes(show.dataset.showId, show.dataset.showTitle).catch(error => toast(error.message));
});
$("episodeSearch").addEventListener("input", () => {
  clearTimeout(episodeTimer);
  episodeTimer = setTimeout(() => {
    const query = $("episodeSearch").value.trim();
    if (query) beginCatalog({query, title: `Titles starting with “${query}”`}).catch(error => toast(error.message));
  }, 350);
});
async function loadEpisodes(showId, title) { await beginCatalog({showId, title}); }
async function beginCatalog({showId = "", title, query = ""}) {
  catalogGeneration++;
  catalogLoading = false;
  catalog = {showId, title, query, cursor: null};
  currentEpisodes = [];
  episodesByKey = new Map();
  episodePage = 0;
  for (const id of ["episodeFilter", "seasonFilter", "yearFilter", "sourceFilter", "downloadFilter"]) $(id).value = "";
  $("episodePanel").style.display = "";
  $("episodeShowTitle").textContent = title;
  await loadCatalogPage();
}
async function loadCatalogPage(loadAll = false) {
  if (catalogLoading) return;
  const generation = catalogGeneration;
  catalogLoading = true;
  $("episodeCount").textContent = "Loading…";
  try {
    do {
      const params = new URLSearchParams({limit: catalog.showId ? "500" : "100"});
      if (catalog.cursor) params.set("cursor", catalog.cursor);
      if (catalog.query) params.set("q", catalog.query);
      const path = catalog.showId ? `/api/browse/shows/${encodeURIComponent(catalog.showId)}/episodes` : "/api/browse/episodes";
      const result = await api(path + "?" + params);
      if (generation !== catalogGeneration) return;
      for (const episode of result.episodes) {
        const existing = episode.keys.map(key => episodesByKey.get(key)).find(Boolean);
        if (existing) {
          existing.rt_url ||= episode.rt_url;
          existing.youtube_url ||= episode.youtube_url;
          existing.keys = [...new Set([...existing.keys, ...episode.keys])];
          existing.keys.forEach(key => episodesByKey.set(key, existing));
        } else {
          episode.selected = defaultPlatform(episode);
          episode.checked = false;
          currentEpisodes.push(episode);
          episode.keys.forEach(key => episodesByKey.set(key, episode));
        }
      }
      if (!loadAll || !result.next_cursor) {
        currentEpisodes.sort((a, b) => catalog.showId ? a.date - b.date : a.title.localeCompare(b.title));
      }
      catalog.cursor = result.next_cursor;
      updateFilterOptions();
      renderEpisodes();
    } while (loadAll && catalog.cursor);
  } finally {
    if (generation === catalogGeneration) { catalogLoading = false; renderEpisodes(); }
  }
}
function updateFilterOptions() {
  for (const [id, values, label] of [
    ["seasonFilter", currentEpisodes.map(e => e.season).filter(v => v != null), "All seasons"],
    ["yearFilter", currentEpisodes.map(e => (e.release_date || "").slice(0, 4)).filter(Boolean), "All years"]
  ]) {
    const selected = $(id).value;
    $(id).innerHTML = `<option value="">${label}</option>` + [...new Set(values)].sort((a, b) => Number(a) - Number(b)).map(value => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
    $(id).value = selected;
  }
}
function filteredEpisodes() {
  return currentEpisodes.filter(episode => episode.title.toLowerCase().includes($("episodeFilter").value.toLowerCase())
    && (!$("seasonFilter").value || String(episode.season) === $("seasonFilter").value)
    && (!$("yearFilter").value || (episode.release_date || "").startsWith($("yearFilter").value))
    && (!$("sourceFilter").value || ($("sourceFilter").value === "youtube" ? episode.youtube_url : episode.rt_url))
    && (!$("downloadFilter").value || episode.download_status === $("downloadFilter").value));
}
function renderEpisodes() {
  if (!$("episodeList")) return;
  const filtered = filteredEpisodes();
  const pages = Math.max(1, Math.ceil(filtered.length / episodePageSize));
  episodePage = Math.min(episodePage, pages - 1);
  const page = filtered.slice(episodePage * episodePageSize, (episodePage + 1) * episodePageSize);
  const indexes = new Map(currentEpisodes.map((episode, index) => [episode, index]));
  $("episodeList").innerHTML = page.map(episode => {
    const index = indexes.get(episode);
    const season = episode.season != null && episode.episode_number != null ? `S${episode.season}E${episode.episode_number} · ` : "";
    const badges = [["roosterteeth", episode.rt_url, "RT"], ["youtube", episode.youtube_url, "YT"]].map(([platform, url, text]) => `<button type="button" class="ep-badge ${url ? "avail" : ""} ${episode.selected === platform ? "selected" : ""}" data-idx="${index}" data-platform="${platform}" ${url ? "" : "disabled"}>${text}</button>`).join("");
    return `<div class="ep-row"><input type="checkbox" class="ep-check" data-idx="${index}" aria-label="Select ${escapeHtml(episode.title)}" ${episode.checked ? "checked" : ""} ${episode.selected ? "" : "disabled"}><span style="min-width:0;flex:1">${escapeHtml(episode.title)}<small>${escapeHtml(season + (episode.release_date || ""))} · ${escapeHtml(episode.download_status)}${catalog.query && episode.show ? " · " + escapeHtml(episode.show) : ""}</small></span><span class="ep-badges">${badges}</span></div>`;
  }).join("") || '<div class="empty">No matching episodes in the loaded catalog.</div>';
  const selected = currentEpisodes.filter(episode => episode.checked).length;
  $("episodeCount").textContent = `${currentEpisodes.length.toLocaleString()} loaded · ${filtered.length.toLocaleString()} matching · ${selected.toLocaleString()} selected${catalogLoading ? " · loading…" : ""}`;
  $("episodePage").textContent = `${episodePage + 1} / ${pages}`;
  $("previousEpisodes").disabled = episodePage === 0;
  $("nextEpisodes").disabled = episodePage + 1 >= pages;
  $("loadMoreEpisodes").disabled = catalogLoading || !catalog.cursor;
  $("loadAllEpisodes").disabled = catalogLoading || !catalog.cursor;
  $("followShow").disabled = !catalog.showId;
  $("queueSelected").disabled = selected === 0;
  const available = filtered.filter(episode => episode.selected);
  $("selectAllEpisodes").checked = available.length > 0 && available.every(episode => episode.checked);
  $("selectAllEpisodes").indeterminate = available.some(episode => episode.checked) && !$("selectAllEpisodes").checked;
}
for (const id of ["episodeFilter", "seasonFilter", "yearFilter", "sourceFilter", "downloadFilter"]) $(id).addEventListener(id === "episodeFilter" ? "input" : "change", () => { episodePage = 0; renderEpisodes(); });
$("selectAllEpisodes").onchange = event => { filteredEpisodes().forEach(episode => episode.checked = Boolean(event.target.checked && episode.selected)); renderEpisodes(); };
$("selectMissing").onclick = () => { filteredEpisodes().forEach(episode => episode.checked = episode.download_status === "missing" && Boolean(episode.selected)); renderEpisodes(); };
$("previousEpisodes").onclick = () => { episodePage--; renderEpisodes(); };
$("nextEpisodes").onclick = () => { episodePage++; renderEpisodes(); };
action("loadMoreEpisodes", () => loadCatalogPage());
action("loadAllEpisodes", () => loadCatalogPage(true));
$("episodeList").addEventListener("change", event => {
  if (event.target.matches(".ep-check")) { currentEpisodes[Number(event.target.dataset.idx)].checked = event.target.checked; renderEpisodes(); }
});
$("episodeList").addEventListener("click", event => {
  const badge = event.target.closest(".ep-badge");
  if (badge && !badge.disabled) { currentEpisodes[Number(badge.dataset.idx)].selected = badge.dataset.platform; renderEpisodes(); }
});
async function queueSelected() {
  const selected = currentEpisodes.filter(episode => episode.checked);
  const items = selected.map(episode => ({url: episode.selected === "youtube" ? episode.youtube_url : episode.rt_url, keys: episode.keys,
    show: episode.show, season: episode.season, episode: episode.episode_number, title: episode.title, description: episode.description, release_date: episode.release_date}));
  const result = await api("/api/download", {items});
  queuedToast(result);
  selected.forEach(episode => {
    episode.checked = false;
    episode.download_status = "queued";
  });
  renderEpisodes(); await refresh();
}
action("queueSelected", () => queueSelected());

action("followShow", async () => {
  await api("/api/follows", {show_id: catalog.showId, title: catalog.title, preference: currentPref, interval_hours: Number($("followHours").value), auto_queue: $("followAuto").checked});
  await refreshFollows(); toast("Show followed. The first check records its existing catalog.");
});
action("checkFollows", async () => { await api("/api/follows/check", {}); toast("Checking followed shows."); });
async function refreshFollows() {
  const {follows} = await api("/api/follows");
  $("follows").innerHTML = follows.map(follow => `<div class="follow"><div class="row spread"><span>${escapeHtml(follow.title || follow.id)}</span><button data-unfollow="${escapeHtml(follow.id)}" class="job-btn">Unfollow</button></div><small>${follow.auto_queue ? "Queue new episodes" : "Notify about new episodes"} · every ${follow.interval_hours} hours · ${follow.last_check ? new Date(follow.last_check * 1000).toLocaleString() : "Awaiting first check"}</small>${follow.error ? `<div class="notice">${escapeHtml(follow.error)}</div>` : ""}</div>`).join("") || '<p class="browse-meta">Follow a show from the archive browser.</p>';
}
$("follows").addEventListener("click", async event => {
  const button = event.target.closest("[data-unfollow]");
  if (!button) return;
  try { await api(`/api/follows/${encodeURIComponent(button.dataset.unfollow)}`, {}, "DELETE"); await refreshFollows(); }
  catch (error) { toast(error.message); }
});

let jobOffset = 0, queuePaused = false, jobGeneration = 0;
let jobController = null;
const jobPageSize = 50;
function transferStats(job) {
  const progress = job.status === "uploading"
    ? "Copying " + bytes(job.upload_bytes) + " / " + bytes(job.upload_total_bytes)
    : bytes(job.downloaded_bytes) + (job.total_bytes ? " / " + bytes(job.total_bytes) : "");
  return progress + " " + (job.speed || "") + (job.eta ? " · ETA " + job.eta : "")
    + " · attempt " + job.attempts
    + (job.status === "retry_wait" ? " · retry " + new Date(job.next_attempt_at * 1000).toLocaleTimeString() : "");
}
function cardStructure(job) {
  return JSON.stringify([job.status, job.title, job.show, job.url, job.error, job.attempts,
    job.download_complete, job.next_attempt_at, Boolean(job.has_log || job.log?.length)]);
}
function row(job) {
  const active = ["queued", "running", "uploading", "paused", "retry_wait"].includes(job.status);
  const retry = ["error", "cancelled"].includes(job.status);
  const button = (operation, text, title = "") => `<button class="job-btn" data-action="${operation}" data-job-id="${job.id}" title="${title}">${text}</button>`;
  const hasLog = job.has_log || job.log?.length;
  return `<div class="job ${job.status}" data-job-id="${job.id}">
    <div class="job-top">
      <div style="min-width:0;flex:1">
        <div class="job-title">${escapeHtml(job.title || job.show || "Resolving…")}</div>
        <div class="job-url">${escapeHtml(job.url)}</div>
      </div>
      <div class="job-right">
        ${active ? button("cancel", "Cancel") : ""}
        ${retry ? button("retry", job.download_complete ? "Retry upload" : "Retry") : ""}
        ${!active ? button("remove", "Remove", "Remove this job and its staged files. Completed history is retained.") : ""}
        <span class="job-state ${job.status}">${escapeHtml(job.status.replace("_", " "))}</span>
      </div>
    </div>
    <div class="bar"><span style="width:${Math.max(0, Math.min(100, job.percent || 0))}%"></span></div>
    <div class="stats">${escapeHtml(transferStats(job))}</div>
    ${job.error ? `<div class="err">${escapeHtml(job.error)}</div>` : ""}
    ${hasLog ? `<details data-log="${job.id}"><summary>Log</summary><pre>${escapeHtml(job.log?.join("\n") || "Open to load log output.")}</pre></details>` : ""}
  </div>`;
}
function renderJobs(jobs) {
  const container = $("jobs");
  if (!jobs.length) {
    const empty = container.querySelector(".empty");
    if (empty) empty.textContent = "No matching transfers.";
    else container.innerHTML = '<div class="empty">No matching transfers.</div>';
    return;
  }
  const oldCards = [...container.querySelectorAll(".job[data-job-id]")];
  const byId = new Map(oldCards.map(card => [card.dataset.jobId, card]));
  const sameOrder = oldCards.length === jobs.length && jobs.every((job, i) => job.id === oldCards[i].dataset.jobId);
  const cards = jobs.map(job => {
    const previous = byId.get(job.id);
    const structure = cardStructure(job);
    if (previous && previous.dataset.structure === structure) {
      const width = `${Math.max(0, Math.min(100, job.percent || 0))}%`;
      const bar = previous.querySelector(".bar > span");
      if (bar.style.width !== width) bar.style.width = width;
      const stats = previous.querySelector(".stats");
      const text = transferStats(job);
      if (stats.textContent !== text) stats.textContent = text;
      return previous;
    }
    const template = document.createElement("template");
    template.innerHTML = row(job);
    const card = template.content.firstElementChild;
    card.dataset.structure = structure;
    const oldLog = previous?.querySelector("details[data-log]");
    const newLog = card.querySelector("details[data-log]");
    if (oldLog?.open && newLog) {
      newLog.open = true;
      newLog.querySelector("pre").textContent = oldLog.querySelector("pre").textContent;
    }
    if (sameOrder && previous) previous.replaceWith(card);
    return card;
  });
  if (!sameOrder) container.replaceChildren(...cards);
}
async function loadJobLog(details) {
  if (details.logRequest) return details.logRequest;
  details.logRequest = api(`/api/jobs/${encodeURIComponent(details.dataset.log)}/logs`)
    .then(result => {
      if (!details.isConnected || !details.open) return;
      const pre = details.querySelector("pre");
      const text = result.log.join("\n") || "No log output yet.";
      if (pre.textContent !== text) pre.textContent = text;
    })
    .catch(error => {
      if (details.isConnected && details.open) details.querySelector("pre").textContent = error.message;
    })
    .finally(() => { details.logRequest = null; });
  return details.logRequest;
}
$("jobs").addEventListener("toggle", event => {
  if (event.target.matches("details[data-log][open]")) loadJobLog(event.target);
}, true);

async function refresh() {
  const generation = ++jobGeneration;
  jobController?.abort();
  const controller = new AbortController();
  jobController = controller;
  try {
    const params = new URLSearchParams({limit: jobPageSize, offset: jobOffset,
      q: $("jobSearch").value, status: $("jobStatus").value, include_logs: "false"});
    const result = await api("/api/jobs?" + params, undefined, "GET", controller.signal);
    if (generation !== jobGeneration) return;
    queuePaused = result.paused;
    $("pauseQueue").textContent = queuePaused ? "Resume queue" : "Pause queue";
    $("pauseReason").textContent = result.pause_reason || result.notification_error || "";
    $("queueSummary").innerHTML = Object.entries(result.counts).filter(([, count]) => count).map(([state, count]) => `<span>${count.toLocaleString()} ${state.replace("_", " ")}</span>`).join("") + `<span>${bytes(result.completed_bytes)} delivered</span>`;
    renderJobs(result.jobs);
    for (const details of $("jobs").querySelectorAll("details[data-log][open]")) loadJobLog(details);
    $("previousJobs").disabled = jobOffset === 0;
    $("nextJobs").disabled = jobOffset + jobPageSize >= result.total;
    $("jobPage").textContent = result.total ? `${jobOffset + 1}–${Math.min(jobOffset + jobPageSize, result.total)} of ${result.total.toLocaleString()}` : "0 transfers";
    if (jobOffset >= result.total && jobOffset) { jobOffset = 0; await refresh(); }
  } catch (error) {
    if (error.name !== "AbortError") $("pauseReason").textContent = error.message;
  } finally {
    if (jobController === controller) jobController = null;
  }
}
action("pauseQueue", async () => { await api(queuePaused ? "/api/queue/resume" : "/api/queue/pause", {}); await refresh(); });
let jobTimer;
$("jobSearch").addEventListener("input", () => { clearTimeout(jobTimer); jobTimer = setTimeout(() => { jobOffset = 0; refresh(); }, 250); });
$("jobStatus").onchange = () => { jobOffset = 0; refresh(); };
$("previousJobs").onclick = () => { jobOffset = Math.max(0, jobOffset - jobPageSize); refresh(); };
$("nextJobs").onclick = () => { jobOffset += jobPageSize; refresh(); };
$("jobs").addEventListener("click", async event => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  button.disabled = true;
  try {
    const {action: operation, jobId} = button.dataset;
    await api(operation === "remove" ? `/api/jobs/${jobId}` : `/api/jobs/${jobId}/${operation}`, {}, operation === "remove" ? "DELETE" : "POST");
    await refresh();
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});
for (const [id, path] of [["clear", "/api/jobs/clear"], ["clearDone", "/api/jobs/clear/done"], ["retryFailed", "/api/jobs/retry/failed"]]) action(id, async () => { await api(path, {}); await refresh(); });
async function refreshBatches() {
  const {batches} = await api("/api/batches?limit=30");
  $("batches").innerHTML = batches.slice(0, 30).map(batch => `<div class="batch">${new Date(batch.queued_at * 1000).toLocaleString()} · ${batch.done.toLocaleString()} / ${batch.total.toLocaleString()} completed · ${batch.error} failed · ${batch.active} pending · ${bytes(batch.bytes)}</div>`).join("") || "No batches yet.";
}
action("scanThumbs", async () => { await api("/api/thumbnails/scan", {}); await pollScans(); });
action("scanLibrary", async () => { await api("/api/library/scan", {}); await pollScans(); });
async function pollScans() {
  const [thumbnails, library] = await Promise.all([api("/api/thumbnails/scan"), api("/api/library/scan")]);
  $("scanThumbs").disabled = thumbnails.running;
  $("scanStatus").textContent = thumbnails.total ? `${thumbnails.checked}/${thumbnails.total} checked · ${thumbnails.added} added · ${thumbnails.errors} failed` : "";
  $("scanLibrary").disabled = library.running;
  $("libraryStatus").textContent = library.error || (library.checked ? `${library.checked} files checked · ${library.recorded} recorded${library.running ? " · scanning…" : ""}. Reload the show to refresh its download status.` : "");
}
$("toTop").onclick = () => window.scrollTo({top: 0, behavior: "smooth"});
window.addEventListener("scroll", () => $("toTop").classList.toggle("show", window.scrollY > 400), {passive: true});
setPref("roosterteeth");
Promise.all([loadConfig(), refresh(), refreshBatches(), refreshFollows(), pollScans()]).catch(error => toast(error.message));
setInterval(() => { if (!document.hidden && !jobController) refresh(); }, 3000);
let auxiliaryBusy = false;
async function refreshAuxiliary() {
  if (document.hidden || auxiliaryBusy) return;
  auxiliaryBusy = true;
  try { await Promise.all([refreshBatches(), refreshFollows(), pollScans()]); }
  catch { /* Keep the last successful auxiliary state. */ }
  finally { auxiliaryBusy = false; }
}
setInterval(refreshAuxiliary, 10000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { refresh(); refreshAuxiliary(); }
});
