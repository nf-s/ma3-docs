"use strict";

const elements = {
  main: document.querySelector("#main"),
  search: document.querySelector("#search"),
  history: document.querySelector("#include-history"),
  category: document.querySelector("#category"),
  commandSource: document.querySelector("#command-source"),
  version: document.querySelector("#version"),
  navigation: document.querySelector("#navigation"),
  sidebarTitle: document.querySelector("#sidebar-title"),
  navCount: document.querySelector("#nav-count"),
};
const state = {
  mode: "manual",
  stats: null,
  commandLinks: null,
  virtualList: null,
  searchAbort: null,
  referenceCounts: { commands: 0, lua: 0 },
  navigation: { manual: [], releases: [], guides: [] },
  offset: 0,
  request: 0,
  documentId: "",
  documentToc: [],
  documentMetadata: null,
  navigationPaths: new Map(),
  navigationTitles: new Map(),
  navigationReveal: true,
  navigationFrame: null,
  bugEntries: [],
  timer: null,
  observer: null,
  loading: false,
};

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined && text !== null) result.textContent = String(text);
  return result;
}

function link(text, href, className) {
  const result = node("a", className, text);
  const destination = new URL(href, window.location.href);
  result.href = ["http:", "https:", "mailto:"].includes(destination.protocol) ? destination.href : "#";
  return result;
}

function button(text, className, action) {
  const result = node("button", className, text);
  result.type = "button";
  result.addEventListener("click", action);
  return result;
}

function docUrl(id, anchor = "") {
  const parameters = { doc: id };
  if (state.mode === "bugs" && id.startsWith("release")) {
    parameters.mode = "bugs";
    parameters.version = elements.version.value;
  }
  return `/?${new URLSearchParams(parameters)}${anchor ? `#${encodeURIComponent(anchor)}` : ""}`;
}

function modeUrl(mode, parameters = {}) {
  return `/?${new URLSearchParams({ mode, ...parameters })}`;
}

function releaseEditions() {
  return [...new Set(state.navigation.releases.map((item) => item.version))].sort((a, b) => b.localeCompare(a, undefined, { numeric: true }));
}

function indexNavigation() {
  state.navigationPaths.clear();
  state.navigationTitles.clear();
  function visit(items, ancestors = []) {
    for (const item of items) {
      if (!item || typeof item.id !== "string" || typeof item.title !== "string") continue;
      const path = [...ancestors, { id: item.id, title: item.title }];
      state.navigationPaths.set(item.id, path);
      const matches = state.navigationTitles.get(item.title) || [];
      matches.push({ id: item.id, title: item.title });
      state.navigationTitles.set(item.title, matches);
      if (Array.isArray(item.children)) visit(item.children, path);
    }
  }
  const manual = Array.isArray(state.navigation.manual) ? state.navigation.manual : state.navigation.manual.children || [];
  visit(manual);
  visit(state.navigation.guides || []);
  visit(state.navigation.releases || []);
}

function selectedOccurrence(item) {
  const edition = item.matched_edition || (state.mode === "bugs" ? elements.version.value : "");
  if (!edition) return null;
  return [...(item.matched_occurrences || []), ...(item.occurrences || [])].find((occurrence) => occurrence.publication_version === edition) || null;
}

function sourceHref(item) {
  return item.source_url || docUrl(item.document_id || item.id);
}

function editionHref(version, item = {}) {
  const occurrence = [...(item.matched_occurrences || []), ...(item.occurrences || [])].find((entry) => entry.publication_version === version && entry.source_url);
  if (occurrence) return occurrence.source_url;
  const edition = state.navigation.releases.find((entry) => entry.version === version);
  if (edition) return sourceHref(edition);
  return item.id || item.document_id ? sourceHref(item) : modeUrl("releases", { version });
}

function manualHref() {
  const roots = Array.isArray(state.navigation.manual) ? state.navigation.manual : [];
  return roots[0]?.id ? docUrl(roots[0].id) : "https://help.malighting.com/grandMA3/";
}

function sourceLink(text, href, className = "") {
  const reference = link(text, href, className);
  const destination = new URL(reference.href);
  if (destination.origin !== window.location.origin || destination.pathname.startsWith("/release-notes-pdf/")) {
    reference.target = "_blank";
    reference.rel = "noopener noreferrer";
  }
  return reference;
}

function commandSourceLink(item, className = "") {
  const name = item.source_kind === "live-introspection" ? "Live introspection" : item.authority === "release-note" ? "Historical release note" : item.authority === "editorial" ? "Guide" : "Manual";
  const provenance = [...(Array.isArray(item.provenance) ? item.provenance : []), ...(item.occurrences || [])].find((entry) => entry.source_url === sourceHref(item));
  const publication = item.publication_version || provenance?.publication_version;
  const edition = publication && publication !== item.version ? ` · edition ${publication}` : "";
  return sourceLink(`${name} ${item.version}${edition}${item.source_page ? ` · p. ${item.source_page}` : ""}`, sourceHref(item), className);
}

function commandSources(item) {
  return Array.isArray(item.source_records) && item.source_records.length ? item.source_records : [item];
}

function commandProvenanceSources(items) {
  const sources = new Map();
  for (const item of items) {
    const occurrences = [...(Array.isArray(item.provenance) ? item.provenance : []), ...(item.occurrences || [])];
    const primary = occurrences.find((entry) => entry.source_url === sourceHref(item));
    for (const source of [{ ...item, publication_version: item.publication_version || primary?.publication_version }, ...occurrences.filter((entry) => entry.source_url).map((entry) => ({ ...item, ...entry }))]) {
      const key = JSON.stringify([source.source_document_id || source.document_id, sourceHref(source), source.version, source.publication_version || "", source.source_page || ""]);
      if (!sources.has(key)) sources.set(key, source);
    }
  }
  return [...sources.values()];
}

function commandMetadataRows(items, documentData = {}) {
  const metadata = node("div", "command-sources");
  for (const item of items) {
    const row = node("p", "result-meta", `${friendlyCategory(item.type)} · `);
    const sourceId = item.source_document_id || item.document_id;
    const sourcePath = state.navigationPaths.get(sourceId);
    const sourceTitle = item.source_document_title || (sourceId === documentData.id ? documentData.title : sourcePath?.[sourcePath.length - 1]?.title) || "Source topic";
    row.append(link(sourceTitle, docUrl(sourceId)), document.createTextNode(" · "), commandSourceLink(item));
    metadata.append(row);
  }
  return metadata;
}

let sourcePopoverId = 0;

function commandMetadata(items, documentData = {}) {
  const sources = commandProvenanceSources(items);
  const metadata = commandMetadataRows(sources, documentData);
  const count = sources.length;
  if (count <= 1) return metadata;
  const wrapper = node("div", "command-source-control");
  const trigger = button(`${count} sources`, "command-source-trigger", () => {
    if (pinned) { panel.hidePopover(); pinned = false; }
    else { pinned = true; show(); }
  });
  const panel = node("div", "command-source-popover");
  panel.id = `command-sources-${++sourcePopoverId}`;
  panel.setAttribute("popover", "auto");
  panel.setAttribute("role", "group");
  panel.setAttribute("aria-label", "Command sources and provenance");
  trigger.setAttribute("aria-controls", panel.id);
  trigger.setAttribute("aria-expanded", "false");
  panel.append(node("strong", "", "Sources & provenance"), metadata);
  wrapper.append(trigger, panel);
  let pinned = false;
  let timer = null;
  function show() {
    clearTimeout(timer);
    if (!panel.isConnected) return;
    panel.showPopover();
    const anchor = trigger.getBoundingClientRect();
    const width = panel.getBoundingClientRect().width;
    panel.style.left = `${Math.max(8, Math.min(anchor.left, window.innerWidth - width - 8))}px`;
    const height = panel.getBoundingClientRect().height;
    panel.style.top = `${Math.max(8, Math.min(anchor.bottom + 5, window.innerHeight - height - 8))}px`;
  }
  function delayClose() {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (!pinned && !wrapper.contains(document.activeElement) && !panel.matches(":hover")) panel.hidePopover();
    }, 180);
  }
  trigger.addEventListener("pointerenter", (event) => { if (event.pointerType === "mouse") show(); });
  trigger.addEventListener("pointerleave", delayClose);
  wrapper.addEventListener("focusin", show);
  wrapper.addEventListener("focusout", delayClose);
  panel.addEventListener("pointerenter", () => clearTimeout(timer));
  panel.addEventListener("pointerleave", delayClose);
  panel.addEventListener("toggle", () => {
    const open = panel.matches(":popover-open");
    trigger.setAttribute("aria-expanded", String(open));
    if (!open) pinned = false;
  });
  return wrapper;
}

