// Release notes (GitHub Markdown) shown in the update dialog. The notes come from the
// internet, so this never builds HTML from them: it creates elements and text nodes
// only. Supported: paragraphs, #/##/### headings, "- " / "* " / "1. " lists,
// **bold**, *italic* / _italic_, `code`, and [links](url) shown as their text.
"use strict";

function notesInline(text) {
  const frag = document.createDocumentFragment();
  // One pass, left to right: the earliest match wins; unmatched markers stay as text.
  const pattern = /\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\([^)\s]+\)|(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])|(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)/g;
  let last = 0;
  for (const m of text.matchAll(pattern)) {
    if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
    let el;
    if (m[1] !== undefined) { el = document.createElement("strong"); el.appendChild(notesInline(m[1])); }
    else if (m[2] !== undefined) { el = document.createElement("code"); el.textContent = m[2]; }
    else if (m[3] !== undefined) { el = document.createElement("span"); el.appendChild(notesInline(m[3])); }
    else { el = document.createElement("em"); el.appendChild(notesInline(m[4] !== undefined ? m[4] : m[5])); }
    frag.appendChild(el);
    last = m.index + m[0].length;
  }
  if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
  return frag;
}

function renderNotes(container, markdown) {
  container.textContent = "";
  const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
  let para = [];
  let list = null;
  const flushPara = () => {
    if (!para.length) return;
    const p = document.createElement("p");
    p.appendChild(notesInline(para.join(" ")));
    container.appendChild(p);
    para = [];
  };
  const endList = () => { list = null; };
  for (const raw of lines) {
    const line = raw.trim();
    let m;
    if (!line) { flushPara(); endList(); continue; }
    if ((m = /^(#{1,6})\s+(.*)$/.exec(line))) {
      flushPara(); endList();
      const h = document.createElement("h4");
      h.appendChild(notesInline(m[2].replace(/\s+#+$/, "")));
      container.appendChild(h);
      continue;
    }
    const bullet = /^[-*+]\s+(.*)$/.exec(line);
    const numbered = /^\d+[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      flushPara();
      const tag = bullet ? "UL" : "OL";
      if (!list || list.tagName !== tag) {
        list = document.createElement(tag.toLowerCase());
        container.appendChild(list);
      }
      const li = document.createElement("li");
      li.appendChild(notesInline((bullet || numbered)[1]));
      list.appendChild(li);
      continue;
    }
    if (list && /^\s{2,}/.test(raw)) {        // an indented continuation of the last list item
      const li = list.lastChild;
      li.appendChild(document.createTextNode(" "));
      li.appendChild(notesInline(line));
      continue;
    }
    endList();
    para.push(line);
  }
  flushPara();
}

// The update dialog's notes: one section per release the update skips over, newest first
// ({version, date, notes} as check_update returns them), each a collapsible <details> headed
// "What's new in <newest>" / "<older>", its notes beneath (their own headings as subheadings).
// The newest is open; the older ones are open too when everything together is short
// (RELEASES_OPEN_CHARS), otherwise collapsed. `earlier` releases beyond the list are counted.
const RELEASES_OPEN_CHARS = 600;

function renderReleaseNotes(container, releases, earlier) {
  container.textContent = "";
  const list = (releases || []).filter((r) => r && r.version);
  const short = list.reduce((n, r) => n + String(r.notes || "").length, 0) <= RELEASES_OPEN_CHARS;
  list.forEach((r, i) => {
    const section = document.createElement("details");
    section.className = "release";
    section.open = i === 0 || short;
    const summary = document.createElement("summary");
    const title = document.createElement("span");
    title.className = "release-version";
    title.textContent = i === 0 ? `What's new in ${r.version}` : String(r.version);
    summary.appendChild(title);
    if (r.date) {
      const date = document.createElement("span");
      date.className = "release-date";
      date.textContent = String(r.date);
      summary.appendChild(date);
    }
    section.appendChild(summary);
    const body = document.createElement("div");
    body.className = "release-notes";
    if (String(r.notes || "").trim()) renderNotes(body, r.notes);
    else {
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = "No notes for this release.";
      body.appendChild(p);
    }
    section.appendChild(body);
    container.appendChild(section);
  });
  if (earlier > 0) {
    const more = document.createElement("p");
    more.className = "muted release-earlier";
    more.textContent = `…and ${earlier} earlier update${earlier === 1 ? "" : "s"}.`;
    container.appendChild(more);
  }
}

if (typeof module !== "undefined") module.exports = { renderNotes, renderReleaseNotes };
