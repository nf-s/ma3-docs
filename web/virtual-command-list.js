"use strict";

class VirtualListLayout {
  constructor(total, estimate) {
    this.sizes = Array(total).fill(estimate);
    this.offsets = [];
    this.rebuild();
  }

  rebuild() {
    this.offsets = [0];
    for (const size of this.sizes) this.offsets.push(this.offsets[this.offsets.length - 1] + size);
  }

  measure(index, height) {
    if (!Number.isFinite(height) || height <= 0 || Math.abs(this.sizes[index] - height) < .5) return false;
    this.sizes[index] = height;
    this.rebuild();
    return true;
  }

  indexAt(position) {
    let low = 0;
    let high = this.sizes.length;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (this.offsets[middle + 1] <= position) low = middle + 1;
      else high = middle;
    }
    return Math.min(low, Math.max(0, this.sizes.length - 1));
  }

  range(top, height, overscan = 600) {
    if (!this.sizes.length) return { start: 0, end: 0 };
    return { start: this.indexAt(Math.max(0, top - overscan)), end: Math.min(this.sizes.length, this.indexAt(top + height + overscan) + 1) };
  }

  get height() { return this.offsets[this.offsets.length - 1]; }
}

function validateCommandPage(page, offset, pageSize, total = null) {
  if (!page || !Array.isArray(page.results) || !Number.isSafeInteger(page.total) || page.total < 0 || page.offset !== offset || page.limit !== pageSize || (total !== null && page.total !== total)) throw new Error("The command catalog returned an invalid page. Refresh to try again.");
  if (page.results.length !== Math.min(pageSize, Math.max(0, page.total - offset))) throw new Error("The command catalog returned an incomplete page.");
  for (const item of page.results) {
    if (item?.source_records !== undefined && !Array.isArray(item.source_records)) throw new Error("The command catalog returned invalid sources.");
    const sources = Array.isArray(item?.source_records) && item.source_records.length ? item.source_records : [item];
    for (const source of [item, ...sources]) {
      if (!source || ["id", "document_id", "text", "version"].some((field) => typeof source[field] !== "string") || !["keyword", "syntax", "example"].includes(source.type) || !["manual", "release-note", "editorial"].includes(source.authority)) throw new Error("The command catalog returned an invalid record.");
    }
  }
  return page;
}

class CommandPageCache {
  constructor(initialPage, fetchPage, onChange) {
    this.pageSize = initialPage.limit;
    this.total = initialPage.total;
    validateCommandPage(initialPage, 0, this.pageSize);
    this.pages = new Map([[0, initialPage]]);
    this.errors = new Map();
    this.pending = new Map();
    this.wanted = [];
    this.fetchPage = fetchPage;
    this.onChange = onChange;
    this.closed = false;
  }

  item(index) {
    const offset = Math.floor(index / this.pageSize) * this.pageSize;
    return this.pages.get(offset)?.results[index - offset];
  }

  ensure(start, end, priority = null) {
    this.wanted = priority === null ? [] : [Math.floor(priority / this.pageSize) * this.pageSize];
    for (let offset = Math.floor(start / this.pageSize) * this.pageSize; offset < end; offset += this.pageSize) if (!this.wanted.includes(offset)) this.wanted.push(offset);
    for (const offset of this.pages.keys()) {
      if (this.pages.size <= 12) break;
      if (!this.wanted.includes(offset)) this.pages.delete(offset);
    }
    for (const [offset, controller] of this.pending) {
      if (!this.wanted.includes(offset)) controller.abort();
    }
    this.pump();
  }

  retry(offset) {
    this.errors.delete(offset);
    this.pump();
  }

  pump() {
    if (this.closed) return;
    for (const offset of this.wanted) {
      if (this.pending.size >= 2) break;
      if (this.pages.has(offset) || this.pending.has(offset) || this.errors.has(offset)) continue;
      const controller = new AbortController();
      this.pending.set(offset, controller);
      Promise.resolve().then(() => this.fetchPage(offset, controller.signal)).then((page) => {
        if (!this.closed && !controller.signal.aborted) this.pages.set(offset, validateCommandPage(page, offset, this.pageSize, this.total));
      }).catch((error) => {
        if (!this.closed && !controller.signal.aborted) this.errors.set(offset, error);
      }).finally(() => {
        this.pending.delete(offset);
        if (!this.closed) { this.onChange(); this.pump(); }
      });
    }
  }

  destroy() {
    this.closed = true;
    for (const controller of this.pending.values()) controller.abort();
    this.pending.clear();
  }
}

function commandLoadingRow() {
  const placeholder = document.createElement("div");
  placeholder.className = "command-placeholder";
  placeholder.setAttribute("aria-label", "Loading command…");
  for (const className of ["placeholder-title", "placeholder-detail"]) {
    const line = document.createElement("span");
    line.className = `placeholder-line ${className}`;
    line.setAttribute("aria-hidden", "true");
    placeholder.append(line);
  }
  return placeholder;
}