function commandExtracts(items, documentData = {}, keepEmpty = false) {
  const unique = new Map();
  for (const group of items || []) {
    for (const item of commandSources(group)) if (item && typeof item.id === "string" && item.type !== "keyword") unique.set(item.id, item);
  }
  const priority = { manual: 0, editorial: 1, "release-note": 2 };
  const commands = [...unique.values()].sort((left, right) => {
    return (priority[left.authority] ?? 3) - (priority[right.authority] ?? 3) || Number(right.document_id === documentData.id) - Number(left.document_id === documentData.id);
  });
  const groups = new Map();
  for (const item of commands) {
    const key = JSON.stringify([item.text, item.language === "text" ? "ma3" : item.language ?? "ma3"]);
    const sources = groups.get(key) || [];
    sources.push(item);
    groups.set(key, sources);
  }
  if (!commands.length && !keepEmpty) return null;
  const section = node("section", "command-extracts article");
  section.id = "extracted-commands";
  section.append(node("h2", "", "Extracted syntax & command examples"));
  const sorted = [...groups.values()].sort((left, right) => left[0].text.localeCompare(right[0].text, undefined, { sensitivity: "base" }) || left[0].text.localeCompare(right[0].text));
  for (const sources of sorted) {
    const item = sources[0];
    const historical = sources.every((source) => source.authority === "release-note");
    const record = node("div", `command-extract${historical ? " extract-historical" : ""}`);
    record.setAttribute("data-command-id", item.id);
    record.append(commandMetadata(sources, documentData), commandCode(item, sources));
    section.append(record);
  }
  return section;
}

async function collectCommandPages(parameters, initialItems, firstPage, onPage, isCurrent) {
  const records = new Map((initialItems || []).map((item) => [item.id, item]));
  let offset = 0;
  let total = null;
  let pendingPage = firstPage;
  while (isCurrent()) {
    const page = pendingPage || await getJson("/api/commands", { ...parameters, authority: "all", type: "all", limit: 200, offset });
    pendingPage = null;
    if (!isCurrent()) return null;
    if (!page || !Array.isArray(page.results) || !Number.isSafeInteger(page.total) || page.total < 0 || page.offset !== offset || !Number.isSafeInteger(page.limit) || page.limit < 1 || page.limit > 200) throw new Error("The example catalog returned an invalid page.");
    if (total !== null && page.total !== total) throw new Error("The example catalog changed while loading. Please retry.");
    total = page.total;
    if (page.results.length !== Math.min(page.limit, Math.max(0, total - offset))) throw new Error("The example catalog returned an incomplete page.");
    for (const item of page.results) {
      for (const source of [item, ...commandSources(item || {})]) {
        if (!source || ["id", "document_id", "text", "version"].some((field) => typeof source[field] !== "string") || !["keyword", "syntax", "example"].includes(source.type) || !["manual", "release-note", "editorial"].includes(source.authority)) throw new Error("The example catalog returned an invalid record.");
      }
      records.set(item.id, { ...records.get(item.id), ...item });
    }
    offset += page.results.length;
    const items = [...records.values()];
    onPage(items, { loaded: offset, total });
    if (offset >= total) return items;
  }
  return null;
}

function loadDocumentExtracts(documentData, section, request) {
  const keyword = typeof documentData.command_lookup === "string" ? documentData.command_lookup : "";
  const parameters = keyword ? { keyword, ...(documentData.command_reference_id ? { keyword_document_id: documentData.command_reference_id } : {}) } : { document_id: documentData.id };
  let records = documentData.commands || [];
  const current = () => request === state.request && section.isConnected;
  const render = (message = "", retry = false) => {
    if (!current()) return;
    const contents = commandExtracts(records, documentData, true);
    section.replaceChildren(...contents.childNodes);
    if (message) {
      const status = node("p", "extract-load-status", message);
      status.setAttribute("role", retry ? "alert" : "status");
      if (retry) status.append(button("Retry", "small-button", () => load(false)));
      section.querySelector("h2")?.after(status);
    }
  };
  async function load(useInitialPage = true) {
    render(keyword ? "Loading matching syntax and examples…" : "Loading all syntax and examples…");
    try {
      const result = await collectCommandPages(parameters, records, useInitialPage ? documentData.commands_page : null, (items, progress) => {
        records = items;
        render(progress.loaded < progress.total ? `Loading matching examples… ${progress.loaded} of ${progress.total} results` : "");
      }, current);
      if (result && current()) {
        records = result;
        render(records.some((item) => item.type !== "keyword") ? "" : "No indexed syntax or examples found.");
      }
    } catch (error) {
      render(`Could not load every matching example. ${error.message}`, true);
    }
  }
  load();
}

function breadcrumbItems(item) {
  const historical = item.authority === "release-note";
  const knownBugs = historical && state.mode === "bugs";
  const root = historical
    ? { title: knownBugs ? "Known bugs" : "Release notes", href: modeUrl(knownBugs ? "bugs" : "releases", knownBugs ? { version: elements.version.value } : {}) }
    : { title: item.authority === "editorial" ? "Working guides" : "Manual", href: modeUrl("manual") };
  const crumbs = [root];
  let ancestors = [];
  if (historical && item.id.startsWith("release")) {
    const parentId = selectedOccurrence(item)?.document_id || item.parent_id;
    ancestors = state.navigationPaths.get(parentId) || [];
  } else {
    const path = state.navigationPaths.get(item.id);
    ancestors = path ? path.slice(0, -1) : state.navigationPaths.get(item.parent_id) || [];
    if (!ancestors.length) {
      for (const title of item.breadcrumbs || []) {
        const matches = state.navigationTitles.get(title) || [];
        if (matches.length === 1) ancestors.push(matches[0]);
      }
    }
  }
  const seen = new Set([item.id]);
  const manualRoots = Array.isArray(state.navigation.manual) ? state.navigation.manual.map((entry) => entry.id) : [];
  for (const ancestor of ancestors) {
    if (seen.has(ancestor.id) || manualRoots.includes(ancestor.id)) continue;
    seen.add(ancestor.id);
    crumbs.push({ title: ancestor.title, href: docUrl(ancestor.id) });
  }
  return crumbs;
}

function breadcrumbsFor(item, result = false) {
  const breadcrumbs = node("nav", result ? "breadcrumb result-breadcrumb" : "breadcrumb");
  breadcrumbs.setAttribute("aria-label", "Breadcrumb");
  for (const [index, crumb] of breadcrumbItems(item).entries()) {
    if (index) breadcrumbs.append(node("span", "separator", "/"));
    breadcrumbs.append(link(crumb.title, crumb.href));
  }
  return breadcrumbs;
}

function badge(authority, label) {
  return node("span", `badge${authority === "release-note" ? " history" : authority === "editorial" ? " neutral" : ""}`, label || (authority === "release-note" ? "Release note" : authority === "editorial" ? "Guide" : "Manual"));
}

function formatCount(value) {
  return Number(value || 0).toLocaleString();
}

