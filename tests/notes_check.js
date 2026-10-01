// app/ui/notes.js rendered with a tiny fake DOM (run by test_ui.py with Node): release
// notes in GitHub Markdown become paragraphs, lists, headings and bold/italic/code, and
// nothing in the notes is ever interpreted as HTML. Prints "ok" or throws.
"use strict";
const assert = require("assert");

class Node_ {
  constructor(tag, text) { this.tagName = tag; this.text = text; this.children = []; }
  appendChild(c) { if (c.tagName === "#fragment") this.children.push(...c.children); else this.children.push(c); return c; }
  get lastChild() { return this.children[this.children.length - 1]; }
  set textContent(t) { this.children = t ? [new Node_("#text", String(t))] : []; }
  get textContent() { return this.tagName === "#text" ? this.text : this.children.map((c) => c.textContent).join(""); }
  // The rendered tree as compact markup, for comparison (text escaped so markup in notes shows).
  get html() {
    if (this.tagName === "#text") return this.text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const inner = this.children.map((c) => c.html).join("");
    return this.tagName === "#root" ? inner : `<${this.tagName.toLowerCase()}>${inner}</${this.tagName.toLowerCase()}>`;
  }
}
global.document = {
  createElement: (t) => new Node_(t.toUpperCase()),
  createTextNode: (t) => new Node_("#text", String(t)),
  createDocumentFragment: () => new Node_("#fragment"),
};
const { renderNotes, renderReleaseNotes } = require("../app/ui/notes.js");

function html(md) { const root = new Node_("#root"); renderNotes(root, md); return root.html; }

assert.strictEqual(html("Fixes for **EVP marks**. Update when it starts."),
  "<p>Fixes for <strong>EVP marks</strong>. Update when it starts.</p>");
assert.strictEqual(html("- **Browse folders** like File Explorer\n- *All recordings* switch\n\nDone."),
  "<ul><li><strong>Browse folders</strong> like File Explorer</li><li><em>All recordings</em> switch</li></ul><p>Done.</p>");
assert.strictEqual(html("## What's new\n1. one\n2. two"),
  "<h4>What's new</h4><ol><li>one</li><li>two</li></ol>");
assert.strictEqual(html("Run `openevp-st25.exe --wav` and see [the README](https://example.com/x)."),
  "<p>Run <code>openevp-st25.exe --wav</code> and see <span>the README</span>.</p>");
assert.strictEqual(html("line one\nline two"), "<p>line one line two</p>");
assert.strictEqual(html("Windows\r\n\r\n* item"), "<p>Windows</p><ul><li>item</li></ul>");
assert.strictEqual(html("- item\n  continues here"), "<ul><li>item continues here</li></ul>");
// Markup in the notes stays text; stray markers stay as they are.
assert.strictEqual(html("<img src=x onerror=alert(1)> **<b>bold</b>**"),
  "<p>&lt;img src=x onerror=alert(1)&gt; <strong>&lt;b&gt;bold&lt;/b&gt;</strong></p>");
assert.strictEqual(html("2 * 3 = 6 and a_b_c stays"), "<p>2 * 3 = 6 and a_b_c stays</p>");
assert.strictEqual(html(""), "");

// The update dialog: one collapsible section per release skipped over, newest first, each
// headed by its version (the newest as "What's new in ..."), its own headings beneath.
function releases(list, earlier) { const root = new Node_("#root"); renderReleaseNotes(root, list, earlier); return root; }
const long = "x".repeat(700);
let root = releases([{ version: "0.10.0", date: "2026-10-01", notes: "## Fixes\n- **one**" },
                     { version: "0.9.9", date: "2026-09-20", notes: long },
                     { version: "0.9.8", date: "", notes: "" }], 3);
assert.strictEqual(root.html,
  "<details><summary><span>What's new in 0.10.0</span><span>2026-10-01</span></summary>" +
  "<div><h4>Fixes</h4><ul><li><strong>one</strong></li></ul></div></details>" +
  `<details><summary><span>0.9.9</span><span>2026-09-20</span></summary><div><p>${long}</p></div></details>` +
  "<details><summary><span>0.9.8</span></summary><div><p>No notes for this release.</p></div></details>" +
  "<p>…and 3 earlier updates.</p>");
assert.deepStrictEqual(root.children.slice(0, 3).map((d) => d.open), [true, false, false]);   // long: only the newest open
// Short notes: all open; one earlier update is singular; no count when there are none.
root = releases([{ version: "0.9.8", notes: "a" }, { version: "0.9.7", notes: "b" }], 1);
assert.deepStrictEqual(root.children.slice(0, 2).map((d) => d.open), [true, true]);
assert.strictEqual(root.children[2].textContent, "…and 1 earlier update.");
assert.strictEqual(releases([{ version: "0.9.8", notes: "a" }], 0).children.length, 1);
// Markup in any release's notes, or in its version or date, stays text.
root = releases([{ version: "0.9.8", notes: "ok" },
                 { version: "<i>0.9.7</i>", date: "<b>d</b>", notes: "<script>alert(1)</script> **<img src=x>**" }]);
assert.strictEqual(root.children[1].html,
  "<details><summary><span>&lt;i&gt;0.9.7&lt;/i&gt;</span><span>&lt;b&gt;d&lt;/b&gt;</span></summary>" +
  "<div><p>&lt;script&gt;alert(1)&lt;/script&gt; <strong>&lt;img src=x&gt;</strong></p></div></details>");
assert.strictEqual(releases(null, 0).html, "");
console.log("ok");