class VirtualCommandList {
  constructor(container, initialPage, fetchPage, renderItem, estimate) {
    this.container = container;
    this.renderItem = renderItem;
    this.layout = new VirtualListLayout(initialPage.total, estimate);
    this.rows = new Map();
    this.frame = null;
    this.focusIndex = null;
    this.closed = false;
    this.cache = new CommandPageCache(initialPage, fetchPage, () => this.schedule());
    container.classList.add("virtual-command-list");
    container.setAttribute("role", "list");
    container.setAttribute("aria-live", "off");
    container.setAttribute("aria-label", "Command results");
    container.tabIndex = 0;
    this.layer = document.createElement("div");
    this.layer.className = "virtual-command-window";
    container.append(this.layer);
    this.resize = new ResizeObserver((entries) => {
      let changed = false;
      const top = Math.max(0, -this.container.getBoundingClientRect().top);
      const anchor = this.layout.indexAt(top);
      const previousOffset = this.layout.offsets[anchor];
      for (const entry of entries) {
        const index = Number(entry.target.dataset.virtualIndex);
        changed = this.layout.measure(index, entry.target.getBoundingClientRect().height) || changed;
      }
      if (changed) {
        if (top > 0) window.scrollBy({ top: this.layout.offsets[anchor] - previousOffset });
        this.schedule();
      }
    });
    this.onScroll = () => this.schedule();
    this.onResize = () => {
      const focusedRow = document.activeElement?.closest("[data-virtual-index]");
      if (focusedRow && this.container.contains(focusedRow)) this.focusIndex = Number(focusedRow.dataset.virtualIndex);
      this.rows.clear();
      this.resize.disconnect();
      this.layer.replaceChildren();
      this.schedule();
    };
    this.onKey = (event) => this.keydown(event);
    this.onFocus = () => this.schedule();
    window.addEventListener("scroll", this.onScroll, { passive: true });
    window.addEventListener("resize", this.onResize);
    container.addEventListener("keydown", this.onKey);
    container.addEventListener("focusin", this.onFocus);
    container.addEventListener("focusout", this.onFocus);
    this.render();
  }

  schedule() {
    if (this.frame === null && !this.closed) this.frame = requestAnimationFrame(() => { this.frame = null; this.render(); });
  }

  render() {
    if (this.closed || !this.container.isConnected) return;
    const top = Math.max(0, -this.container.getBoundingClientRect().top);
    const range = this.layout.range(top, window.innerHeight);
    const focusedRow = document.activeElement?.closest("[data-virtual-index]");
    const retained = focusedRow && this.container.contains(focusedRow) ? Number(focusedRow.dataset.virtualIndex) : null;
    const indices = new Set();
    for (let index = range.start; index < range.end; index += 1) indices.add(index);
    if (retained !== null) indices.add(retained);
    if (this.focusIndex !== null) indices.add(this.focusIndex);
    this.container.style.height = `${this.layout.height}px`;
    for (const [index, row] of this.rows) {
      if (!indices.has(index)) { this.resize.unobserve(row); row.remove(); this.rows.delete(index); }
    }
    for (const index of [...indices].sort((left, right) => left - right)) {
      const item = this.cache.item(index);
      const offset = Math.floor(index / this.cache.pageSize) * this.cache.pageSize;
      const error = this.cache.errors.get(offset);
      let row = this.rows.get(index);
      const status = item || (index === retained && row?.dataset.status === "ready") ? "ready" : error ? "error" : "loading";
      if (!row || row.dataset.status !== status) {
        if (row) { this.resize.unobserve(row); row.remove(); }
        row = document.createElement("div");
        row.className = "virtual-command-row";
        row.dataset.virtualIndex = String(index);
        row.dataset.status = status;
        row.setAttribute("role", "listitem");
        row.setAttribute("aria-posinset", String(index + 1));
        row.setAttribute("aria-setsize", String(this.cache.total));
        row.tabIndex = -1;
        if (item) row.append(this.renderItem(item));
        else if (error) {
          const retry = document.createElement("button");
          retry.type = "button";
          retry.className = "small-button";
          retry.textContent = "Could not load commands. Retry";
          retry.addEventListener("click", () => { this.focusIndex = index; this.cache.retry(offset); this.schedule(); });
          row.append(retry);
        } else row.append(commandLoadingRow());
        this.rows.set(index, row);
        const next = [...this.layer.children].find((child) => Number(child.dataset.virtualIndex) > index);
        this.layer.insertBefore(row, next || null);
        this.resize.observe(row);
      }
      row.style.transform = `translateY(${this.layout.offsets[index]}px)`;
    }
    this.cache.ensure(range.start, range.end, this.focusIndex);
    if (this.focusIndex !== null && this.cache.item(this.focusIndex)) {
      const row = this.rows.get(this.focusIndex);
      row?.querySelector("a,button")?.focus();
      this.focusIndex = null;
    }
  }

  keydown(event) {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.target.closest(".command-source-popover")) return;
    const row = event.target.closest("[data-virtual-index]");
    const current = row ? Number(row.dataset.virtualIndex) : 0;
    const position = this.layout.offsets[current];
    const destinations = { ArrowDown: row ? current + 1 : 0, ArrowUp: current - 1, PageDown: Math.max(current + 1, this.layout.indexAt(position + window.innerHeight)), PageUp: Math.min(current - 1, this.layout.indexAt(Math.max(0, position - window.innerHeight))), Home: 0, End: this.cache.total - 1 };
    if (!(event.key in destinations) || !this.cache.total) return;
    event.preventDefault();
    const index = Math.max(0, Math.min(this.cache.total - 1, destinations[event.key]));
    this.focusIndex = index;
    const header = document.querySelector(".search-bar");
    const visibleTop = header ? header.getBoundingClientRect().bottom : 0;
    window.scrollTo({ top: window.scrollY + this.container.getBoundingClientRect().top + this.layout.offsets[index] - visibleTop });
    this.schedule();
  }

  destroy() {
    this.closed = true;
    this.cache.destroy();
    this.resize.disconnect();
    if (this.frame !== null) cancelAnimationFrame(this.frame);
    window.removeEventListener("scroll", this.onScroll);
    window.removeEventListener("resize", this.onResize);
    this.container.removeEventListener("keydown", this.onKey);
    this.container.removeEventListener("focusin", this.onFocus);
    this.container.removeEventListener("focusout", this.onFocus);
  }
}