function friendlyCategory(value) {
  const names = { command: "Command", option: "Option", lua: "Lua API", topic: "Topic", index: "Index", release_note: "Release history", "bug-fixes": "Bug fixes", features: "Features", changes: "Changes", limitations: "Limitations", deprecated: "Deprecated", enhancements: "Enhancements", appendix: "Appendix", example: "Example", syntax: "Syntax", keyword: "Keyword" };
  return names[value] || String(value || "Reference").replaceAll("-", " ");
}

async function getJson(path, parameters = {}, signal) {
  const query = new URLSearchParams(parameters);
  const response = await fetch(`${path}${query.size ? `?${query}` : ""}`, { signal });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "The reference could not be loaded.");
  return data;
}

function setUrl(parameters, anchor = "", replace = false) {
  const query = new URLSearchParams(parameters);
  const destination = `/${query.size ? `?${query}` : ""}${anchor ? `#${encodeURIComponent(anchor)}` : ""}`;
  window.history[replace ? "replaceState" : "pushState"]({}, "", destination);
}

function closeMenu() {
  document.body.classList.remove("menu-open");
  document.querySelector("#menu-toggle").setAttribute("aria-expanded", "false");
}

function showToast(message) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.hidden = false;
  window.setTimeout(() => { toast.hidden = true; }, 2200);
}

function showError(error, retry = route) {
  elements.main.replaceChildren();
  const box = node("section", "empty-state");
  box.append(node("h2", "", "Unable to load the reference"), node("p", "", error.message));
  box.append(button("Try again", "small-button", retry));
  elements.main.append(box);
}

function commandMode() {
  return state.mode === "commands" || state.mode === "lua";
}

function clearVirtualList() {
  state.searchAbort?.abort();
  state.searchAbort = null;
  state.virtualList?.destroy();
  state.virtualList = null;
}

function updateFilters() {
  const selectedVersion = elements.version.value;
  const options = commandMode()
    ? [["keyword", state.mode === "lua" ? "Functions" : "Keywords & options"], ["syntax", "Syntax templates"], ["example", "Command examples"], ["all", "All extracts"]]
    : state.mode === "bugs" ? [["limitations", "Known limitations"]]
    : state.mode === "releases"
      ? [["", "All categories"], ["features", "Features"], ["changes", "Changes"], ["bug-fixes", "Bug fixes"], ["enhancements", "Enhancements"], ["deprecated", "Deprecated"], ["limitations", "Limitations"]]
      : [["", "All topics"], ["command", "Commands"], ["option", "Options"], ["lua", "Lua API"], ["topic", "Topics"], ["index", "Indexes"]];
  elements.category.replaceChildren(...options.map(([value, label]) => {
    const option = node("option", "", label);
    option.value = value;
    return option;
  }));
  elements.history.closest("label").hidden = state.mode !== "manual";
  elements.commandSource.hidden = !commandMode();
  elements.category.hidden = state.mode === "bugs";
  elements.version.hidden = commandMode();
  const versions = state.mode === "bugs" ? releaseEditions() : [...(state.stats?.versions || [])].map((item) => item.name).sort((a, b) => b.localeCompare(a, undefined, { numeric: true }));
  elements.version.replaceChildren();
  if (state.mode !== "bugs") {
    const all = node("option", "", "All versions");
    all.value = "";
    elements.version.append(all);
  }
  for (const version of versions) {
    const option = node("option", "", `${state.mode === "bugs" ? "Edition" : "Version"} ${version}`);
    option.value = version;
    elements.version.append(option);
  }
  elements.version.value = versions.includes(selectedVersion) ? selectedVersion : state.mode === "bugs" ? versions[0] || "" : "";
  elements.search.placeholder = commandMode() ? state.mode === "lua" ? "Search Lua functions and examples…" : "Search commands and options…" : state.mode === "bugs" ? "Search known limitations in this edition…" : state.mode === "releases" ? "Search release notes…" : "Search documentation…";
  for (const tab of document.querySelectorAll("[data-mode]")) {
    tab.classList.toggle("active", tab.dataset.mode === state.mode);
    tab.setAttribute("aria-current", tab.dataset.mode === state.mode ? "page" : "false");
  }
}

function makeNavNode(item) {
  const children = Array.isArray(item.children) ? item.children : [];
  if (!children.length) {
    const itemLink = link(item.title, docUrl(item.id), "nav-link");
    itemLink.dataset.documentId = item.id;
    itemLink.classList.toggle("selected", item.id === state.documentId);
    return itemLink;
  }
  const details = node("details", "nav-group");
  const summary = node("summary", "", item.title);
  details.append(summary);
  let populated = false;
  const populate = () => {
    if (populated) return;
    populated = true;
    const list = node("div", "nav-children");
    if (item.id) {
      const overview = link(`${item.title} overview`, docUrl(item.id), "nav-link");
      overview.dataset.documentId = item.id;
      overview.classList.toggle("selected", item.id === state.documentId);
      list.append(overview);
    }
    list.append(...children.map(makeNavNode));
    details.append(list);
  };
  details.addEventListener("toggle", () => { if (details.open) populate(); });
  const activePath = state.navigationPaths.get(state.documentId) || state.navigationPaths.get(state.documentMetadata?.parent_id) || [];
  if (activePath.some((entry) => entry.id === item.id) || (!item.id && children.some((entry) => entry.id === state.documentId))) {
    populate();
    details.open = true;
  }
  return details;
}

function sortVersions(a, b) {
  return String(b.version || "").localeCompare(String(a.version || ""), undefined, { numeric: true });
}

function sourceTocTree(entries) {
  const root = { level: 0, children: [] };
  const stack = [root];
  for (const entry of Array.isArray(entries) ? entries : []) {
    if (!entry || typeof entry.title !== "string" || typeof entry.anchor !== "string" || !entry.anchor || !Number.isInteger(entry.level) || entry.level < 1 || entry.level > 6) continue;
    const item = { title: entry.title, anchor: entry.anchor, level: entry.level, children: [] };
    while (stack.length > 1 && entry.level <= stack[stack.length - 1].level) stack.pop();
    stack[stack.length - 1].children.push(item);
    stack.push(item);
  }
  return root.children;
}

function makeReleaseTocNode(item, documentId, depth = 0) {
  const title = link(item.title, docUrl(documentId, item.anchor), "nav-link");
  title.dataset.documentId = documentId;
  title.dataset.documentAnchor = item.anchor;
  if (!item.children.length) return title;
  const group = node("details", "nav-group");
  const summary = node("summary");
  const children = node("div", "nav-children");
  summary.append(title);
  children.append(...item.children.map((child) => makeReleaseTocNode(child, documentId, depth + 1)));
  group.append(summary, children);
  group.open = depth === 0;
  return group;
}

