"use strict";

// Keep source text and markup intact: only text-node slices become links.
class CommandLinks {
  constructor(payload) {
    if (!payload || !Array.isArray(payload.references)) throw new Error("The command reference catalog is invalid.");
    this.references = new Map();
    this.terms = new Map();
    for (const reference of payload.references) {
      if (!reference || typeof reference.document_id !== "string" || !(reference.document_id.startsWith("manual/") || (reference.category === "lua" && /^live-lua\/(?:free|object)-[a-z0-9_]+$/.test(reference.document_id))) || typeof reference.command !== "string" || !reference.command || !["command", "option", "lua"].includes(reference.category) || !Array.isArray(reference.terms) || (reference.scope !== undefined && typeof reference.scope !== "string")) throw new Error("The command reference catalog contains an invalid reference.");
      this.references.set(reference.document_id, reference);
      for (const entry of reference.terms) {
        if (!entry || typeof entry.term !== "string" || !entry.term || !["canonical", "documented-shortcut"].includes(entry.origin)) throw new Error("The command reference catalog contains an invalid spelling.");
        const key = entry.term.toLowerCase();
        const candidates = this.terms.get(key) || [];
        candidates.push({ reference, term: entry.term, origin: entry.origin });
        this.terms.set(key, candidates);
      }
    }
  }