function renderNavigation() {
  elements.navigation.replaceChildren();
  if (state.mode === "bugs") {
    elements.sidebarTitle.textContent = "Known limitations by edition";
    elements.navCount.textContent = formatCount(releaseEditions().length);
    for (const version of releaseEditions()) {
      const edition = link(`Edition ${version}`, modeUrl("bugs", { version }), "nav-link");
      edition.classList.toggle("selected", version === elements.version.value && !state.documentId);
      elements.navigation.append(edition);
      if (version === elements.version.value) {
        const children = node("div", "nav-children");
        const entries = [...state.bugEntries];
        if (state.documentMetadata && !entries.some((item) => item.id === state.documentId)) entries.unshift(state.documentMetadata);
        for (const item of entries) {
          const itemLink = link(item.title.replace(/^Known Limitations:\s*/i, ""), docUrl(item.id), "nav-link");
          itemLink.dataset.documentId = item.id;
          itemLink.classList.toggle("selected", item.id === state.documentId);
          children.append(itemLink);
        }
        elements.navigation.append(children);
      }
    }
    return;
  }
  if (commandMode()) {
    elements.sidebarTitle.textContent = state.mode === "lua" ? "Lua reference" : "Command reference";
    elements.navCount.textContent = formatCount(state.referenceCounts[state.mode]);
    for (const [title, type, source] of [[state.mode === "lua" ? "Functions" : "Keywords & options", "keyword", state.mode === "lua" ? "all" : "manual"], ["Syntax templates", "syntax", "manual"], ["Manual examples", "example", "manual"], ["Release-note examples", "example", "release-note"]]) {
      elements.navigation.append(button(title, "nav-link", () => {
        elements.category.value = type;
        elements.commandSource.value = source;
        elements.search.value = "";
        state.offset = 0;
        runSearch();
      }));
    }
    if (state.mode === "lua" && state.stats?.has_lua_coverage) {
      elements.navigation.append(link("API coverage & sources", docUrl("live-lua/coverage-audit"), "nav-link"));
    }
    return;
  }
  if (state.mode === "releases") {
    elements.sidebarTitle.textContent = "Source editions";
    elements.navCount.textContent = formatCount(state.navigation.releases.length);
    for (const item of [...state.navigation.releases].sort(sortVersions)) {
      const itemLink = link(item.title, docUrl(item.id), "nav-link");
      itemLink.dataset.documentId = item.id;
      itemLink.classList.toggle("selected", item.id === state.documentId);
      elements.navigation.append(itemLink);
      if (item.id === state.documentId && state.documentToc.length) {
        const toc = node("nav", "release-toc nav-children");
        toc.setAttribute("aria-label", `${item.title} contents`);
        toc.append(...state.documentToc.map((entry) => makeReleaseTocNode(entry, item.id)));
        elements.navigation.append(toc);
      }
      if (item.id === state.documentMetadata?.parent_id && state.documentId !== item.id) {
        const children = node("div", "nav-children");
        const current = link(state.documentMetadata.title, docUrl(state.documentId), "nav-link selected");
        current.dataset.documentId = state.documentId;
        children.append(current);
        elements.navigation.append(children);
      }
    }
    return;
  }
  elements.sidebarTitle.textContent = "Manual contents";
  elements.navCount.textContent = formatCount(state.stats?.manual_count);
  if (state.navigation.guides?.length) {
    const guideGroup = { title: "Working guides", children: state.navigation.guides };
    const group = makeNavNode(guideGroup);
    group.open = true;
    elements.navigation.append(group);
  }
  const tree = Array.isArray(state.navigation.manual) ? state.navigation.manual : state.navigation.manual.children || [];
  const items = tree.length === 1 && tree[0].children?.length ? tree[0].children : tree;
  elements.navigation.append(...items.map(makeNavNode));
  if (state.documentMetadata && ![...elements.navigation.querySelectorAll("[data-document-id]")].some((item) => item.dataset.documentId === state.documentId)) {
    const current = link(state.documentMetadata.title, docUrl(state.documentId), "nav-link selected");
    current.dataset.documentId = state.documentId;
    elements.navigation.append(current);
  }
}

function setNavigationReveal(reveal) {
  state.navigationReveal = reveal;
  if (state.navigationFrame !== null) {
    cancelAnimationFrame(state.navigationFrame);
    state.navigationFrame = null;
  }
}

function navigationSpacer() {
  let spacer = elements.navigation.querySelector(".nav-scroll-spacer");
  if (!spacer) {
    spacer = node("div", "nav-scroll-spacer");
    spacer.setAttribute("aria-hidden", "true");
    elements.navigation.append(spacer);
  }
  return spacer;
}

function revealNavigationSelection(anchor = "") {
  setNavigationReveal(state.navigationReveal);
  const links = [...elements.navigation.querySelectorAll("[data-document-id]")];
  for (const item of links) {
    const selected = item.dataset.documentId === state.documentId && (!item.dataset.documentAnchor || item.dataset.documentAnchor === anchor);
    item.classList.toggle("selected", selected);
    if (selected) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  const selected = links.find((item) => item.dataset.documentId === state.documentId && item.dataset.documentAnchor === anchor) || links.find((item) => item.classList.contains("selected"));
  if (!selected || !state.navigationReveal) return;
  let parent = selected.parentElement;
  while (parent && parent !== elements.navigation) {
    if (parent.tagName === "DETAILS") parent.open = true;
    parent = parent.parentElement;
  }
  state.navigationFrame = requestAnimationFrame(() => {
    state.navigationFrame = null;
    if (!state.navigationReveal || !selected.isConnected) return;
    const spacer = navigationSpacer();
    const top = elements.navigation.getBoundingClientRect().top;
    const scrollTop = elements.navigation.scrollTop;
    const destination = Math.max(0, scrollTop + selected.getBoundingClientRect().top - top - 12);
    const contentEnd = scrollTop + spacer.getBoundingClientRect().top - top;
    const bottomPadding = Number.parseFloat(getComputedStyle(elements.navigation).paddingBottom) || 0;
    const missingSpace = Math.max(0, destination + elements.navigation.clientHeight - contentEnd - bottomPadding);
    spacer.style.height = `${Math.ceil(missingSpace)}px`;
    elements.navigation.scrollTop = destination;
  });
}

function selectMode(mode, updateUrl = true, parameters = new URLSearchParams()) {
  clearVirtualList();
  setNavigationReveal(true);
  state.mode = ["manual", "commands", "lua", "releases", "bugs"].includes(mode) ? mode : "manual";
  state.documentId = "";
  state.documentToc = [];
  state.documentMetadata = null;
  state.bugEntries = [];
  state.offset = 0;
  elements.search.value = "";
  elements.history.checked = false;
  elements.commandSource.value = state.mode === "lua" ? "all" : "manual";
  elements.version.value = "";
  updateFilters();
  if (parameters.has("version") && [...elements.version.options].some((option) => option.value === parameters.get("version"))) elements.version.value = parameters.get("version");
  if (parameters.has("category") && state.mode !== "bugs") elements.category.value = parameters.get("category");
  elements.search.value = parameters.get("q") || "";
  elements.history.checked = parameters.get("history") === "1";
  if (parameters.has("authority")) elements.commandSource.value = parameters.get("authority");
  renderNavigation();
  closeMenu();
  if (updateUrl) setUrl(state.mode === "manual" ? {} : { mode: state.mode, ...(state.mode === "bugs" ? { version: elements.version.value } : {}) });
  if (state.mode === "releases" && parameters.get("review") === "1") renderRevisions(false);
  else if (state.mode === "bugs" || ["q", "category", "history", "version", "authority"].some((name) => parameters.has(name))) runSearch(false);
  else if (state.mode === "manual") renderHome();
  else if (state.mode === "releases") renderReleases();
  else runSearch(false);
}

function renderHome() {
  state.request += 1;
  state.observer?.disconnect();
  document.title = "MA3 Reference";
  const header = node("header", "overview-header");
  if (state.stats?.documents === 0) {
    header.append(node("h1", "", "Get started"));
    const sources = node("p");
    sources.append("In a local checkout, import your own manual, release notes, or both. ",
      sourceLink("Official manual", "https://help.malighting.com/grandMA3/"), " · ",
      sourceLink("Release-note downloads", "https://www.malighting.com/downloads/products/grandma3/"));
    const steps = node("ol");
    const manual = node("li");
    manual.append("Copy the installed language folder (including HTML and _webHelpScripts) to ", node("code", "", "sources/manual/"), ".");
    const releases = node("li");
    releases.append("Place any release-note PDFs in ", node("code", "", "release-notes-pdf/"), ".");
    const build = node("li");
    build.append("Stop the viewer and follow the repository README to install the import tools. Run ", node("code", "", "sh scripts/build_all.sh"), ", then start the viewer again.");
    steps.append(manual, releases, build);
    elements.main.replaceChildren(header, sources, steps);
    return;
  }
  const overview = node("p", "", `${formatCount(state.stats?.manual_count)} topics · ${formatCount(state.stats?.keyword_count)} commands, options and functions · `);
  overview.append(sourceLink("Manual source", manualHref()));
  header.append(node("h1", "", "grandMA3 manual"), overview);
  const shortcuts = node("div", "shortcut-row");
  shortcuts.append(button("Command index", "small-button", () => selectMode("commands")), button(`${state.stats?.pdf_count || 0} release-note editions`, "small-button", () => selectMode("releases")));
  const content = node("div", "overview-columns");
  if (state.navigation.guides?.length) {
    const guides = node("section", "overview-section");
    guides.append(node("h2", "", "Working guides"));
    for (const guide of state.navigation.guides) guides.append(link(guide.title, docUrl(guide.id), "overview-link"));
    content.append(guides);
  }
  const tree = Array.isArray(state.navigation.manual) ? state.navigation.manual : state.navigation.manual.children || [];
  const groups = tree.length === 1 && tree[0].children?.length ? tree[0].children : tree;
  for (const group of groups) {
    const section = node("section", "overview-section");
    const title = node("h2", "");
    title.append(link(group.title, docUrl(group.id)));
    section.append(title);
    for (const child of (group.children || []).slice(0, 9)) section.append(link(child.title, docUrl(child.id), "overview-link"));
    if ((group.children || []).length > 9) section.append(link(`All ${group.children.length} topics →`, docUrl(group.id), "overview-link more-link"));
    content.append(section);
  }
  elements.main.replaceChildren(header, shortcuts, content);
}

function renderReleases() {
  state.request += 1;
  state.observer?.disconnect();
  document.title = "Release history · MA3 Reference";
  const hero = node("section", "hero");
  hero.append(node("h1", "", "Release notes"), node("p", "hero-description", `${state.navigation.releases.length} source editions. Repeated entries are combined in search and retain all source links.`));
  const warning = node("div", "history-warning");
  warning.append(node("strong", "", "Historical context. "), document.createTextNode("Earlier notes may describe behaviour that changed again. The "), sourceLink("grandMA3 manual", manualHref()), document.createTextNode(" remains the baseline; release-note examples retain their original version."));
  const grid = node("div", "release-grid");
  for (const item of [...state.navigation.releases].sort(sortVersions)) {
    const card = link("", docUrl(item.id), "release-card");
    card.append(badge("release-note", `Version ${item.version}`), node("h2", "", item.title), node("p", "", "Complete source edition · PDF page provenance"));
    grid.append(card);
  }
  if (!grid.children.length) grid.append(node("p", "muted", "No release notes imported. Add release-note PDFs to release-notes-pdf/ in your local checkout and rebuild the index."));
  elements.main.replaceChildren(hero, warning);
  if (state.stats?.revision_candidate_count) elements.main.append(button(`Review ${state.stats.revision_candidate_count} possible wording revisions`, "small-button", () => renderRevisions()));
  elements.main.append(grid);
}

async function renderRevisions(updateUrl = true) {
  const request = ++state.request;
  state.observer?.disconnect();
  if (updateUrl) setUrl({ mode: "releases", review: "1" });
  const heading = node("header", "overview-header");
  heading.append(node("h1", "", "Possible wording revisions"), node("p", "", "Similar entries were kept separate. These are review candidates, not established conflicts or current guidance."));
  const list = node("div", "results-list");
  const sentinel = node("div", "load-more", "Loading…");
  elements.main.replaceChildren(heading, list, sentinel);
  let offset = 0;
  let loading = false;
  async function load() {
    if (loading || request !== state.request) return;
    state.observer?.disconnect();
    loading = true;
    try {
      const data = await getJson("/api/revisions", { limit: 30, offset });
      if (request !== state.request) return;
      for (const pair of data.results) {
        const result = node("article", "result");
        for (const item of [pair.left, pair.right]) {
          const part = node("div", "revision-side");
          const metadata = node("p", "result-meta");
          metadata.append(sourceLink(`Release note ${item.version}`, sourceHref(item)), document.createTextNode(" · "), link(`${item.occurrence_count} source occurrence${item.occurrence_count === 1 ? "" : "s"}`, docUrl(item.id)));
          part.append(link(item.title, docUrl(item.id), "result-title"), metadata, node("p", "result-excerpt", item.excerpt));
          result.append(part);
        }
        list.append(result);
      }
      offset += data.limit;
      if (offset >= data.total) { sentinel.remove(); state.observer?.disconnect(); }
      else {
        state.observer = new IntersectionObserver((entries) => { if (entries.some((entry) => entry.isIntersecting)) load(); }, { rootMargin: "700px" });
        state.observer.observe(sentinel);
      }
    } catch (error) { if (request === state.request) showError(error); }
    finally { loading = false; }
  }
  await load();
}

function searchParameters() {
  const query = elements.search.value.trim();
  if (commandMode()) return { q: query, type: elements.category.value || "keyword", authority: elements.commandSource.value, reference_kind: state.mode, sort: "text", limit: 40, offset: state.offset };
  if (state.mode === "bugs") return { q: query, kind: "release_note", category: "limitations", edition: elements.version.value, limit: 30, offset: state.offset };
  return { q: query, kind: state.mode === "releases" ? "release_note" : elements.history.checked ? "all" : "manual", category: elements.category.value, version: elements.version.value, limit: 30, offset: state.offset };
}

async function runSearch(updateUrl = true, append = false) {
  const parameters = searchParameters();
  clearVirtualList();
  state.documentId = "";
  state.documentMetadata = null;
  state.documentToc = [];
  const request = ++state.request;
  const controller = new AbortController();
  state.searchAbort = controller;
  state.loading = true;
  state.observer?.disconnect();
  if (updateUrl) {
    const location = { mode: state.mode };
    if (parameters.q) location.q = parameters.q;
    if (elements.category.value) location.category = elements.category.value;
    if (elements.version.value) location.version = elements.version.value;
    if (elements.history.checked) location.history = "1";
    if (commandMode() && elements.commandSource.value !== "manual") location.authority = elements.commandSource.value;
    setUrl(location, "", true);
  }
  if (commandMode()) {
    const placeholders = node("div", "command-list-loading");
    placeholders.setAttribute("role", "status");
    placeholders.setAttribute("aria-label", "Loading command results");
    for (let index = 0; index < 12; index += 1) placeholders.append(commandLoadingRow());
    elements.main.replaceChildren(placeholders);
    window.scrollTo({ top: 0 });
  }
  try {
    const data = await getJson(commandMode() ? "/api/commands" : "/api/search", parameters, controller.signal);
    if (commandMode()) validateCommandPage(data, parameters.offset, parameters.limit);
    if (request !== state.request) return;
    state.loading = false;
    if (state.mode === "bugs") state.bugEntries = append ? [...state.bugEntries, ...data.results] : data.results;
    if (!append || state.mode === "bugs") renderNavigation();
    renderResults(data, append);
  } catch (error) {
    if (request === state.request) { state.loading = false; showError(error); }
  }
}

function renderResults(data, append = false) {
  const query = elements.search.value.trim();
  document.title = `${query || (commandMode() ? state.mode === "lua" ? "Lua" : "Commands" : state.mode === "bugs" ? "Known bugs" : "Browse")} · MA3 Reference`;
  const heading = node("div", "results-heading");
  const text = node("div", "");
  text.append(node("h1", "", query ? `Results for “${query}”` : commandMode() ? state.mode === "lua" ? "Lua reference" : "Command reference" : state.mode === "bugs" ? "Known limitations" : "Browse the reference"));
  const summary = node("p", "", `${formatCount(data.total)} results · `);
  if (state.mode === "bugs") summary.append(sourceLink(`From edition ${elements.version.value}`, editionHref(elements.version.value)));
  else if (commandMode()) {
    const scopeHref = elements.commandSource.value === "manual" ? manualHref() : elements.commandSource.value === "release-note" ? modeUrl("releases") : modeUrl(state.mode, { authority: "all", category: elements.category.value });
    summary.append(document.createTextNode(`${friendlyCategory(elements.category.value || "keyword")} · `), sourceLink(elements.commandSource.selectedOptions[0].textContent, scopeHref));
  } else if (state.mode === "releases") summary.append(sourceLink("Release history", modeUrl("releases")));
  else if (elements.history.checked) summary.append(sourceLink("Manual", manualHref()), document.createTextNode(", "), sourceLink("guides", modeUrl("manual")), document.createTextNode(" and "), sourceLink("release history", modeUrl("releases")));
  else summary.append(sourceLink("Current manual", manualHref()));
  text.append(summary);
  if (state.mode === "bugs") text.append(node("p", "limits-note", "Includes restrictions as well as software bugs."));
  heading.append(text);
  const list = append ? elements.main.querySelector(".results-list") : node("div", "results-list");
  if (!commandMode()) for (const item of data.results) list.append(documentResult(item));
  if (!data.results.length) {
    const empty = node("div", "empty-state");
    const explanation = node("p", "", state.mode === "bugs" ? "No matching known limitations were extracted for this edition. Check its " : "Try fewer words, an exact keyword, or a broader category. Historical material is available through ");
    explanation.append(sourceLink(state.mode === "bugs" ? "original release notes" : "release history", state.mode === "bugs" ? editionHref(elements.version.value) : modeUrl("releases")), document.createTextNode("."));
    empty.append(node("h2", "", "No matching entries"), explanation);
    list.append(empty);
  }
  if (!append) elements.main.replaceChildren(heading, list);
  elements.main.querySelector(".load-more")?.remove();
  if (commandMode() && data.total) {
    const parameters = searchParameters();
    state.virtualList = new VirtualCommandList(list, data, (offset, signal) => getJson("/api/commands", { ...parameters, offset }, signal), commandResult, parameters.type === "keyword" ? 39 : 145);
  } else if (data.offset + data.limit < data.total) {
    const sentinel = node("div", "load-more", "Loading more…");
    elements.main.append(sentinel);
    state.observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting) || state.loading) return;
      state.offset = data.offset + data.limit;
      runSearch(false, true);
    }, { rootMargin: "700px" });
    state.observer.observe(sentinel);
  }
}