  tokens(text, lua = false, strings = true) {
    const result = [];
    let position = 0;
    while (position < text.length) {
      const start = position;
      const rest = text.slice(position);
      if (/^\s/.test(rest)) { position += 1; continue; }
      if (lua && rest.startsWith("--")) {
        const long = /^--\[(=*)\[/.exec(rest);
        const end = long ? text.indexOf(`]${long[1]}]`, position + long[0].length) : text.indexOf("\n", position);
        position = end < 0 ? text.length : end + (long ? long[1].length + 2 : 1);
        continue;
      }
      const long = lua ? /^\[(=*)\[/.exec(rest) : null;
      const quote = strings && { '"': '"', "'": "'", "“": "”", "‘": "’" }[text[position]];
      if (long || quote) {
        const contentStart = position + (long ? long[0].length : 1);
        position = contentStart;
        const closing = long ? `]${long[1]}]` : quote;
        let complete = false;
        while (position < text.length) {
          if (!long && text[position] === "\\") { position += 2; continue; }
          if (text.startsWith(closing, position)) { complete = true; break; }
          position += 1;
        }
        const contentEnd = Math.min(position, text.length);
        position = Math.min(text.length, position + (complete ? closing.length : 0));
        result.push({ value: text.slice(contentStart, contentEnd), start, end: position, kind: complete ? "string" : "incomplete-string", contentStart, contentEnd });
        continue;
      }
      const word = /^(?:\/[A-Za-z_][A-Za-z_0-9]*|[A-Za-z_][A-Za-z_0-9]*)/.exec(rest);
      const number = /^\d+(?:\.\d+)*/.exec(rest);
      let kind = "symbol";
      if (word) {
        kind = "word";
        position += word[0].length;
        if (word[0].toLowerCase() === "go" && /[+-]/.test(text[position] || " ")) position += 1;
      } else if (number) {
        kind = "number";
        position += number[0].length;
      } else if (rest.startsWith("#[")) {
        const placeholder = /^#\[[^\]\n]*\]/.exec(rest);
        position += placeholder ? placeholder[0].length : 2;
      } else if (rest.startsWith("<<<") || rest.startsWith(">>>")) position += 3;
      else position += 1;
      const value = text.slice(start, position);
      result.push({ value: value.startsWith("#[") ? "#[Object]" : value, start, end: position, kind });
      if (value === "$") {
        const variable = /^[A-Za-z_][A-Za-z_0-9]*/.exec(text.slice(position));
        if (variable) position += variable[0].length;
      }
    }
    return result;
  }

  target(token, options = {}) {
    let candidates = this.terms.get(token.value.toLowerCase()) || [];
    if (options.lua) {
      candidates = candidates.filter((entry) => entry.reference.category === "lua" && entry.reference.command === token.value);
      if (candidates.length > 1 && options.hints?.size) {
        const hinted = candidates.filter((entry) => options.hints.has(entry.reference.document_id));
        if (hinted.length) candidates = hinted;
      }
      if (candidates.length > 1) {
        const scope = options.scope || (options.member ? "manual/lua_object" : "manual/lua_objectfree");
        candidates = candidates.filter((entry) => entry.reference.scope === scope);
      }
    } else {
      candidates = candidates.filter((entry) => entry.reference.category !== "lua");
      if (options.prose) {
        candidates = candidates.filter((entry) => entry.origin === "canonical" && (options.emphasized || entry.reference.command === token.value) && (token.kind === "word" || /[A-Za-z]/.test(token.value)));
      }
      const canonical = candidates.filter((entry) => entry.origin === "canonical");
      if (canonical.length) candidates = canonical;
    }
    if (options.hints?.size) {
      const hinted = candidates.filter((entry) => options.hints.has(entry.reference.document_id));
      if (hinted.length) candidates = hinted;
    }
    const identifiers = new Set(candidates.map((entry) => entry.reference.document_id));
    return identifiers.size === 1 ? candidates[0].reference : null;
  }

  matches(text, options = {}) {
    const initial = this.tokens(text, false, options.mode === "code");
    const inferredLua = options.mode === "code" && !["ma3", "lua"].includes(options.language) && initial.some((token, index) => token.kind === "word" && initial[index + 1]?.value === "(" && initial[index - 1]?.value !== "function" && this.target(token, { lua: true, scope: options.scope, member: [".", ":"].includes(initial[index - 1]?.value) }));
    const lua = options.language === "lua" || inferredLua;
    const lexical = lua ? this.tokens(text, true) : initial;
    const found = [];
    const prompt = options.mode === "code" && !lua ? /^[^>\n]*(?:@|\[Fixture\])[^>\n]*>\s*/.exec(text) : null;
    const hints = new Set((options.matchedKeywords || []).map((entry) => entry.keyword_document_id));
    for (let index = 0; index < lexical.length; index += 1) {
      const token = lexical[index];
      if (["string", "incomplete-string", "number"].includes(token.kind) || (prompt && token.start < prompt[0].length)) continue;
      const emphasized = options.emphasizedAt ? options.emphasizedAt(token.start) : false;
      let reference = null;
      if (lua) {
        if (token.kind !== "word" || lexical[index + 1]?.value !== "(" || lexical[index - 1]?.value === "function") continue;
        reference = this.target(token, { lua: true, scope: options.scope, member: [".", ":"].includes(lexical[index - 1]?.value), hints });
        if (["Cmd", "CmdIndirect", "CmdIndirectWait"].includes(token.value)) {
          const argument = lexical[index + 2];
          if (argument?.kind === "string" && [")", ","].includes(lexical[index + 3]?.value) && !text.slice(argument.contentStart, argument.contentEnd).includes("\\")) {
            for (const match of this.matches(argument.value, { mode: "code", matchedKeywords: options.matchedKeywords })) {
              found.push({ ...match, start: match.start + argument.contentStart, end: match.end + argument.contentStart });
            }
          }
        }
      } else if (options.mode !== "code" && options.scope) {
        reference = this.target(token, { lua: true, scope: options.scope, hints });
      }
      if (!reference && !lua) reference = this.target(token, { prose: options.mode !== "code", emphasized, hints });
      if (reference) found.push({ start: token.start, end: token.end, reference });
    }
    return found.sort((left, right) => left.start - right.start);
  }

  decorate(root, options = {}) {
    const owner = this.references.get(options.documentId);
    const scope = owner?.category === "lua" ? owner.scope : "";
    const ignored = "a,button,input,textarea,select,script,style,nav,.source-note,.source-meta,.rn-toc,.CHCodeSample_header,.maInputBoxImageCell";
    const codeSelector = "pre,code,samp,.maInputBoxTextCell,.commandLineInput,.inlineCode,.command-example";
    const groups = [];
    const walker = root.ownerDocument.createTreeWalker(root, 5);
    let current = null;
    while (walker.nextNode()) {
      const textNode = walker.currentNode;
      if (textNode.nodeType === 1) {
        if (["BR", "HR"].includes(textNode.tagName) && current) current.text += "\n";
        continue;
      }
      const parent = textNode.parentElement;
      if (!parent) continue;
      const code = parent.closest(codeSelector);
      const block = code || parent.closest("p,li,td,th,h1,h2,h3,h4,h5,h6,figcaption,dt,dd") || root;
      if (!current || current.block !== block) {
        current = { block, code, text: "", parts: [] };
        groups.push(current);
      }
      current.parts.push({ node: textNode, start: current.text.length, end: current.text.length + textNode.data.length, ignored: Boolean(parent.closest(ignored)), emphasized: Boolean(parent.closest("strong,b,.hardkey")) });
      current.text += textNode.data;
    }
    for (const group of groups) {
      const luaContainer = group.code?.closest(".CHCodeSample_container");
      const language = options.language || (luaContainer?.querySelector(".CHCodeSample_langName")?.textContent.trim().toLowerCase() === "lua" ? "lua" : undefined);
      const matches = this.matches(group.text, { ...options, scope, language, mode: options.mode || (group.code ? "code" : "prose"), emphasizedAt: (start) => group.parts.some((part) => part.start <= start && part.end > start && part.emphasized) });
      for (const part of group.parts) {
        if (part.ignored) continue;
        const intersections = matches.filter((match) => match.start < part.end && match.end > part.start && !group.parts.some((other) => other.ignored && match.start < other.end && match.end > other.start));
        if (!intersections.length) continue;
        const fragment = root.ownerDocument.createDocumentFragment();
        let offset = 0;
        for (const match of intersections) {
          const start = Math.max(0, match.start - part.start);
          const end = Math.min(part.node.data.length, match.end - part.start);
          fragment.append(root.ownerDocument.createTextNode(part.node.data.slice(offset, start)));
          const anchor = root.ownerDocument.createElement("a");
          anchor.className = "command-reference";
          anchor.href = `/?${new URLSearchParams({ doc: match.reference.document_id })}`;
          anchor.textContent = part.node.data.slice(start, end);
          fragment.append(anchor);
          offset = end;
        }
        fragment.append(root.ownerDocument.createTextNode(part.node.data.slice(offset)));
        part.node.replaceWith(fragment);
      }
    }
    for (const anchor of root.querySelectorAll("a[href]")) {
      const destination = new URL(anchor.getAttribute("href"), root.ownerDocument.baseURI);
      if (destination.origin === new URL(root.ownerDocument.baseURI).origin && destination.pathname === "/" && this.references.has(destination.searchParams.get("doc"))) anchor.classList.add("command-reference");
    }
  }
}