function documentResult(item) {
  const result = node("article", "result");
  const top = node("div", "result-top");
  top.append(link(state.mode === "bugs" ? item.title.replace(/^Known Limitations:\s*/i, "") : item.title, docUrl(item.id), "result-title"));
  if (item.exact_match) top.append(node("span", "badge exact", "Exact match"));
  const metadata = node("p", "result-meta", `${friendlyCategory(item.category)} · `);
  metadata.append(sourceLink(`Source v${item.version}`, sourceHref(item)));
  if (item.matched_edition) metadata.append(document.createTextNode(" · "), sourceLink(`Edition ${item.matched_edition}`, editionHref(item.matched_edition, item)));
  if (item.matched_version && item.matched_version !== item.version) metadata.append(document.createTextNode(" · "), sourceLink(item.version_match_roles?.includes("edition") ? `Included in edition ${item.matched_version}` : `Also recorded for ${item.matched_version}`, editionHref(item.matched_version, item)));
  if (item.occurrence_count > 1) metadata.append(document.createTextNode(" · "), link(`${item.occurrence_count} source occurrences`, docUrl(item.id)));
  const occurrence = selectedOccurrence(item);
  if (occurrence?.source_url) metadata.append(document.createTextNode(" · "), sourceLink(`PDF${occurrence.source_page ? ` p. ${occurrence.source_page}` : ""}`, occurrence.source_url));
  const excerpt = node("p", "result-excerpt", item.excerpt);
  state.commandLinks?.decorate(excerpt, { documentId: item.id });
  result.append(top, breadcrumbsFor(item, true), metadata, excerpt);
  return result;
}

function commandCode(item, sources = commandSources(item)) {
  const code = node("pre", "result-code", item.text);
  state.commandLinks?.decorate(code, { mode: "code", language: item.language, documentId: item.document_id, matchedKeywords: sources.flatMap((source) => source.matched_keywords || []) });
  return code;
}

function commandResult(item) {
  const result = node("article", "result");
  const top = node("div", "result-top");
  top.append(link(item.command || "Command example", docUrl(item.document_id), "result-title"));
  if (item.type === "keyword") {
    result.classList.add("command-keyword");
    top.append(commandSourceLink(item, "command-version"));
    result.append(top);
    return result;
  }
  result.setAttribute("data-command-id", item.id);
  result.append(top, commandMetadata(commandSources(item)), commandCode(item));
  return result;
}

function sourcePanel(documentData) {
  const occurrence = selectedOccurrence(documentData);
  const sourceUrl = occurrence?.source_url || sourceHref(documentData);
  const panel = node("aside", "source-panel");
  panel.append(node("h2", "", "Source & provenance"));
  const details = node("dl", "");
  const rows = [
    ["Authority", documentData.source_kind === "live-introspection" ? "Live introspection supplement" : documentData.authority === "manual" ? "Current installed manual" : documentData.authority === "release-note" ? "Release-note supplement" : "Original synthesis", sourceHref(documentData)],
    ["Source version", documentData.version, sourceHref(documentData)],
    ["Selected edition", occurrence?.publication_version, sourceUrl],
    ["Page version", documentData.page_version && documentData.page_version !== documentData.version ? documentData.page_version : null, sourceHref(documentData)],
    ["Category", friendlyCategory(documentData.category)],
    ["PDF page", occurrence?.source_page || documentData.source_page, sourceUrl],
  ];
  for (const [name, value, href] of rows) if (value) {
    const description = node("dd", "");
    description.append(href ? sourceLink(value, href) : document.createTextNode(value));
    details.append(node("dt", "", name), description);
  }
  panel.append(details);
  if (occurrence?.source_url || documentData.source_url) {
    const source = link(new URL(sourceUrl, window.location.href).pathname.startsWith("/release-notes-pdf/") ? "Open source PDF ↗" : "Original source ↗", sourceUrl, "small-button");
    source.target = "_blank";
    source.rel = "noopener noreferrer";
    panel.append(source);
  }
  if (documentData.markdown_path) panel.append(link("Read Markdown ↗", `/api/document?id=${encodeURIComponent(documentData.id)}&format=markdown`, "small-button"));
  panel.append(button("Copy reference link", "small-button", async () => {
    try { await navigator.clipboard.writeText(window.location.href); showToast("Reference link copied"); }
    catch { showToast("Copy the URL from your browser address bar"); }
  }));
  return panel;
}

function validLiveBuild(build) {
  return build && typeof build === "object" && ["version", "codeType", "hostType"].every((field) => typeof build[field] === "string");
}

function liveApiDetails(documentData) {
  const signature = documentData.live_lua_signature;
  const tokens = documentData.live_keyword_tokens;
  if (signature !== undefined && (!signature || ["signature", "scope", "name", "source_function", "source_url", "captured_at"].some((field) => typeof signature[field] !== "string") || !validLiveBuild(signature.build))) throw new Error("The live Lua details returned invalid metadata.");
  if (tokens !== undefined && (!Array.isArray(tokens) || tokens.some((token) => !token || !Number.isSafeInteger(token.index) || token.index < 0 || ["name", "source_function"].some((field) => typeof token[field] !== "string") || !validLiveBuild(token.build) || token.behaviour_verified !== false))) throw new Error("The live token details returned invalid metadata.");
  if (!signature && !tokens?.length) return null;
  const details = node("details", "related-section live-api-details");
  details.append(node("summary", "", "Live API details"));
  if (signature) {
    const provenance = node("p", "result-meta");
    provenance.append(sourceLink(`${signature.source_function} · build ${signature.build.version}`, signature.source_url), document.createTextNode(` · captured ${signature.captured_at}`));
    details.append(provenance, node("pre", "result-code", signature.signature));
  }
  if (tokens?.length) {
    const provenance = node("p", "result-meta");
    const builds = [...new Set(tokens.map((token) => token.build.version))].join(", ");
    provenance.append(sourceLink(`GetTokenNameByIndex · build ${builds}`, docUrl("live-lua/coverage-audit")));
    const list = node("ul", "live-token-list");
    for (const token of tokens) {
      const entry = node("li", "");
      entry.append(node("code", "", token.name), document.createTextNode(` · internal token index ${token.index}`));
      list.append(entry);
    }
    details.append(provenance, node("p", "limits-note", "Token indices are internal metadata, not command syntax or aliases. The capture does not verify behaviour."), list);
  }
  return details;
}

function occurrencesPanel(occurrences) {
  if (!Array.isArray(occurrences) || !occurrences.length) return null;
  const details = node("details", "occurrences");
  details.append(node("summary", "", `Found in ${occurrences.length} source occurrence${occurrences.length === 1 ? "" : "s"}`));
  const list = node("ul", "");
  for (const occurrence of occurrences) {
    const item = node("li", "");
    const title = `v${occurrence.version || occurrence.publication_version || "?"}${occurrence.publication_version && occurrence.publication_version !== occurrence.version ? ` · edition ${occurrence.publication_version}` : ""}${occurrence.source_page ? ` · page ${occurrence.source_page}` : ""}`;
    item.append(sourceLink(title, occurrence.source_url || docUrl(occurrence.document_id)));
    if (occurrence.section) item.append(document.createTextNode(" — "), sourceLink(occurrence.section, occurrence.source_url || docUrl(occurrence.document_id)));
    list.append(item);
  }
  details.append(list);
  return details;
}

function prepareArticleImages(article) {
  for (const image of article.querySelectorAll("img")) {
    for (const dimension of ["width", "height"]) {
      const maximum = image.getAttribute(`data-source-max-${dimension}`);
      if (maximum && /^[1-9]\d{0,3}$/.test(maximum)) image.style.setProperty(`--source-image-max-${dimension}`, `${Number(maximum)}px`);
    }
    const preserveNaturalSize = () => {
      if (!image.naturalWidth || !image.naturalHeight) return;
      image.style.setProperty("--image-natural-width", `${image.naturalWidth}px`);
      if ((image.naturalWidth > 320 || image.naturalHeight > 320) && !image.closest("a")) {
        const original = link("", image.src, "image-link");
        original.target = "_blank";
        original.rel = "noopener noreferrer";
        original.title = "Open image at full resolution";
        original.setAttribute("aria-label", image.alt ? `Open ${image.alt} at full resolution` : "Open image at full resolution");
        image.replaceWith(original);
        original.append(image);
      }
    };
    if (image.complete) preserveNaturalSize();
    else image.addEventListener("load", preserveNaturalSize, { once: true });
  }
}

async function openDocument(id, anchor = "", updateUrl = true, context = new URLSearchParams(), revealSidebar = true) {
  clearVirtualList();
  const request = ++state.request;
  const previousMode = state.mode;
  const previousDocumentId = state.documentId;
  setNavigationReveal(revealSidebar);
  state.observer?.disconnect();
  state.documentId = id;
  state.documentMetadata = null;
  const previousEdition = elements.version.value;
  const mode = context.get("mode") === "bugs" && id.startsWith("release") ? "bugs" : id.startsWith("release") ? "releases" : "manual";
  if (state.mode !== mode) { state.mode = mode; updateFilters(); }
  if (mode === "bugs" && releaseEditions().includes(context.get("version"))) elements.version.value = context.get("version");
  if (mode !== "bugs" || previousEdition !== elements.version.value) state.bugEntries = [];
  closeMenu();
  if (updateUrl) setUrl({ doc: id, ...(mode === "bugs" ? { mode, version: elements.version.value } : {}) }, anchor);
  elements.main.replaceChildren(node("p", "loading-line muted", "Loading source topic…"));
  try {
    const data = await getJson("/api/document", { id });
    if (request !== state.request) return;
    state.documentMetadata = data;
    state.documentToc = sourceTocTree(data.toc);
    const existingLink = [...elements.navigation.querySelectorAll("[data-document-id]")].some((item) => item.dataset.documentId === id);
    if (revealSidebar || previousMode !== mode || !existingLink || (mode === "releases" && previousDocumentId !== id)) {
      const scrollTop = elements.navigation.scrollTop;
      renderNavigation();
      if (!revealSidebar) {
        const missingSpace = Math.max(0, scrollTop + elements.navigation.clientHeight - elements.navigation.scrollHeight);
        if (missingSpace) navigationSpacer().style.height = `${Math.ceil(missingSpace)}px`;
        elements.navigation.scrollTop = scrollTop;
      }
    }
    document.title = `${data.title} · MA3 Reference`;
    const breadcrumbs = breadcrumbsFor(data);
    const heading = node("header", "document-heading");
    const labels = node("div", "document-labels");
    const subtitle = node("span", "document-subtitle");
    if (mode === "bugs") subtitle.append(sourceLink(`Edition ${elements.version.value}`, editionHref(elements.version.value, data)), document.createTextNode(" · "));
    subtitle.append(sourceLink(`${mode === "bugs" ? "Source v" : "Version "}${data.version}`, sourceHref(data)), document.createTextNode(` · ${friendlyCategory(data.category)}`));
    labels.append(subtitle);
    heading.append(breadcrumbs, labels, node("h1", "", data.title));
    const layout = node("div", "document-layout");
    const body = node("div", "document-body");
    if (data.authority === "release-note") {
      const warning = node("div", "history-warning");
      warning.append(node("strong", "", "Release-note context. "), document.createTextNode("This describes the "), sourceLink("source version", sourceHref(data)), document.createTextNode(". Later changes may supersede it; use the "), sourceLink("current manual", manualHref()), document.createTextNode(" to establish present behaviour. Examples are not live-tested commands."));
      body.append(warning);
    }
    const article = node("article", "article");
    article.classList.add(`authority-${data.authority}`);
    // This is the server-sanitized article fragment, never a query or raw source HTML.
    article.innerHTML = data.html;
    prepareArticleImages(article);
    state.commandLinks?.decorate(article, { documentId: data.id });
    for (const paragraph of article.querySelectorAll("p")) {
      if (!paragraph.textContent.trim() && !paragraph.querySelector("img,table,figure,iframe")) {
        if (paragraph.id || paragraph.querySelector("[id]")) paragraph.classList.add("empty-spacing");
        else paragraph.remove();
      }
    }
    for (const title of article.querySelectorAll("h1,h2,h3,h4,h5,h6")) {
      for (const breakElement of title.querySelectorAll("br")) breakElement.remove();
      if (!title.textContent.trim() && !title.id) title.remove();
    }
    body.append(article);
    const loadMoreCommands = Boolean(data.command_lookup) || (data.commands || []).length >= 200 || data.commands_page?.total > (data.commands || []).length;
    const extracts = commandExtracts(data.commands, data, loadMoreCommands);
    if (extracts) body.append(extracts);
    const liveDetails = liveApiDetails(data);
    if (liveDetails) body.append(liveDetails);
    const occurrences = occurrencesPanel(data.occurrences);
    if (occurrences) body.append(occurrences);
    if (data.revision_candidates?.length) {
      const revisions = node("section", "related-section");
      revisions.append(node("h2", "", "Possible wording revisions"), node("p", "", "Similar source entries were kept separate. Compare their wording and versions; similarity alone does not establish a conflict."));
      for (const pair of data.revision_candidates) {
        const other = pair.left.id === data.id ? pair.right : pair.left;
        const reference = node("div", "related-item");
        const provenance = node("small", "");
        provenance.append(sourceLink(`Release note ${other.version}`, sourceHref(other)));
        reference.append(link(other.title, docUrl(other.id)), provenance);
        revisions.append(reference);
      }
      body.append(revisions);
    }
    if (data.related?.length) {
      const related = node("details", "related-section related-history");
      const explanation = node("p", "", "Matched by topic text. These links offer historical context; they do not change the ");
      explanation.append(sourceLink("manual above", sourceHref(data)), document.createTextNode("."));
      related.append(node("summary", "", "Related release history"), explanation);
      for (const item of data.related) {
        const reference = node("div", "related-item");
        const provenance = node("small", "");
        provenance.append(sourceLink(`Release note ${item.version}`, sourceHref(item)));
        if (item.occurrence_count > 1) provenance.append(document.createTextNode(" · "), link(`${item.occurrence_count} occurrences`, docUrl(item.id)));
        reference.append(link(item.title, docUrl(item.id)), provenance);
        related.append(reference);
      }
      body.append(related);
    }
    const panel = sourcePanel(data);
    const toc = node("nav", "on-this-page");
    toc.setAttribute("aria-label", "On this page");
    toc.append(node("h2", "", "On this page"));
    let headingIndex = 0;
    for (const title of article.querySelectorAll("h2,h3")) {
      if (!title.id) title.id = `section-${headingIndex}`;
      headingIndex += 1;
      toc.append(link(title.textContent, `#${encodeURIComponent(title.id)}`, ""));
      if (headingIndex >= 18) break;
    }
    if (extracts) { toc.append(link("Extracted syntax & command examples", "#extracted-commands")); headingIndex += 1; }
    if (headingIndex) panel.append(toc);
    layout.append(body, panel);
    elements.main.replaceChildren(heading, layout);
    if (extracts && loadMoreCommands) loadDocumentExtracts(data, extracts, request);
    revealNavigationSelection(anchor);
    if (anchor) {
      requestAnimationFrame(() => document.getElementById(anchor)?.scrollIntoView());
    } else window.scrollTo({ top: 0 });
  } catch (error) {
    if (request === state.request) showError(error);
  }
}

function route(revealSidebar = true) {
  setNavigationReveal(revealSidebar);
  const parameters = new URLSearchParams(window.location.search);
  const id = parameters.get("doc");
  if (id) {
    const mode = parameters.get("mode") === "bugs" && id.startsWith("release") ? "bugs" : id.startsWith("release") ? "releases" : "manual";
    const anchor = decodeURIComponent(window.location.hash.slice(1));
    if (id === state.documentId && state.documentMetadata && state.mode === mode && (mode !== "bugs" || parameters.get("version") === elements.version.value)) {
      revealNavigationSelection(anchor);
      return;
    }
    openDocument(id, anchor, false, parameters, revealSidebar);
    return;
  }
  selectMode(parameters.get("mode") || "manual", false, parameters);
}

document.addEventListener("click", (event) => {
  const anchor = event.target.closest("a");
  if (!anchor || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0 || anchor.target === "_blank") return;
  const url = new URL(anchor.href, window.location.origin);
  if (url.origin !== window.location.origin) return;
  const id = url.searchParams.get("doc");
  if (url.pathname === "/" && id) {
    const revealSidebar = !elements.navigation.contains(anchor);
    setNavigationReveal(revealSidebar);
    if (id === state.documentId && url.hash && url.search === window.location.search) {
      event.preventDefault();
      window.history.pushState({}, "", url.pathname + url.search + url.hash);
      route(revealSidebar);
      closeMenu();
      document.getElementById(decodeURIComponent(url.hash.slice(1)))?.scrollIntoView();
      return;
    }
    event.preventDefault();
    window.history.pushState({}, "", url.pathname + url.search + url.hash);
    route(revealSidebar);
  } else if (url.pathname === "/" && (url.searchParams.has("mode") || (!url.search && !url.hash))) {
    event.preventDefault();
    window.history.pushState({}, "", url.pathname + url.search + url.hash);
    route();
  }
});

for (const tab of document.querySelectorAll("[data-mode]")) tab.addEventListener("click", () => selectMode(tab.dataset.mode));
elements.search.addEventListener("input", () => {
  clearTimeout(state.timer);
  state.offset = 0;
  state.timer = setTimeout(() => {
    if (!elements.search.value.trim() && !elements.category.value && !elements.history.checked && !elements.version.value && !commandMode()) {
      setUrl(state.mode === "manual" ? {} : { mode: state.mode }, "", true);
      if (state.mode === "manual") renderHome(); else renderReleases();
    } else runSearch();
  }, 160);
});
elements.search.addEventListener("keydown", (event) => {
  if (event.key === "Enter") { clearTimeout(state.timer); state.offset = 0; runSearch(); }
  if (event.key === "Escape") elements.search.blur();
});
for (const field of [elements.history, elements.category, elements.version]) field.addEventListener("change", () => { state.offset = 0; state.bugEntries = []; runSearch(); });
elements.commandSource.addEventListener("change", () => {
  if (elements.commandSource.value === "release-note" && elements.category.value === "keyword") elements.category.value = "example";
  state.offset = 0;
  runSearch();
});
document.querySelector("#overview-button").addEventListener("click", () => selectMode(state.mode, true, new URLSearchParams(state.mode === "bugs" ? { version: elements.version.value } : {})));
document.querySelector("#menu-toggle").addEventListener("click", () => {
  const isOpen = document.body.classList.toggle("menu-open");
  document.querySelector("#menu-toggle").setAttribute("aria-expanded", String(isOpen));
});
function setTheme(theme) {
  const selected = theme === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = selected;
  const toggle = document.querySelector("#theme-toggle");
  toggle.textContent = selected === "light" ? "Dark mode" : "Light mode";
  toggle.setAttribute("aria-label", `Switch to ${selected === "light" ? "dark" : "light"} mode`);
  try { localStorage.setItem("ma3-docs-theme", selected); } catch { /* Storage may be unavailable in private contexts. */ }
}
document.querySelector("#theme-toggle").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
try { setTheme(localStorage.getItem("ma3-docs-theme") || "light"); } catch { setTheme("light"); }
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); elements.search.focus(); elements.search.select(); }
  if (event.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) { event.preventDefault(); elements.search.focus(); }
  if (event.key === "Escape") closeMenu();
});
window.addEventListener("popstate", () => route());

new ResizeObserver(() => {
  if (state.navigationReveal && state.documentId && state.documentMetadata) revealNavigationSelection(decodeURIComponent(window.location.hash.slice(1)));
}).observe(elements.navigation);

const searchBar = document.querySelector(".search-bar");
if (searchBar) {
  const updateSearchHeaderHeight = () => {
    document.documentElement.style.setProperty("--search-header-height", `${Math.ceil(searchBar.getBoundingClientRect().height)}px`);
  };
  new ResizeObserver(updateSearchHeaderHeight).observe(searchBar);
  updateSearchHeaderHeight();
}

async function initialize() {
  try {
    const [stats, navigation, references] = await Promise.all([getJson("/api/stats"), getJson("/api/navigation"), getJson("/api/references")]);
    state.commandLinks = new CommandLinks(references);
    state.referenceCounts = { commands: references.references.filter((item) => item.category !== "lua").length, lua: references.references.filter((item) => item.category === "lua").length };
    state.stats = stats;
    if (stats.pdf_count > 0) {
      const releaseStyles = document.createElement("link");
      releaseStyles.rel = "stylesheet";
      releaseStyles.href = "/assets/releases/source-styles.css";
      document.head.append(releaseStyles);
    }
    state.navigation = navigation;
    indexNavigation();
    document.querySelector("#corpus-status").textContent = `${formatCount(stats.documents)} entries`;
    if (!navigator.platform.toLowerCase().includes("mac")) document.querySelector("#search-shortcut").textContent = "Ctrl K";
    route();
  } catch (error) { showError(error, initialize); document.querySelector("#corpus-status").textContent = "Index unavailable"; }
}

initialize();
